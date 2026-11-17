"""App settings, read once from environment variables prefixed DUIT_ (or backend/.env in dev)."""

from dataclasses import dataclass
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

    # The mailbox that receives bank and e-wallet alert emails (polled by the worker over
    # IMAPS). Polling is off until host, username, password and owner are all set.
    imap_host: str | None = None
    imap_port: int = 993
    imap_username: str | None = None
    imap_password: SecretStr | None = None  # an app password, never the account's own
    imap_folder: str = "INBOX"
    imap_owner: str | None = None  # email of the Duit user the alerts belong to

    def imap(self) -> "ImapConfig | None":
        if not (self.imap_host and self.imap_username and self.imap_password and self.imap_owner):
            return None
        return ImapConfig(
            host=self.imap_host,
            port=self.imap_port,
            username=self.imap_username,
            password=self.imap_password,
            folder=self.imap_folder,
            owner=self.imap_owner.lower(),
        )


@dataclass(frozen=True)
class ImapConfig:
    host: str
    port: int
    username: str
    password: SecretStr
    folder: str
    owner: str

    @property
    def key(self) -> str:
        """Names this mailbox in imap_cursors. A new host, account or folder starts over."""
        return f"{self.username}@{self.host}/{self.folder}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]  # values come from the environment
