"""The ledger: accounts, categories and transactions.

Tenant isolation is enforced by the database, not only by queries. Every table carries
`user_id`, and references between ledger tables are composite foreign keys on
(user_id, <id>) -> (user_id, id). A row can therefore only point at rows of the same user:
even a query that forgets `WHERE user_id = ...` cannot link one user's transaction to another
user's account or category.
"""

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from duit.core.db import Base

# ISO 4217 code, e.g. MYR. Checked in the database so a typo ("myr", "RM") never gets stored.
CURRENCY_CHECK = "~ '^[A-Z]{3}$'"


class AccountType(enum.StrEnum):
    BANK = "bank"
    CARD = "card"
    EWALLET = "ewallet"
    CASH = "cash"
    EPF = "epf"
    INVESTMENT = "investment"


class RuleMatch(enum.StrEnum):
    MERCHANT = "merchant"  # the merchant key equals the pattern (learned from corrections)
    CONTAINS = "contains"  # the merchant key contains the pattern (written by the user)


class RuleSource(enum.StrEnum):
    USER = "user"
    LEARNED = "learned"


class TransactionStatus(enum.StrEnum):
    PENDING = "pending"  # e.g. from an alert email, not yet on a statement
    CONFIRMED = "confirmed"


def text_enum(cls: type[enum.StrEnum], name: str) -> Enum:
    """Store an enum as text plus a CHECK constraint, not a native Postgres ENUM type: adding
    or removing a value later is a plain constraint swap in a migration."""

    def values(_: object) -> list[str]:  # store "bank", not the member name "BANK"
        return [m.value for m in cls]

    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=16,
        values_callable=values,
    )


