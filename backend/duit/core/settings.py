"""App settings, read once from environment variables prefixed DUIT_ (or backend/.env in dev)."""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DUIT_", env_file=".env")

    environment: Literal["dev", "staging", "prod"] = "dev"
    # postgresql+asyncpg://user:password@host:5432/db. SecretStr: never printed in logs or reprs.
    database_url: SecretStr


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]  # values come from the environment
