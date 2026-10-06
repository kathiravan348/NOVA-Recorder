"""Tables: `ticks`/`index_ticks` exactly as NOVA has them (R1 (3)), the pushed sync, recordings."""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Identity,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {str: Text(), datetime: DateTime(timezone=True)}


class Tick(Base):
    """One live stock tick: every field of Kite's `full` packet (NOVA `ticks`, same columns).

    Hypertable on `received_at`, 1-day chunks. Depth arrays hold the best 5 levels, best first.
    """

    __tablename__ = "ticks"
    __table_args__ = (
        CheckConstraint("exchange IN ('NSE', 'NFO')", name="exchange"),
        CheckConstraint("last_price_paise > 0 AND last_qty >= 0 AND volume >= 0", name="values"),
    )

    exchange: Mapped[str] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(primary_key=True)
    received_at: Mapped[datetime] = mapped_column(primary_key=True)
    exchange_ts: Mapped[datetime | None]
    last_price_paise: Mapped[int] = mapped_column(BigInteger)
    last_qty: Mapped[int] = mapped_column(BigInteger)
    volume: Mapped[int] = mapped_column(BigInteger)
    oi: Mapped[int | None] = mapped_column(BigInteger)
    avg_price_paise: Mapped[int | None] = mapped_column(BigInteger)
    buy_qty: Mapped[int | None] = mapped_column(BigInteger)
    sell_qty: Mapped[int | None] = mapped_column(BigInteger)
    open_paise: Mapped[int | None] = mapped_column(BigInteger)
    high_paise: Mapped[int | None] = mapped_column(BigInteger)
    low_paise: Mapped[int | None] = mapped_column(BigInteger)
    # Kite's `close`: the previous trading day's close during the session.
    close_paise: Mapped[int | None] = mapped_column(BigInteger)
    last_trade_ts: Mapped[datetime | None]
    oi_day_high: Mapped[int | None] = mapped_column(BigInteger)
    oi_day_low: Mapped[int | None] = mapped_column(BigInteger)
    bid_price_paise: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    bid_qty: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    bid_orders: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))
    ask_price_paise: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    ask_qty: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    ask_orders: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))


class IndexTick(Base):
    """Recorded index prices (NOVA `index_ticks`, same columns; no foreign key here)."""

    __tablename__ = "index_ticks"
    __table_args__ = (
        CheckConstraint("last_price_paise > 0", name="last_price"),
        *(
            CheckConstraint(f"{name}_paise > 0", name=name)
            for name in ("high", "low", "open", "close")
        ),
    )

    symbol: Mapped[str] = mapped_column(primary_key=True)
    received_at: Mapped[datetime] = mapped_column(primary_key=True)
    exchange_ts: Mapped[datetime | None]
    last_price_paise: Mapped[int] = mapped_column(BigInteger)
    high_paise: Mapped[int | None] = mapped_column(BigInteger)
    low_paise: Mapped[int | None] = mapped_column(BigInteger)
    open_paise: Mapped[int | None] = mapped_column(BigInteger)
    close_paise: Mapped[int | None] = mapped_column(BigInteger)


class SyncState(Base):
    """The one row NOVA's last `PUT /internal/sync` wrote (`id` is always 1)."""

    __tablename__ = "sync_state"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    account_id: Mapped[str]
    api_key: Mapped[str]
    # Still sealed with NOVA_BROKER_TOKEN_KEY; null when NOVA has no live Kite session.
    access_token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime | None]
    enabled: Mapped[bool]
    synced_at: Mapped[datetime]


class RecStock(Base):
    """A stock to record (NSE), as NOVA pushed it."""

    __tablename__ = "rec_stocks"

    symbol: Mapped[str] = mapped_column(primary_key=True)
    instrument_token: Mapped[int] = mapped_column(BigInteger, unique=True)


class RecIndex(Base):
    """An index to record, as NOVA pushed it."""

    __tablename__ = "rec_indices"

    name: Mapped[str] = mapped_column(primary_key=True)
    instrument_token: Mapped[int] = mapped_column(BigInteger)


class Recording(Base):
    """One recording run of the loop; `rows_written` counts stock ticks (not index ticks)."""

    __tablename__ = "recordings"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'completed', 'failed')", name="status"),
        CheckConstraint("rows_written >= 0", name="rows_written"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    status: Mapped[str]
    error: Mapped[str | None]
    rows_written: Mapped[int] = mapped_column(BigInteger, server_default="0")


class RecordedDay(Base):
    """Frozen IST day counts; retained after NOVA requests deletion (R2)."""

    __tablename__ = "recorded_days"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    ticks: Mapped[int] = mapped_column(BigInteger)
    index_ticks: Mapped[int] = mapped_column(BigInteger)
    stocks: Mapped[int] = mapped_column(BigInteger)
    closed_at: Mapped[datetime]
    imported_at: Mapped[datetime | None]
    delete_requested_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]
