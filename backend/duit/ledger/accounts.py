"""/accounts: the user's real-world accounts (bank, card, e-wallet, cash, EPF, investment)."""

import re
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import exists, select

from duit.auth.models import User
from duit.auth.router import CurrentUser
from duit.core.crypto import encrypt, load_data_key
from duit.core.deps import AppSettings, Db
from duit.core.settings import Settings
from duit.ledger.common import Currency, Input, Name, Patch, flush_or_conflict, not_found
from duit.ledger.models import Account, AccountType, Transaction

router = APIRouter(prefix="/accounts", tags=["accounts"])

DUPLICATE_NAME = "You already have an account with this name"

# Spaces and hyphens are allowed as typed ("1234-5678 9012"); only the digits are stored.
AccountNumber = Annotated[str, Field(pattern=r"^[0-9 -]{4,40}$")]
StatementPassword = Annotated[SecretStr, Field(min_length=1, max_length=128)]


class AccountIn(Input):
    type: AccountType
    name: Name
    institution: Name | None = None
    currency: Currency = "MYR"
    # Write-only: stored encrypted, never returned (only the last 4 digits are).
    account_number: AccountNumber | None = None
    statement_password: StatementPassword | None = None


class AccountUpdate(Patch):
    required = ("type", "name", "currency")

    type: AccountType | None = None
    name: Name | None = None
    institution: Name | None = None
    currency: Currency | None = None
    account_number: AccountNumber | None = None  # null removes it
    statement_password: StatementPassword | None = None  # null removes it


class AccountOut(BaseModel):
    id: uuid.UUID
    type: AccountType
    name: str
    institution: str | None
    currency: str
    account_last4: str | None
    has_account_number: bool
    has_statement_password: bool
    created_at: datetime

    @classmethod
    def of(cls, a: Account) -> "AccountOut":
        return cls(
            id=a.id,
            type=a.type,
            name=a.name,
            institution=a.institution,
            currency=a.currency,
            account_last4=a.account_last4,
            has_account_number=a.account_number_enc is not None,
            has_statement_password=a.statement_password_enc is not None,
            created_at=a.created_at,
        )


def secret_aad(field: str, account_id: uuid.UUID) -> bytes:
    """Binds an encrypted value to its column and row (see duit.core.crypto): a ciphertext
    copied to another account or column fails to decrypt."""
    return f"account.{field}:".encode() + account_id.bytes


def set_account_number(account: Account, number: str | None, settings: Settings) -> None:
    if number is None:
        account.account_number_enc = account.account_last4 = None
        return
    digits = re.sub(r"\D", "", number)
    if len(digits) < 4:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Account number needs at least 4 digits"
        )
    key = load_data_key(settings)
    account.account_number_enc = encrypt(
        key, digits.encode(), secret_aad("account_number", account.id)
    )
    account.account_last4 = digits[-4:]


def set_statement_password(account: Account, pw: SecretStr | None, settings: Settings) -> None:
    if pw is None:
        account.statement_password_enc = None
        return
    account.statement_password_enc = encrypt(
        load_data_key(settings),
        pw.get_secret_value().encode(),
        secret_aad("statement_password", account.id),
    )


async def find_account(db: Db, user: User, account_id: uuid.UUID) -> Account | None:
    """The account, only if it belongs to this user. Every lookup goes through here."""
    return await db.scalar(
        select(Account).where(Account.id == account_id, Account.user_id == user.id)
    )


async def get_account(db: Db, user: User, account_id: uuid.UUID) -> Account:
    account = await find_account(db, user, account_id)
    if account is None:
        raise not_found("Account")
    return account


async def has_transactions(db: Db, account: Account) -> bool:
    return bool(await db.scalar(select(exists().where(Transaction.account_id == account.id))))


@router.get("")
async def list_accounts(user: CurrentUser, db: Db) -> list[AccountOut]:
    accounts = await db.scalars(
        select(Account).where(Account.user_id == user.id).order_by(Account.name)
    )
    return [AccountOut.of(a) for a in accounts]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_account(
    body: AccountIn, user: CurrentUser, db: Db, settings: AppSettings
) -> AccountOut:
    account = Account(
        user_id=user.id,
        type=body.type,
        name=body.name,
        institution=body.institution,
        currency=body.currency,
    )
    db.add(account)
    # Flush first: the encryption is bound to the row id, which Postgres generates (uuidv7).
    await flush_or_conflict(db, "uq_accounts_user_id_name", DUPLICATE_NAME)
    set_account_number(account, body.account_number, settings)
    set_statement_password(account, body.statement_password, settings)
    await db.commit()
    return AccountOut.of(account)


@router.get("/{account_id}")
async def read_account(account_id: uuid.UUID, user: CurrentUser, db: Db) -> AccountOut:
    return AccountOut.of(await get_account(db, user, account_id))


@router.patch("/{account_id}")
async def update_account(
    account_id: uuid.UUID, body: AccountUpdate, user: CurrentUser, db: Db, settings: AppSettings
) -> AccountOut:
    account = await get_account(db, user, account_id)
    changes = body.changes()
    # Checks first, changes after: a rejected request must not leave a modified object behind.
    if changes.get("currency", account.currency) != account.currency and await has_transactions(
        db, account
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Cannot change the currency of an account with transactions"
        )
    if "account_number" in changes:
        set_account_number(account, changes.pop("account_number"), settings)
    if "statement_password" in changes:
        set_statement_password(account, changes.pop("statement_password"), settings)
    for name, value in changes.items():
        setattr(account, name, value)
    await flush_or_conflict(db, "uq_accounts_user_id_name", DUPLICATE_NAME)
    await db.commit()
    return AccountOut.of(account)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(account_id: uuid.UUID, user: CurrentUser, db: Db) -> None:
    account = await get_account(db, user, account_id)
    if await has_transactions(db, account):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Account has transactions; delete or move them first"
        )
    await db.delete(account)
    await db.commit()
