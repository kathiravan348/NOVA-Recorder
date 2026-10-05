"""Infrastructure settings, read only from the environment (`REC_*`, plus NOVA's token key)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["debug", "info", "warning", "error"]


class Settings(BaseSettings):
    # Empty variables (Compose passes "" for unset optional values) count as unset.
    model_config = SettingsConfigDict(
        env_prefix="REC_", extra="forbid", frozen=True, env_ignore_empty=True
    )

    database_url: SecretStr
    # NOVA sends it in the `x-rec-internal-token` header.
    internal_token: SecretStr
    # Same value as NOVA's `.env`: opens the sealed Kite token in memory only (R1 (4)).
    broker_token_key: SecretStr | None = Field(default=None, alias="NOVA_BROKER_TOKEN_KEY")
    log_level: LogLevel = "info"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
