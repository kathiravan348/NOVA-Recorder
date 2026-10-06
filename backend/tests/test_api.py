"""`rec-api`: health, token check, sync replace and limits, status states."""

import asyncio
import base64
from collections.abc import Iterator
from datetime import timedelta

import pytest
from conftest import FRIDAY_10_IST, TOKEN, Clock, sync_body
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import Message
from test_days import DAY, PATH, maintenance, seed

from nova_rec.api import create_app
from nova_rec.crypto import TokenCipher, new_key
from nova_rec.days import export_day, get_day
from nova_rec.models import Recording, RecStock, SyncState, Tick
from nova_rec.settings import Settings

AUTH = {"x-rec-internal-token": TOKEN}


@pytest.fixture
def clock() -> Clock:
    return Clock(FRIDAY_10_IST)


@pytest.fixture
def client(engine: Engine, settings: Settings, clock: Clock) -> Iterator[TestClient]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with TestClient(create_app(settings, factory, now=clock)) as test_client:
        yield test_client


def test_health_needs_no_token(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"x-rec-internal-token": "wrong"}])
def test_internal_calls_without_the_token_get_401(
    client: TestClient, key: str, headers: dict[str, str]
) -> None:
    assert client.put("/internal/sync", json=sync_body(key), headers=headers).status_code == 401
    assert client.put("/internal/sync", json={}, headers=headers).status_code == 401
    assert client.get("/internal/status", headers=headers).status_code == 401
    for suffix in ["", "/2026-09-25/stocks", "/2026-09-25/ticks", "/2026-09-25/index-ticks"]:
        assert client.get("/internal/days" + suffix, headers=headers).status_code == 401
    for suffix in ["imported", "delete"]:
        assert (
            client.post(f"/internal/days/2026-09-25/{suffix}", json={}, headers=headers).status_code
            == 401
        )


def test_sync_stores_the_session_still_sealed_and_the_selection(
    client: TestClient, engine: Engine, key: str
) -> None:
    body = sync_body(key)
    response = client.put("/internal/sync", json=body, headers=AUTH)

    assert response.status_code == 200
    status = response.json()
    assert status["stocks"] == 2 and status["indices"] == 1
    assert status["syncedAt"] is not None and status["state"] == "waiting"
    with Session(engine) as db:
        state = db.get(SyncState, 1)
        assert state is not None and state.api_key == "kitekeyAB12"
        assert state.access_token_encrypted == base64.b64decode(str(body["accessTokenSealed"]))
        assert TokenCipher(key).decrypt(state.access_token_encrypted) == "kite-access-token"


def test_sync_replaces_all_three_tables(client: TestClient, engine: Engine, key: str) -> None:
    client.put("/internal/sync", json=sync_body(key), headers=AUTH)
    second = sync_body(
        key,
        accessTokenSealed=None,
        expiresAt=None,
        enabled=False,
        stocks=[{"symbol": "SBIN", "instrumentToken": 779521}],
        indices=[],
    )
    status = client.put("/internal/sync", json=second, headers=AUTH).json()

    assert (status["stocks"], status["indices"], status["state"]) == (1, 0, "off")
    with Session(engine) as db:
        assert list(db.scalars(select(RecStock.symbol))) == ["SBIN"]
        state = db.get(SyncState, 1)
        assert state is not None and state.access_token_encrypted is None and not state.enabled


def test_sync_over_3000_instruments_is_400_and_keeps_the_old_list(
    client: TestClient, engine: Engine, key: str
) -> None:
    client.put("/internal/sync", json=sync_body(key), headers=AUTH)
    stocks = [{"symbol": f"S{n}", "instrumentToken": n + 1} for n in range(3000)]
    response = client.put("/internal/sync", json=sync_body(key, stocks=stocks), headers=AUTH)

    assert response.status_code == 400
    assert "at most 3000" in response.json()["detail"]
    with Session(engine) as db:
        assert sorted(db.scalars(select(RecStock.symbol))) == ["INFY", "TCS"]


def test_sync_of_exactly_3000_instruments_is_accepted(client: TestClient, key: str) -> None:
    stocks = [{"symbol": f"S{n}", "instrumentToken": n + 1} for n in range(2999)]
    response = client.put("/internal/sync", json=sync_body(key, stocks=stocks), headers=AUTH)
    assert response.status_code == 200 and response.json()["stocks"] == 2999


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "stocks": [
                {"symbol": "INFY", "instrumentToken": 1},
                {"symbol": "INFY", "instrumentToken": 2},
            ]
        },
        {"indices": [{"name": "NIFTY 50", "instrumentToken": 408065}]},  # INFY's token
        {"accessTokenSealed": "not base64!"},
        {"expiresAt": None},
        {"accessTokenSealed": base64.b64encode(TokenCipher(new_key()).encrypt("t")).decode()},
    ],
)
def test_bad_syncs_are_400(client: TestClient, key: str, overrides: dict[str, object]) -> None:
    response = client.put("/internal/sync", json=sync_body(key, **overrides), headers=AUTH)
    assert response.status_code == 400


