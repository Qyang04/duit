"""/accounts/{id}/statements: import a bank statement PDF into an account.

Two steps, both taking the same PDF:
- preview: parse and show every line, flagging those already in the ledger. Saves nothing.
- import: parse again on the server and insert the new lines.

Import re-parses instead of accepting rows back from the client, so the ledger only ever holds
what the server itself read from the bank's document. Duplicates are skipped by the database
(ON CONFLICT on the per-account dedupe_key), so importing the same or an overlapping statement
twice, even concurrently, adds every transaction exactly once.
"""

import enum
import uuid
from collections.abc import Callable
from datetime import date
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from duit.auth.router import CurrentUser
from duit.core.crypto import decrypt, load_data_key
from duit.core.deps import AppSettings, Db
from duit.core.settings import Settings
from duit.ledger.accounts import get_account, secret_aad
from duit.ledger.models import Account, Transaction
from duit.parsers.statements.base import (
    Statement,
    StatementError,
    StatementPasswordError,
    Word,
    dedupe_keys,
    read_pdf,
)
from duit.parsers.statements.cimb import parse_cimb

router = APIRouter(prefix="/accounts/{account_id}/statements", tags=["statements"])

MAX_BYTES = 10 * 1024 * 1024


class Bank(enum.StrEnum):
    CIMB = "cimb"


PARSERS: dict[Bank, Callable[[list[list[Word]]], Statement]] = {Bank.CIMB: parse_cimb}
CURRENCY: dict[Bank, str] = {Bank.CIMB: "MYR"}

PdfFile = Annotated[UploadFile, File(description="The statement PDF")]
Password = Annotated[
    str | None,
    Form(max_length=128, description="Only if the account has no stored statement password"),
]


class PreviewLine(BaseModel):
    posted_on: date
    description: str
    amount_sen: int
    balance_sen: int
    duplicate: bool  # already in the ledger: import will skip it


class Preview(BaseModel):
    opening_balance_sen: int
    closing_balance_sen: int
    new_count: int
    duplicate_count: int
    lines: list[PreviewLine]


class ImportResult(BaseModel):
    imported: int
    skipped_duplicates: int


def unprocessable(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


def stored_password(account: Account, settings: Settings) -> str | None:
    if account.statement_password_enc is None:
        return None
    aad = secret_aad("statement_password", account.id)
    return decrypt(load_data_key(settings), account.statement_password_enc, aad).decode()


async def parse_upload(
    account: Account, bank: Bank, file: UploadFile, password: str | None, settings: Settings
) -> tuple[Statement, list[str]]:
    if account.currency != CURRENCY[bank]:
        raise unprocessable(f"A {bank.value} statement is in {CURRENCY[bank]}")
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Statement larger than 10 MB")
    if not data.startswith(b"%PDF-"):
        raise unprocessable("Not a PDF")
    if password is None:
        password = stored_password(account, settings)

    def parse() -> Statement:
        return PARSERS[bank](read_pdf(data, password))

    try:
        # PDF parsing is CPU-bound: in the event loop it would stall every other request.
        statement = await run_in_threadpool(parse)
    except StatementPasswordError as exc:
        raise unprocessable(
            "Wrong or missing PDF password: send it, or store it on the account"
        ) from exc
    except StatementError as exc:
        raise unprocessable(str(exc)) from exc  # messages never contain amounts or names
    return statement, dedupe_keys(account.id, statement.lines)


async def known_keys(db: Db, account: Account, keys: list[str]) -> set[str]:
    if not keys:
        return set()
    found = await db.scalars(
        select(Transaction.dedupe_key).where(
            Transaction.account_id == account.id, Transaction.dedupe_key.in_(keys)
        )
    )
    return {k for k in found if k is not None}


@router.post("/preview")
async def preview_statement(
    account_id: uuid.UUID,
    file: PdfFile,
    user: CurrentUser,
    db: Db,
    settings: AppSettings,
    password: Password = None,
    bank: Bank = Bank.CIMB,
) -> Preview:
    account = await get_account(db, user, account_id)
    statement, keys = await parse_upload(account, bank, file, password, settings)
    known = await known_keys(db, account, keys)
    lines = [
        PreviewLine(
            posted_on=line.posted_on,
            description=line.description,
            amount_sen=line.amount_sen,
            balance_sen=line.balance_sen,
            duplicate=key in known,
        )
        for line, key in zip(statement.lines, keys, strict=True)
    ]
    duplicates = sum(line.duplicate for line in lines)
    return Preview(
        opening_balance_sen=statement.opening_balance_sen,
        closing_balance_sen=statement.closing_balance_sen,
        new_count=len(lines) - duplicates,
        duplicate_count=duplicates,
        lines=lines,
    )


@router.post("/import")
async def import_statement(
    account_id: uuid.UUID,
    file: PdfFile,
    user: CurrentUser,
    db: Db,
    settings: AppSettings,
    password: Password = None,
    bank: Bank = Bank.CIMB,
) -> ImportResult:
    account = await get_account(db, user, account_id)
    statement, keys = await parse_upload(account, bank, file, password, settings)
    if not statement.lines:
        return ImportResult(imported=0, skipped_duplicates=0)
    rows = [
        {
            "user_id": user.id,
            "account_id": account.id,
            "posted_on": line.posted_on,
            "amount_sen": line.amount_sen,
            "currency": account.currency,
            "merchant_raw": line.description,
            "dedupe_key": key,
        }
        for line, key in zip(statement.lines, keys, strict=True)
    ]
    inserted = await db.scalars(
        insert(Transaction)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_transactions_account_id_dedupe_key")
        .returning(Transaction.id)
    )
    imported = len(inserted.all())
    await db.commit()
    return ImportResult(imported=imported, skipped_duplicates=len(rows) - imported)
