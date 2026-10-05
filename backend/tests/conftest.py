"""Shared fixtures: a throwaway database on `rec-test-db`, settings, a clock and sync helpers.

`REC_TEST_DATABASE_URL` points at `rec-test-db`'s maintenance database; `rec-check` sets it.
Without it (quick host runs) the database tests are skipped. Tests never call Kite or NOVA.
"""

import base64
import os
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from nova_rec.crypto import TokenCipher, new_key
from nova_rec.db import upgrade
from nova_rec.models import Base
from nova_rec.settings import Settings

TOKEN = "internal-test-token"
API_KEY = "kitekeyAB12"
ACCESS_TOKEN = "kite-access-token"
# Friday 25 Sep 2026, 10:00 IST.
FRIDAY_10_IST = datetime(2026, 9, 25, 4, 30, tzinfo=UTC)


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    admin_url = os.environ.get("REC_TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("REC_TEST_DATABASE_URL is not set (database tests run in rec-check)")
    name = f"rec_test_{secrets.token_hex(4)}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def migrated(database_url: str) -> Iterator[Engine]:
    upgrade(database_url)
    db_engine = create_engine(database_url)
    yield db_engine
    db_engine.dispose()


@pytest.fixture
def engine(migrated: Engine) -> Engine:
    """The migrated database, emptied before each test."""
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    with migrated.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY"))
    return migrated


@pytest.fixture
def key() -> str:
    return new_key()


@pytest.fixture
def settings(database_url: str, key: str) -> Settings:
    return Settings.model_validate(
        {"database_url": database_url, "internal_token": TOKEN, "NOVA_BROKER_TOKEN_KEY": key}
    )


def sealed_token(key: str, token: str = ACCESS_TOKEN) -> str:
    """The Kite token as NOVA sends it: sealed with the shared key, base64."""
    return base64.b64encode(TokenCipher(key).encrypt(token)).decode("ascii")


def sync_body(key: str, **overrides: object) -> dict[str, object]:
    """A valid `PUT /internal/sync` body: INFY and TCS, NIFTY 50, a session live for a day."""
    body: dict[str, object] = {
        "accountId": "acc_owner",
        "apiKey": API_KEY,
        "accessTokenSealed": sealed_token(key),
        "expiresAt": (FRIDAY_10_IST + timedelta(days=1)).isoformat(),
        "enabled": True,
        "stocks": [
            {"symbol": "INFY", "instrumentToken": 408065},
            {"symbol": "TCS", "instrumentToken": 2953217},
        ],
        "indices": [{"name": "NIFTY 50", "instrumentToken": 256265}],
    }
    return body | overrides
