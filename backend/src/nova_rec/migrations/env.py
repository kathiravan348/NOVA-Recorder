"""Alembic environment: online migrations only, one transaction per run.

The URL comes from `nova_rec.db.alembic_config` (`python -m nova_rec migrate`, tests), or from
`REC_DATABASE_URL` when Alembic is run directly with `backend/alembic.ini`.
"""

import os

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from nova_rec.models import Base

url = context.config.get_main_option("sqlalchemy.url") or os.environ.get("REC_DATABASE_URL")
if not url:
    raise RuntimeError("No database URL: set REC_DATABASE_URL or use `python -m nova_rec migrate`")

engine = create_engine(url, poolclass=NullPool)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
