"""/transactions: the ledger. Amounts are signed integer sen (negative = money out).

The list uses keyset (cursor) pagination on (posted_on, id), newest first. Unlike OFFSET, a page
costs the same however deep you scroll (it is an index range scan, not "skip N rows"), and rows
added while you page never shift items between pages, so nothing is shown twice or skipped.
"""

import base64
import binascii
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, tuple_

from duit.auth.models import User
from duit.auth.router import CurrentUser
from duit.core.deps import Db
from duit.ledger.accounts import find_account
from duit.ledger.categories import find_category
from duit.ledger.common import Currency, Input, Patch, not_found, unknown
from duit.ledger.models import Category, Transaction, TransactionStatus

router = APIRouter(prefix="/transactions", tags=["transactions"])

MAX_SEN = 10**15  # RM 10 trillion: rejects absurd values long before bigint overflows
AmountSen = Annotated[int, Field(ge=-MAX_SEN, le=MAX_SEN, examples=[-1250])]
FxRate = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=8)]
Merchant = Annotated[str, Field(min_length=1, max_length=500)]


class TransactionIn(Input):
    account_id: uuid.UUID
    posted_on: date
    amount_sen: AmountSen
    merchant_raw: Merchant
    currency: Currency | None = None  # default: the account's currency
    fx_rate: FxRate | None = None  # required when currency differs from the account's
    category_id: uuid.UUID | None = None
    status: TransactionStatus = TransactionStatus.CONFIRMED


class TransactionUpdate(Patch):
    required = ("account_id", "posted_on", "amount_sen", "merchant_raw", "currency", "status")

    account_id: uuid.UUID | None = None
    posted_on: date | None = None
    amount_sen: AmountSen | None = None
    merchant_raw: Merchant | None = None
    currency: Currency | None = None
    fx_rate: FxRate | None = None
    category_id: uuid.UUID | None = None  # null = uncategorised
    status: TransactionStatus | None = None


class TransactionOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    posted_on: date
    amount_sen: int
    currency: str
    fx_rate: Decimal | None
    merchant_raw: str
    category_id: uuid.UUID | None
    status: TransactionStatus
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, t: Transaction) -> "TransactionOut":
        return cls(
            id=t.id,
            account_id=t.account_id,
            posted_on=t.posted_on,
            amount_sen=t.amount_sen,
            currency=t.currency,
            fx_rate=t.fx_rate,
            merchant_raw=t.merchant_raw,
            category_id=t.category_id,
            status=t.status,
            created_at=t.created_at,
            updated_at=t.updated_at,
        )


class TransactionPage(BaseModel):
    items: list[TransactionOut]
    next_cursor: str | None  # pass back as ?cursor= for the next page; null on the last page


# Fields check_references needs, on create and (merged with the row) on update.
CHECKED = ("account_id", "category_id", "currency", "fx_rate")


# --- helpers ---------------------------------------------------------------------------------