class Account(Base):
    """One row per real-world account: a bank account, card, e-wallet, cash, EPF..."""

    __tablename__ = "accounts"
    __table_args__ = (
        # Target of the composite foreign keys from transactions (see module docstring).
        UniqueConstraint("user_id", "id"),
        UniqueConstraint("user_id", "name"),
        CheckConstraint(f"currency {CURRENCY_CHECK}", name="currency_iso"),
        CheckConstraint("account_last4 ~ '^[0-9]{4}$'", name="last4_digits"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    type: Mapped[AccountType] = mapped_column(text_enum(AccountType, "account_type"))
    institution: Mapped[str | None]  # e.g. "Maybank"
    name: Mapped[str]  # the user's label, e.g. "Maybank savings"
    currency: Mapped[str] = mapped_column(server_default="MYR")
    # Shown in the UI to tell accounts apart; the full number is only stored encrypted.
    account_last4: Mapped[str | None]
    # AES-GCM with the data key (duit.core.crypto), associated data bound to this row's id.
    account_number_enc: Mapped[bytes | None]
    statement_password_enc: Mapped[bytes | None]  # to open password-protected PDF statements
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Category(Base):
    """Two-level categories (e.g. Transport > Fuel). Uncategorised = category_id IS NULL."""

    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("user_id", "id"),
        # Names are unique among siblings. NULLS NOT DISTINCT (Postgres 15+) makes this hold
        # for top-level categories too, where parent_id is NULL.
        UniqueConstraint("user_id", "parent_id", "name", postgresql_nulls_not_distinct=True),
        # The parent must belong to the same user. Deleting a parent that still has children
        # fails; move or delete the children first.
        ForeignKeyConstraint(["user_id", "parent_id"], ["categories.user_id", "categories.id"]),
        CheckConstraint("parent_id <> id", name="not_own_parent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    parent_id: Mapped[uuid.UUID | None]
    name: Mapped[str]
    is_income: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CategoryRule(Base):
    """Categorisation rules: "a transaction whose merchant key is / contains PATTERN belongs in
    CATEGORY". Patterns are merchant keys too (see Transaction.merchant_key), so "Grab*" and
    "GRAB" are the same rule. Deleting the category deletes its rules."""

    __tablename__ = "category_rules"
    __table_args__ = (
        UniqueConstraint("user_id", "id"),
        UniqueConstraint("user_id", "match_type", "pattern"),  # one category per pattern
        ForeignKeyConstraint(
            ["user_id", "category_id"],
            ["categories.user_id", "categories.id"],
            ondelete="CASCADE",
        ),
        # The database keeps patterns normalised, whoever writes them.
        CheckConstraint("pattern <> '' AND pattern = merchant_key(pattern)", name="normalised"),
        # A one- or two-letter "contains" would match almost everything.
        CheckConstraint("match_type <> 'contains' OR length(pattern) >= 3", name="contains_min"),
    )
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    match_type: Mapped[RuleMatch] = mapped_column(text_enum(RuleMatch, "rule_match"))
    pattern: Mapped[str]
    category_id: Mapped[uuid.UUID]
    source: Mapped[RuleSource] = mapped_column(text_enum(RuleSource, "rule_source"))
    hit_count: Mapped[int] = mapped_column(server_default=text("0"))  # transactions it categorised
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Transaction(Base):
    """The ledger. amount_sen is signed: negative = money out, positive = money in."""

    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("user_id", "id"),  # target of transaction_sources' composite key
        # The account and category must belong to the same user as the transaction.
        # Deleting an account that still has transactions fails (NO ACTION), so ledger rows
        # never vanish by accident. NO ACTION rather than RESTRICT: the check runs at the end
        # of the statement, so deleting a user (which cascades to both tables) still works.
        ForeignKeyConstraint(["user_id", "account_id"], ["accounts.user_id", "accounts.id"]),
        # Deleting a category un-categorises its transactions. The column list (Postgres 15+)
        # nulls only category_id; a plain SET NULL would null user_id too and fail.
        ForeignKeyConstraint(
            ["user_id", "category_id"],
            ["categories.user_id", "categories.id"],
            ondelete="SET NULL (category_id)",
        ),
        # Re-importing the same statement line is a no-op: one dedupe_key per account.
        UniqueConstraint("account_id", "dedupe_key"),
        CheckConstraint(f"currency {CURRENCY_CHECK}", name="currency_iso"),
        CheckConstraint("fx_rate > 0", name="fx_rate_positive"),
        # The transaction list: newest first, per user.
        Index("ix_transactions_user_posted", "user_id", text("posted_on DESC"), text("id DESC")),
        Index("ix_transactions_user_category", "user_id", "category_id"),
        # Which rule set the category; NULL with a category = the user set it, and rules never
        # touch it again. Deleting a category deletes its rules: SET NULL (category_rule_id)
        # here, and the category FK above clears category_id in the same statement.
        # Checked at commit (DEFERRED): both cascades update the same row, and checked at once,
        # the category's update would still see the just-deleted rule's id.
        ForeignKeyConstraint(
            ["user_id", "category_rule_id"],
            ["category_rules.user_id", "category_rules.id"],
            ondelete="SET NULL (category_rule_id)",
            deferrable=True,
            initially="DEFERRED",
        ),
        # Re-categorising every transaction of one merchant after a correction.
        Index("ix_transactions_user_merchant_key", "user_id", "merchant_key"),
    )
    # Read server-generated values (updated_at) back with RETURNING on UPDATE too, so responses
    # built after commit never trigger a lazy load.
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    account_id: Mapped[uuid.UUID]
    # The bank's booking date. A date, not a timestamp: statements only give a date, and
    # date-only values stored as UTC midnight land on the wrong day (or month) in UTC+8.
    posted_on: Mapped[date]
    amount_sen: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str]
    # Account currency per one unit of `currency`, when they differ (a USD charge on a MYR card).
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    merchant_raw: Mapped[str]  # the description exactly as the source wrote it
    # merchant_raw normalised by the SQL function merchant_key() (uppercase letters, single
    # spaces; digits and punctuation dropped): "POS DEBIT / KK SUPER MART / 31/10/2026 2359"
    # (three lines) -> "POS DEBIT KK SUPER MART". Swipe dates and times vary, the merchant does not.
    merchant_key: Mapped[str] = mapped_column(
        Computed("merchant_key(merchant_raw)", persisted=True)  # STORED: indexable
    )
    category_id: Mapped[uuid.UUID | None]
    category_rule_id: Mapped[uuid.UUID | None]
    status: Mapped[TransactionStatus] = mapped_column(
        text_enum(TransactionStatus, "transaction_status"), server_default="confirmed"
    )
    # Stable fingerprint of the source line (set by importers; NULL for manual entries).
    dedupe_key: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
