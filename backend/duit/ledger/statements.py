"""/accounts/{id}/statements: import a bank statement PDF into an account.

Two steps, both taking the same PDF:
- preview: parse and show every line, flagging those already in the ledger. Saves nothing.
- import: parse again on the server and insert the new lines.

Import re-parses instead of accepting rows back from the client, so the ledger only ever holds
what the server itself read from the bank's document. Duplicates are skipped by the database
(ON CONFLICT on the per-account dedupe_key), so importing the same or an overlapping statement
twice, even concurrently, adds every transaction exactly once.

Import also keeps the evidence: the PDF as received (raw_documents, still password-protected),
what the parser read (extractions), and a link from every transaction on the statement, new or
already in the ledger, to that extraction (transaction_sources).
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
from duit.ingestion.documents import link_sources, record_extraction, set_status, store_document
from duit.ingestion.models import DocumentSource, DocumentStatus
from duit.ledger.accounts import get_account, secret_aad
from duit.ledger.models import Account, Transaction
from duit.ledger.rules import apply_rules
from duit.parsers.statements import cimb
from duit.parsers.statements.base import (
    Statement,
    StatementError,
    StatementPasswordError,
    Word,
    dedupe_keys,
    read_pdf,
    statement_fields,
)

router = APIRouter(prefix="/accounts/{account_id}/statements", tags=["statements"])

MAX_BYTES = 10 * 1024 * 1024


class Bank(enum.StrEnum):
    CIMB = "cimb"


PARSERS: dict[Bank, Callable[[list[list[Word]]], Statement]] = {Bank.CIMB: cimb.parse_cimb}
VERSIONS: dict[Bank, str] = {Bank.CIMB: cimb.PARSER_VERSION}
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
    document_id: uuid.UUID  # the stored PDF (GET /documents/{id}/content)
    imported: int
    skipped_duplicates: int
    categorised: int  # of the imported, how many a rule categorised


def unprocessable(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


def stored_password(account: Account, settings: Settings) -> str | None:
    if account.statement_password_enc is None:
        return None
    aad = secret_aad("statement_password", account.id)
    return decrypt(load_data_key(settings), account.statement_password_enc, aad).decode()


async def parse_upload(
    account: Account, bank: Bank, file: UploadFile, password: str | None, settings: Settings
) -> tuple[Statement, list[str], bytes]:
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
    return statement, dedupe_keys(account.id, statement.lines), data


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
    statement, keys, _ = await parse_upload(account, bank, file, password, settings)
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
    statement, keys, data = await parse_upload(account, bank, file, password, settings)
    document_id, _ = await store_document(
        db,
        user.id,
        source=DocumentSource.UPLOAD,
        content=data,
        mime_type="application/pdf",
        filename=file.filename,
        account_id=account.id,
    )
    extraction_id = await record_extraction(
        db,
        user.id,
        document_id,
        parser=f"statement:{bank.value}",
        parser_version=VERSIONS[bank],
        fields=statement_fields(statement),
    )
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
    ids: list[uuid.UUID] = []
    if rows:
        inserted = await db.scalars(
            insert(Transaction)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_transactions_account_id_dedupe_key")
            .returning(Transaction.id)
        )
        ids = list(inserted)
    # Every line's transaction, new or imported before, is backed by this statement.
    on_statement = await db.scalars(
        select(Transaction.id).where(
            Transaction.account_id == account.id, Transaction.dedupe_key.in_(keys)
        )
    )
    await link_sources(db, user.id, extraction_id, on_statement)
    await set_status(db, document_id, DocumentStatus.PROCESSED)
    categorised = await apply_rules(db, user.id, "ids", ids=ids)
    await db.commit()
    return ImportResult(
        document_id=document_id,
        imported=len(ids),
        skipped_duplicates=len(rows) - len(ids),
        categorised=categorised,
    )
