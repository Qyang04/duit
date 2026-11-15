"""App settings, read once from environment variables prefixed DUIT_ (or backend/.env in dev)."""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DUIT_", env_file=".env")

    environment: Literal["dev", "staging", "prod"] = "dev"
    # postgresql://user:password@host:5432/db (+asyncpg is added if missing).
    # SecretStr: never printed in logs or reprs.
    database_url: SecretStr
    # 32 random bytes, base64: encrypts sensitive columns (duit.core.crypto). Optional here so
    # the migration Job can run without it; the API refuses to start without it.
    data_key: SecretStr | None = None
    # Anyone may register while this is true. When false, only the very first account can be
    # created (claim your own instance right after deploying it); later sign-ups get 403.
    registration_open: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]  # values come from the environment
