"""App settings, read once from environment variables prefixed DUIT_."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DUIT_")

    environment: Literal["dev", "staging", "prod"] = "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