def test_unknown_fields_are_refused(client: TestClient, key: str) -> None:
    response = client.put("/internal/sync", json=sync_body(key, extra=1), headers=AUTH)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "suffix, body",
    [
        ("imported", {"ticks": -1, "indexTicks": 0}),
        ("imported", {"ticks": "3", "indexTicks": 0}),
        ("imported", {"ticks": 3, "indexTicks": 0, "extra": True}),
        ("delete", {"novaHasDay": "true"}),
        ("delete", {"novaHasDay": True, "extra": True}),
    ],
)
def test_day_request_validation(client: TestClient, suffix: str, body: dict[str, object]) -> None:
    assert (
        client.post(f"/internal/days/2026-09-25/{suffix}", json=body, headers=AUTH).status_code
        == 422
    )


def test_invalid_day_is_422(client: TestClient) -> None:
    assert client.get("/internal/days/not-a-date/ticks", headers=AUTH).status_code == 422


def test_status_states(client: TestClient, engine: Engine, key: str, clock: Clock) -> None:
    def state() -> str:
        return str(client.get("/internal/status", headers=AUTH).json()["state"])

    assert state() == "off"  # nothing pushed yet
    client.put("/internal/sync", json=sync_body(key, enabled=False), headers=AUTH)
    assert state() == "off"
    client.put("/internal/sync", json=sync_body(key, accessTokenSealed=None), headers=AUTH)
    assert state() == "no_login"
    client.put("/internal/sync", json=sync_body(key), headers=AUTH)
    assert state() == "waiting"
    clock.at += timedelta(days=2)  # the pushed session has expired
    assert state() == "no_login"
    clock.at = FRIDAY_10_IST
    with Session(engine) as db:
        db.add(Recording(started_at=FRIDAY_10_IST, status="running", rows_written=7))
        db.commit()
    status = client.get("/internal/status", headers=AUTH).json()
    assert status["state"] == "recording" and status["todayTicks"] == 7
    assert status["lastError"] is None


def test_status_counts_only_todays_ticks_and_shows_the_last_error(
    client: TestClient, engine: Engine, key: str
) -> None:
    client.put("/internal/sync", json=sync_body(key), headers=AUTH)
    with Session(engine) as db:
        yesterday = FRIDAY_10_IST - timedelta(days=1)
        db.add(Recording(started_at=yesterday, status="completed", rows_written=100))
        db.add(Recording(started_at=FRIDAY_10_IST, status="completed", rows_written=5))
        db.add(Recording(started_at=FRIDAY_10_IST, status="failed", error="socket refused"))
        db.commit()
    status = client.get("/internal/status", headers=AUTH).json()
    assert status["todayTicks"] == 5 and status["lastError"] == "socket refused"
    assert status["sessionExpiresAt"] is not None and status["state"] == "waiting"


def test_list_stocks_import_and_no_automatic_deletion(
    engine: Engine, client: TestClient, clock: Clock
) -> None:
    seed(engine)
    seed(engine, FRIDAY_10_IST - timedelta(days=1))
    maintenance(engine)
    days = client.get("/internal/days", headers=AUTH).json()
    assert [d["day"] for d in days] == [str(DAY), str(DAY - timedelta(days=1))]
    assert set(days[0]) == {
        "day",
        "ticks",
        "indexTicks",
        "stocks",
        "closedAt",
        "importedAt",
        "deleteRequestedAt",
        "deletedAt",
    }
    assert client.get(PATH + "/stocks", headers=AUTH).json() == [
        {"symbol": "INFY", "ticks": 2},
        {"symbol": "TCS", "ticks": 1},
    ]
    bad = client.post(PATH + "/imported", json={"ticks": 2, "indexTicks": 1}, headers=AUTH)
    assert bad.status_code == 409 and bad.json()["detail"] == {"ticks": 3, "indexTicks": 1}
    good = client.post(PATH + "/imported", json={"ticks": 3, "indexTicks": 1}, headers=AUTH)
    assert good.status_code == 200
    clock.at += timedelta(days=30)
    again = client.post(PATH + "/imported", json={"ticks": 3, "indexTicks": 1}, headers=AUTH)
    assert again.json()["importedAt"] == good.json()["importedAt"]
    maintenance(engine, clock.at)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Tick)) == 6
        assert get_day(db, DAY).deleted_at is None


def test_export_lock_is_released_on_disconnect_before_first_chunk(engine: Engine) -> None:
    seed(engine)
    maintenance(engine)
    response = export_day(sessionmaker(engine), DAY, indices=False)
    with Session(engine) as db:
        db.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(OperationalError):
            get_day(db, DAY, lock=True)  # a deletion request cannot take the export's lock

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        raise RuntimeError("client gone")

    with pytest.raises(RuntimeError, match="client gone"):
        asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    with Session(engine) as db:
        db.execute(text("SET LOCAL lock_timeout = '100ms'"))
        assert get_day(db, DAY, lock=True).ticks == 3
