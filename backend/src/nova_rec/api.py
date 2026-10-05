"""`rec-api`: NOVA's only way in (R1, `docs/guides/API.md`); `/internal` calls need the token."""

import base64
import binascii
import hmac
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, time
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from nova_rec.crypto import TokenCipher
from nova_rec.db import create_db_engine, create_session_factory
from nova_rec.loop import IST, MAX_INSTRUMENTS
from nova_rec.models import RecIndex, Recording, RecStock, SyncState
from nova_rec.settings import Settings, get_settings

State = Literal["off", "waiting", "recording", "no_login"]


class Wire(BaseModel):
    """camelCase on the wire; unknown fields are refused."""

    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)


class StockIn(Wire):
    symbol: str = Field(min_length=1)
    instrument_token: int = Field(gt=0)


class IndexIn(Wire):
    name: str = Field(min_length=1)
    instrument_token: int = Field(gt=0)


class SyncIn(Wire):
    account_id: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    # The Kite access token sealed with NOVA_BROKER_TOKEN_KEY, base64; null without a live session.
    access_token_sealed: str | None
    expires_at: AwareDatetime | None
    enabled: bool
    stocks: list[StockIn]
    indices: list[IndexIn]


class StatusOut(Wire):
    state: State
    synced_at: datetime | None
    session_expires_at: datetime | None
    stocks: int
    indices: int
    # Stock ticks saved by recordings that started today (IST).
    today_ticks: int
    # The latest recording's error; null when it ran or runs without one.
    last_error: str | None


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def _sealed(body: SyncIn, cipher: TokenCipher | None) -> bytes | None:
    if body.access_token_sealed is None:
        return None
    if body.expires_at is None:
        raise _bad("expiresAt is required with accessTokenSealed")
    try:
        sealed = base64.b64decode(body.access_token_sealed, validate=True)
    except binascii.Error as exc:
        raise _bad("accessTokenSealed is not base64") from exc
    if cipher is not None:
        try:
            cipher.decrypt(sealed)
        except ValueError as exc:
            raise _bad("accessTokenSealed cannot be opened: NOVA_BROKER_TOKEN_KEY differs") from exc
    return sealed


def _check_selection(body: SyncIn) -> None:
    total = len(body.stocks) + len(body.indices)
    if total > MAX_INSTRUMENTS:
        raise _bad(f"Kite streams at most {MAX_INSTRUMENTS} instruments; got {total}")
    symbols = [s.symbol for s in body.stocks]
    names = [i.name for i in body.indices]
    tokens = [s.instrument_token for s in body.stocks] + [i.instrument_token for i in body.indices]
    for label, values in (("stock", symbols), ("index", names), ("instrument token", tokens)):
        if len(set(values)) != len(values):
            raise _bad(f"Each {label} may appear only once")


def apply_sync(db: Session, body: SyncIn, cipher: TokenCipher | None, now: datetime) -> None:
    """Replaces `sync_state`, `rec_stocks` and `rec_indices` in one transaction."""
    _check_selection(body)
    sealed = _sealed(body, cipher)
    values = {
        "account_id": body.account_id,
        "api_key": body.api_key,
        "access_token_encrypted": sealed,
        "expires_at": body.expires_at if sealed is not None else None,
        "enabled": body.enabled,
        "synced_at": now,
    }
    db.execute(
        insert(SyncState)
        .values(id=1, **values)
        .on_conflict_do_update(index_elements=[SyncState.id], set_=values)
    )
    db.execute(delete(RecStock))
    db.execute(delete(RecIndex))
    db.add_all(RecStock(symbol=s.symbol, instrument_token=s.instrument_token) for s in body.stocks)
    db.add_all(RecIndex(name=i.name, instrument_token=i.instrument_token) for i in body.indices)
    db.commit()


def read_status(db: Session, now: datetime) -> StatusOut:
    state = db.get(SyncState, 1)
    stocks = db.scalar(select(func.count()).select_from(RecStock)) or 0
    indices = db.scalar(select(func.count()).select_from(RecIndex)) or 0
    day_start = datetime.combine(now.astimezone(IST).date(), time(0), IST)
    today = db.scalar(
        select(func.coalesce(func.sum(Recording.rows_written), 0)).where(
            Recording.started_at >= day_start
        )
    )
    latest = db.scalars(select(Recording).order_by(Recording.id.desc()).limit(1)).first()
    running = db.scalar(select(func.count()).where(Recording.status == "running")) or 0
    expires = state.expires_at if state is not None else None
    live = state is not None and state.access_token_encrypted is not None
    label: State
    if state is None or not state.enabled:
        label = "off"
    elif running:
        label = "recording"
    elif not live or expires is None or expires <= now:
        label = "no_login"
    else:
        label = "waiting"
    return StatusOut(
        state=label,
        synced_at=state.synced_at if state is not None else None,
        session_expires_at=expires,
        stocks=stocks,
        indices=indices,
        today_ticks=int(today or 0),
        last_error=latest.error if latest is not None else None,
    )


def create_app(
    settings: Settings,
    factory: sessionmaker[Session],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    app = FastAPI(title="Recorder", docs_url=None, redoc_url=None, openapi_url=None)
    key = settings.broker_token_key
    cipher = TokenCipher(key.get_secret_value()) if key is not None else None
    expected = settings.internal_token.get_secret_value().encode("utf-8")

    def internal(request: Request) -> None:
        sent = request.headers.get("x-rec-internal-token", "").encode("utf-8")
        if not hmac.compare_digest(sent, expected):
            raise HTTPException(status_code=401, detail="Only NOVA may call this service")

    def database() -> Iterator[Session]:
        with factory() as db:
            yield db

    Db = Annotated[Session, Depends(database)]
    guarded = [Depends(internal)]

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.put("/internal/sync", dependencies=guarded)
    def put_sync(body: SyncIn, db: Db) -> StatusOut:
        at = now()
        apply_sync(db, body, cipher, at)
        return read_status(db, at)

    @app.get("/internal/status", dependencies=guarded)
    def get_status(db: Db) -> StatusOut:
        return read_status(db, now())

    return app


def app_factory() -> FastAPI:
    """`uvicorn --factory nova_rec.api:app_factory` (the `rec-api` container)."""
    settings = get_settings()
    engine = create_db_engine(settings.database_url.get_secret_value())
    return create_app(settings, create_session_factory(engine))
