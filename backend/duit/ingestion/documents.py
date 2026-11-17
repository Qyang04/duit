"""Storing raw documents and extractions, and /documents to list and download them.

Both stores are idempotent, enforced by unique constraints rather than a SELECT first: the same
bytes are one document (per user), and one parser version reads a document once.
"""

import hashlib
import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from duit.auth.router import CurrentUser
from duit.core.deps import Db
from duit.ingestion.models import (
    DocumentSource,
    DocumentStatus,
    Extraction,
    RawDocument,
    TransactionSource,
)
from duit.ledger.common import not_found

router = APIRouter(prefix="/documents", tags=["documents"])


async def store_document(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    source: DocumentSource,
    content: bytes,
    mime_type: str,
    filename: str | None = None,
    account_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Store the bytes unless this user already has them; either way, return the document id."""
    sha256 = hashlib.sha256(content).digest()
    inserted = await db.scalar(
        insert(RawDocument)
        .values(
            user_id=user_id,
            source=source,
            account_id=account_id,
            filename=filename[:255] if filename else None,
            mime_type=mime_type,
            size_bytes=len(content),
            sha256=sha256,
            content=content,
        )
        .on_conflict_do_nothing(constraint="uq_raw_documents_user_id_sha256")
        .returning(RawDocument.id)
    )
    if inserted is not None:
        return inserted
    # Already stored. A concurrent insert of the same bytes made ON CONFLICT wait for it to
    # commit, so this read finds the row.
    return (
        await db.scalars(
            select(RawDocument.id).where(
                RawDocument.user_id == user_id, RawDocument.sha256 == sha256
            )
        )
    ).one()


async def record_extraction(
    db: AsyncSession,
    user_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    parser: str,
    parser_version: str,
    fields: dict[str, Any],
) -> uuid.UUID:
    """Store what a parser read, unless this parser version already read this document."""
    inserted = await db.scalar(
        insert(Extraction)
        .values(
            user_id=user_id,
            raw_document_id=document_id,
            parser=parser,
            parser_version=parser_version,
            fields=fields,
        )
        .on_conflict_do_nothing(constraint="uq_extractions_raw_document_id_parser_parser_version")
        .returning(Extraction.id)
    )
    if inserted is not None:
        return inserted
    return (
        await db.scalars(
            select(Extraction.id).where(
                Extraction.raw_document_id == document_id,
                Extraction.parser == parser,
                Extraction.parser_version == parser_version,
            )
        )
    ).one()


async def link_sources(
    db: AsyncSession, user_id: uuid.UUID, extraction_id: uuid.UUID, txn_ids: Iterable[uuid.UUID]
) -> None:
    rows = [
        {"user_id": user_id, "transaction_id": t, "extraction_id": extraction_id} for t in txn_ids
    ]
    if rows:
        await db.execute(insert(TransactionSource).values(rows).on_conflict_do_nothing())


async def set_status(
    db: AsyncSession, document_id: uuid.UUID, status: DocumentStatus, error: str | None = None
) -> None:
    await db.execute(
        update(RawDocument)
        .where(RawDocument.id == document_id)
        .values(status=status, error=error)  # updated_at: the column's onupdate
    )


# --- API -------------------------------------------------------------------------------------


class DocumentOut(BaseModel):
    id: uuid.UUID
    source: DocumentSource
    account_id: uuid.UUID | None
    filename: str | None
    mime_type: str
    size_bytes: int
    sha256: str  # hex
    status: DocumentStatus
    error: str | None
    received_at: datetime

    @classmethod
    def of(cls, d: RawDocument) -> "DocumentOut":
        return cls(
            id=d.id,
            source=d.source,
            account_id=d.account_id,
            filename=d.filename,
            mime_type=d.mime_type,
            size_bytes=d.size_bytes,
            sha256=d.sha256.hex(),
            status=d.status,
            error=d.error,
            received_at=d.received_at,
        )


EXTENSIONS = {"application/pdf": "pdf", "message/rfc822": "eml", "image/jpeg": "jpg"}


@router.get("")
async def list_documents(
    user: CurrentUser,
    db: Db,
    before: Annotated[uuid.UUID | None, Query(description="Next page: the last id seen")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[DocumentOut]:
    """Newest first. Ids are UUIDv7 (time-ordered), so `before` pages without an offset."""
    query = select(RawDocument).where(RawDocument.user_id == user.id)
    if before is not None:
        query = query.where(RawDocument.id < before)
    documents = await db.scalars(query.order_by(RawDocument.id.desc()).limit(limit))
    return [DocumentOut.of(d) for d in documents]


@router.get(
    "/{document_id}/content",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}, "description": "The bytes"}},
)
async def download_document(document_id: uuid.UUID, user: CurrentUser, db: Db) -> Response:
    """The original bytes, always as a download. Documents come from outside (emails, uploads):
    served inline on our origin, an HTML or SVG "document" could run script as the user."""
    row = (
        await db.execute(
            select(RawDocument.content, RawDocument.mime_type).where(
                RawDocument.id == document_id, RawDocument.user_id == user.id
            )
        )
    ).one_or_none()
    if row is None:
        raise not_found("Document")
    extension = EXTENSIONS.get(row.mime_type, "bin")
    return Response(
        row.content,
        media_type=row.mime_type,
        headers={
            # Our own name, never the uploaded one (no header injection, no path tricks).
            "Content-Disposition": f'attachment; filename="document-{document_id}.{extension}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, no-store",
        },
    )
