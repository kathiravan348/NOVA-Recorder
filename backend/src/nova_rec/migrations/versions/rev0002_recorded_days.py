"""Frozen recorded days and manual deletion state.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recorded_days",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("ticks", sa.BigInteger(), nullable=False),
        sa.Column("index_ticks", sa.BigInteger(), nullable=False),
        sa.Column("stocks", sa.BigInteger(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True)),
        sa.Column("delete_requested_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("recorded_days")
