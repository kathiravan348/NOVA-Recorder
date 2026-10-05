"""Engine, session factory and migrations (sync SQLAlchemy 2 + psycopg 3, Alembic)."""

from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from nova_rec.models import Base

# Two long-running processes (recorder, API) with a few connections each.
POOL_SIZE = 2
MAX_OVERFLOW = 4


def create_db_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, pool_size=POOL_SIZE, max_overflow=MAX_OVERFLOW)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", "nova_rec:migrations")
    # ConfigParser treats % as interpolation; URL-encoded passwords contain it.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def diff(database_url: str) -> list[Any]:
    """Differences between the models and a migrated database; empty when they match.

    Any: Alembic returns loosely typed diff tuples.
    """
    engine = create_engine(database_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            return list(compare_metadata(context, Base.metadata))
    finally:
        engine.dispose()
