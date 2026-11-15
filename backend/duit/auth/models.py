"""Users and server-side sessions."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, func, text
from sqlalchemy.orm import Mapped, mapped_column

from duit.core.db import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Emails are stored lowercase, so the UNIQUE constraint is effectively case-insensitive.
        CheckConstraint("email = lower(email)", name="email_lowercase"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    email: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]  # argon2id, parameters embedded in the string
    # TOTP secret, AES-GCM encrypted with the app's data key (duit.core.crypto).
    # Set by /auth/totp/setup; 2FA is only enforced once confirmed with a valid code.
    totp_secret_enc: Mapped[bytes | None]
    totp_confirmed_at: Mapped[datetime | None]
    # Last accepted 30-second time step: a code cannot be replayed within its validity window.
    totp_last_step: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    @property
    def totp_enabled(self) -> bool:
        return self.totp_confirmed_at is not None


class UserSession(Base):
    """One row per logged-in browser. Deleting the row logs that browser out immediately."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # SHA-256 of the random cookie token. The token itself is never stored, so a database
    # leak does not hand out working session cookies.
    token_hash: Mapped[bytes] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    user_agent: Mapped[str | None]