def invalid(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


def encode_cursor(t: Transaction) -> str:
    return base64.urlsafe_b64encode(f"{t.posted_on.isoformat()}|{t.id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[date, uuid.UUID]:
    """Opaque to clients, so the encoding can change without breaking them."""
    try:
        posted_on, txn_id = base64.urlsafe_b64decode(cursor).decode().split("|")
        return date.fromisoformat(posted_on), uuid.UUID(txn_id)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise invalid("Invalid cursor") from exc


def escape_like(text: str) -> str:
    """Search for the user's text literally: % and _ are LIKE wildcards otherwise."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def check_references(db: Db, user: User, fields: dict[str, Any]) -> str:
    """Validates the would-be row before anything is changed, so a rejected request leaves no
    half-edited object behind. The account and category must be the user's own (the composite
    foreign keys would also reject another user's ids, but as a 500); a foreign currency needs
    an FX rate. Returns the currency, which defaults to the account's."""
    account = await find_account(db, user, fields["account_id"])
    if account is None:
        raise unknown("account")
    category_id = fields["category_id"]
    if category_id is not None and await find_category(db, user, category_id) is None:
        raise unknown("category")
    currency: str = fields["currency"] or account.currency
    if currency != account.currency and fields["fx_rate"] is None:
        raise invalid(f"fx_rate is required: the account's currency is {account.currency}")
    if currency == account.currency and fields["fx_rate"] is not None:
        raise invalid("fx_rate is only for transactions in a foreign currency")
    return currency


async def get_transaction(db: Db, user: User, txn_id: uuid.UUID) -> Transaction:
    txn = await db.scalar(
        select(Transaction).where(Transaction.id == txn_id, Transaction.user_id == user.id)
    )
    if txn is None:
        raise not_found("Transaction")
    return txn


# --- endpoints -------------------------------------------------------------------------------


@router.get("")
async def list_transactions(
    user: CurrentUser,
    db: Db,
    account_id: uuid.UUID | None = None,
    category_id: Annotated[
        uuid.UUID | None, Query(description="A parent category includes its subcategories")
    ] = None,
    uncategorised: bool = False,
    date_from: date | None = None,
    date_to: date | None = None,
    q: Annotated[str | None, Query(max_length=100, description="Merchant contains")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> TransactionPage:
    stmt = select(Transaction).where(Transaction.user_id == user.id)
    if account_id is not None:
        stmt = stmt.where(Transaction.account_id == account_id)
    if category_id is not None:
        in_tree = select(Category.id).where(
            Category.user_id == user.id,
            or_(Category.id == category_id, Category.parent_id == category_id),
        )
        stmt = stmt.where(Transaction.category_id.in_(in_tree))
    if uncategorised:
        stmt = stmt.where(Transaction.category_id.is_(None))
    if date_from is not None:
        stmt = stmt.where(Transaction.posted_on >= date_from)
    if date_to is not None:
        stmt = stmt.where(Transaction.posted_on <= date_to)
    if q:
        stmt = stmt.where(Transaction.merchant_raw.ilike(f"%{escape_like(q)}%", escape="\\"))
    if cursor is not None:
        after_date, after_id = decode_cursor(cursor)
        stmt = stmt.where(tuple_(Transaction.posted_on, Transaction.id) < (after_date, after_id))

    # One extra row tells us whether there is a next page, without a COUNT query.
    stmt = stmt.order_by(Transaction.posted_on.desc(), Transaction.id.desc()).limit(limit + 1)
    rows = list(await db.scalars(stmt))
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1]) if len(rows) > limit else None
    return TransactionPage(items=[TransactionOut.of(t) for t in page], next_cursor=next_cursor)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_transaction(body: TransactionIn, user: CurrentUser, db: Db) -> TransactionOut:
    fields = body.model_dump()
    fields["currency"] = await check_references(db, user, fields)
    txn = Transaction(user_id=user.id, **fields)
    db.add(txn)
    await db.commit()
    return TransactionOut.of(txn)


@router.get("/{txn_id}")
async def read_transaction(txn_id: uuid.UUID, user: CurrentUser, db: Db) -> TransactionOut:
    return TransactionOut.of(await get_transaction(db, user, txn_id))


@router.patch("/{txn_id}")
async def update_transaction(
    txn_id: uuid.UUID, body: TransactionUpdate, user: CurrentUser, db: Db
) -> TransactionOut:
    txn = await get_transaction(db, user, txn_id)
    changes: dict[str, Any] = body.changes()
    merged = {name: changes.get(name, getattr(txn, name)) for name in CHECKED}
    changes["currency"] = await check_references(db, user, merged)
    for name, value in changes.items():
        setattr(txn, name, value)
    await db.commit()
    return TransactionOut.of(txn)


@router.delete("/{txn_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_transaction(txn_id: uuid.UUID, user: CurrentUser, db: Db) -> None:
    await db.delete(await get_transaction(db, user, txn_id))
    await db.commit()
