"""IST closure, binary COPY round trips, and manual-only deletion on rec-test-db."""

import threading
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import cast

import pytest
from conftest import FRIDAY_10_IST, TOKEN, Clock
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from nova_rec.api import create_app
from nova_rec.days import INDEX_TICK_COLUMNS, TICK_COLUMNS, bounds, get_day, maintain_days
from nova_rec.loop import RecorderLoop
from nova_rec.models import IndexTick, RecordedDay, Recording, Tick
from nova_rec.settings import Settings

DAY = FRIDAY_10_IST.date()
CLOSE = FRIDAY_10_IST.replace(hour=10, minute=1)
AUTH = {"x-rec-internal-token": TOKEN}
PATH = f"/internal/days/{DAY}"


@pytest.fixture
def clock() -> Clock:
    return Clock(CLOSE)


@pytest.fixture
def client(engine: Engine, settings: Settings, clock: Clock) -> Iterator[TestClient]:
    with TestClient(create_app(settings, sessionmaker(engine, expire_on_commit=False), clock)) as c:
        yield c


def seed(engine: Engine, at: datetime = FRIDAY_10_IST) -> None:
    with Session(engine) as db:
        for i, symbol in enumerate(["INFY", "INFY", "TCS"]):
            tick = Tick(
                exchange="NSE",
                symbol=symbol,
                received_at=at + timedelta(microseconds=i),
                last_price_paise=12345,
                last_qty=2,
                volume=200,
                exchange_ts=at,
                last_trade_ts=at,
            )
            for name in (
                "oi",
                "avg_price_paise",
                "buy_qty",
                "sell_qty",
                "open_paise",
                "high_paise",
                "low_paise",
                "close_paise",
                "oi_day_high",
                "oi_day_low",
            ):
                setattr(tick, name, 12000)
            for side in ("bid", "ask"):
                for suffix, values in [
                    ("price_paise", [12000, 11900]),
                    ("qty", [2, 3]),
                    ("orders", [1, 2]),
                ]:
                    setattr(tick, f"{side}_{suffix}", values)
            db.add(tick)
        db.add(
            IndexTick(
                symbol="NIFTY 50",
                received_at=at,
                exchange_ts=None,
                last_price_paise=2500000,
                high_paise=2510000,
                low_paise=2490000,
                open_paise=None,
                close_paise=2480000,
            )
        )
        db.commit()


def maintenance(engine: Engine, now: datetime = CLOSE) -> None:
    with Session(engine) as db:
        maintain_days(db, now)
        db.commit()


def test_copy_columns_match_models() -> None:
    assert tuple(Tick.__table__.columns.keys()) == TICK_COLUMNS
    assert tuple(IndexTick.__table__.columns.keys()) == INDEX_TICK_COLUMNS


def test_closes_only_after_window_and_without_a_running_recording(engine: Engine) -> None:
    seed(engine)
    for at in [CLOSE - timedelta(seconds=1), FRIDAY_10_IST, bounds(DAY)[0]]:
        maintenance(engine, at)
    with Session(engine) as db:
        assert db.get(RecordedDay, DAY) is None
        db.add(Recording(started_at=FRIDAY_10_IST, status="running"))
        db.commit()
    maintenance(engine)
    with Session(engine) as db:
        assert db.get(RecordedDay, DAY) is None
        recording = db.scalar(select(Recording))
        assert recording is not None
        recording.status = "completed"
        db.commit()
    maintenance(engine)
    with Session(engine) as db:
        day = db.get(RecordedDay, DAY)
        assert day is not None
        assert (day.ticks, day.index_ticks, day.stocks) == (3, 1, 2)
        assert day.closed_at == CLOSE
    seed(engine, FRIDAY_10_IST + timedelta(seconds=1))
    maintenance(engine, CLOSE + timedelta(days=1))
    with Session(engine) as db:
        assert get_day(db, DAY).ticks == 3  # frozen


def test_ist_boundaries_weekends_and_previous_days(engine: Engine) -> None:
    start, end = bounds(DAY)
    for at in [start - timedelta(microseconds=4), start, end - timedelta(microseconds=4), end]:
        seed(engine, at)
    maintenance(engine, start + timedelta(hours=8))  # before Friday's window
    with Session(engine) as db:
        assert list(db.scalars(select(RecordedDay.day))) == [DAY - timedelta(days=1)]
    maintenance(engine, CLOSE)
    with Session(engine) as db:
        assert get_day(db, DAY).ticks == 6
        assert db.get(RecordedDay, DAY + timedelta(days=1)) is None
    maintenance(engine, CLOSE + timedelta(days=3))  # Monday closes backlog, excludes Saturday
    with Session(engine) as db:
        assert db.get(RecordedDay, DAY + timedelta(days=1)) is None


