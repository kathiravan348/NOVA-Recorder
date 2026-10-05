"""Recorder tables: NOVA's `ticks` and `index_ticks` as hypertables, the pushed sync, recordings.

`ticks` and `index_ticks` have NOVA's final columns, types and primary keys (NOVA rev 0004, 0025,
0030) so a recorded day copies into NOVA as-is (R1 (3)); `index_ticks.symbol` has no foreign key.

Revision ID: 0001
Revises:
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TICK_SCALARS = (
    ("avg_price_paise", sa.BigInteger()),
    ("buy_qty", sa.BigInteger()),
    ("sell_qty", sa.BigInteger()),
    ("open_paise", sa.BigInteger()),
    ("high_paise", sa.BigInteger()),
    ("low_paise", sa.BigInteger()),
    ("close_paise", sa.BigInteger()),
    ("last_trade_ts", sa.DateTime(timezone=True)),
    ("oi_day_high", sa.BigInteger()),
    ("oi_day_low", sa.BigInteger()),
)
TICK_DEPTH = (
    ("bid_price_paise", sa.BigInteger()),
    ("bid_qty", sa.BigInteger()),
    ("bid_orders", sa.Integer()),
    ("ask_price_paise", sa.BigInteger()),
    ("ask_qty", sa.BigInteger()),
    ("ask_orders", sa.Integer()),
)
INDEX_PRICES = ("high", "low", "open", "close")


def hypertable(table: str) -> None:
    op.execute(
        f"SELECT create_hypertable('{table}', by_range('received_at', INTERVAL '1 day'),"
        " create_default_indexes => false)"
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.create_table(
        "ticks",
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exchange_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_price_paise", sa.BigInteger(), nullable=False),
        sa.Column("last_qty", sa.BigInteger(), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("oi", sa.BigInteger(), nullable=True),
        *(sa.Column(name, kind, nullable=True) for name, kind in TICK_SCALARS),
        *(sa.Column(name, ARRAY(item), nullable=True) for name, item in TICK_DEPTH),
        sa.CheckConstraint("exchange IN ('NSE', 'NFO')", name=op.f("ck_ticks_exchange")),
        sa.CheckConstraint(
            "last_price_paise > 0 AND last_qty >= 0 AND volume >= 0", name=op.f("ck_ticks_values")
        ),
        sa.PrimaryKeyConstraint("exchange", "symbol", "received_at", name=op.f("pk_ticks")),
    )
    hypertable("ticks")
    op.create_table(
        "index_ticks",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exchange_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_price_paise", sa.BigInteger(), nullable=False),
        *(sa.Column(f"{name}_paise", sa.BigInteger(), nullable=True) for name in INDEX_PRICES),
        sa.CheckConstraint("last_price_paise > 0", name=op.f("ck_index_ticks_last_price")),
        *(
            sa.CheckConstraint(f"{name}_paise > 0", name=op.f(f"ck_index_ticks_{name}"))
            for name in INDEX_PRICES
        ),
        sa.PrimaryKeyConstraint("symbol", "received_at", name=op.f("pk_index_ticks")),
    )
    hypertable("index_ticks")
    op.create_table(
        "sync_state",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("api_key", sa.Text(), nullable=False),
        sa.Column("access_token_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_sync_state_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_state")),
    )
    op.create_table(
        "rec_stocks",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("instrument_token", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("symbol", name=op.f("pk_rec_stocks")),
        sa.UniqueConstraint("instrument_token", name=op.f("uq_rec_stocks_instrument_token")),
    )
    op.create_table(
        "rec_indices",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("instrument_token", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_rec_indices")),
    )
    op.create_table(
        "recordings",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("rows_written", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')", name=op.f("ck_recordings_status")
        ),
        sa.CheckConstraint("rows_written >= 0", name=op.f("ck_recordings_rows_written")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recordings")),
    )


def downgrade() -> None:
    for table in ("recordings", "rec_indices", "rec_stocks", "sync_state", "index_ticks", "ticks"):
        op.drop_table(table)