@pytest.mark.parametrize("indices", [False, True])
def test_export_binary_copy_round_trips_every_column(
    engine: Engine, client: TestClient, indices: bool
) -> None:
    seed(engine)
    seed(engine, FRIDAY_10_IST - timedelta(days=1))
    maintenance(engine)
    table = "index_ticks" if indices else "ticks"
    columns = INDEX_TICK_COLUMNS if indices else TICK_COLUMNS
    response = client.get(PATH + ("/index-ticks" if indices else "/ticks"), headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.content.startswith(b"PGCOPY\n\xff\r\n\x00")
    with engine.connect() as db:
        raw = cast(Connection[tuple[object, ...]], db.connection.driver_connection)
        with raw.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE TEMP TABLE copied (LIKE {})").format(sql.Identifier(table))
            )
            fields = sql.SQL(", ").join(map(sql.Identifier, columns))
            with cursor.copy(
                sql.SQL("COPY copied ({}) FROM STDIN (FORMAT binary)").format(fields)
            ) as c:
                c.write(response.content)
            cursor.execute(sql.SQL("SELECT {} FROM copied ORDER BY received_at").format(fields))
            copied = cursor.fetchall()
            cursor.execute(
                sql.SQL(
                    "SELECT {} FROM {} WHERE received_at >= %s AND received_at < %s "
                    "ORDER BY received_at"
                ).format(fields, sql.Identifier(table)),
                bounds(DAY),
            )
            assert copied == cursor.fetchall()
            assert len(copied) == int(response.headers["X-Row-Count"]) == (1 if indices else 3)


@pytest.mark.parametrize("imported", [False, True])
def test_delete_only_requested_day_outside_window_keeps_registry(
    engine: Engine, client: TestClient, clock: Clock, imported: bool
) -> None:
    seed(engine)
    seed(engine, bounds(DAY)[0] - timedelta(seconds=1))  # shares a UTC chunk with target
    seed(engine, bounds(DAY)[1] + timedelta(seconds=1))
    maintenance(engine)
    rejected = client.post(PATH + "/delete", json={"novaHasDay": False}, headers=AUTH)
    assert rejected.status_code == 409 and rejected.json()["detail"] == "Day not imported"
    if imported:
        assert (
            client.post(
                PATH + "/imported", json={"ticks": 3, "indexTicks": 1}, headers=AUTH
            ).status_code
            == 200
        )
    accepted = client.post(PATH + "/delete", json={"novaHasDay": not imported}, headers=AUTH)
    assert accepted.status_code == 202 and accepted.json()["deletedAt"] is None
    clock.at += timedelta(seconds=5)
    repeated = client.post(PATH + "/delete", json={"novaHasDay": False}, headers=AUTH)
    assert repeated.json()["deleteRequestedAt"] == accepted.json()["deleteRequestedAt"]
    for suffix in ["/ticks", "/index-ticks", "/stocks"]:
        assert client.get(PATH + suffix, headers=AUTH).status_code == 410
    assert (
        client.post(
            PATH + "/imported", json={"ticks": 3, "indexTicks": 1}, headers=AUTH
        ).status_code
        == 409
    )
    maintenance(engine, FRIDAY_10_IST)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Tick)) == 9
    maintenance(engine)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Tick)) == 6
        assert db.scalar(select(func.count()).select_from(IndexTick)) == 2
        assert get_day(db, DAY).deleted_at == CLOSE
    maintenance(engine, CLOSE + timedelta(days=1))
    assert client.get("/internal/days", headers=AUTH).json()[0]["deletedAt"] is not None


def test_open_day_returns_404(engine: Engine, client: TestClient) -> None:
    seed(engine)
    for suffix in ["/stocks", "/ticks", "/index-ticks"]:
        assert client.get(PATH + suffix, headers=AUTH).status_code == 404
    for suffix, body in [
        ("/imported", {"ticks": 3, "indexTicks": 1}),
        ("/delete", {"novaHasDay": True}),
    ]:
        assert client.post(PATH + suffix, json=body, headers=AUTH).status_code == 404


def test_loop_maintains_days_even_without_session_key(engine: Engine) -> None:
    seed(engine)
    clock = Clock(FRIDAY_10_IST)
    loop = RecorderLoop(
        sessionmaker(engine, expire_on_commit=False), None, threading.Event(), now=clock
    )
    loop.step()
    with Session(engine) as db:
        assert db.get(RecordedDay, DAY) is None
    clock.at = CLOSE
    loop.step()
    with Session(engine) as db:
        row = db.get(RecordedDay, DAY)
        assert row is not None
        row.delete_requested_at = CLOSE
        db.commit()
    clock.at = FRIDAY_10_IST
    loop.step()
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Tick)) == 3
    clock.at = CLOSE
    loop.step()
    with Session(engine) as db:
        assert get_day(db, DAY).deleted_at == CLOSE


def test_drops_wholly_contained_chunks_without_removing_neighbors(engine: Engine) -> None:
    target = date(2027, 1, 4)
    start, end = bounds(target)
    with engine.begin() as db:
        db.execute(text("SELECT set_chunk_time_interval('ticks', INTERVAL '1 hour')"))
    try:
        for at in [
            start - timedelta(minutes=15),
            start + timedelta(hours=2),
            end + timedelta(minutes=15),
        ]:
            seed(engine, at)
        maintenance(engine, end + timedelta(hours=16))
        with Session(engine) as db:
            get_day(db, target).delete_requested_at = end
            before = db.scalar(
                text(
                    "SELECT count(*) FROM timescaledb_information.chunks "
                    "WHERE hypertable_name = 'ticks'"
                )
            )
            db.commit()
        maintenance(engine, end + timedelta(hours=17))
        with Session(engine) as db:
            after = db.scalar(
                text(
                    "SELECT count(*) FROM timescaledb_information.chunks "
                    "WHERE hypertable_name = 'ticks'"
                )
            )
            assert after == before - 1
            assert db.scalar(select(func.count()).select_from(Tick)) == 6
            assert db.scalar(select(func.count()).select_from(IndexTick)) == 2
    finally:
        with engine.begin() as db:
            db.execute(text("SELECT set_chunk_time_interval('ticks', INTERVAL '1 day')"))
