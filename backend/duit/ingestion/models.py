"""Every input is stored unchanged first (raw -> parsed -> ledger), so any parser can be re-run
later and every transaction can be traced back to the document it came from.

- raw_documents: the original bytes (a statement PDF, an alert email, a receipt photo).
- extractions: what one parser version read from one document. Many per document, so a fixed
  or new parser can run again and the results can be compared.
- transaction_sources: which extractions produced or confirmed a transaction. One transaction
  may have several (an alert, then the statement line for the same purchase).

Same tenant rule as the ledger: composite (user_id, id) foreign keys, so a row can only point at
rows of the same user.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from duit.core.db import Base
from duit.ledger.models import text_enum


class DocumentSource(enum.StrEnum):
    UPLOAD = "upload"  # a statement or receipt uploaded in the app
    EMAIL = "email"  # a bank or e-wallet alert email
    TELEGRAM = "telegram"  # a receipt photo sent to the bot


class DocumentStatus(enum.StrEnum):
    RECEIVED = "received"  # stored, waiting for its processing job
    PROCESSED = "processed"  # a parser read it and the ledger is updated
    FAILED = "failed"  # no parser could read it; `error` says why
    IGNORED = "ignored"  # read, but nothing for the ledger (e.g. a promotional email)


class RawDocument(Base):
    __tablename__ = "raw_documents"
    __table_args__ = (
        UniqueConstraint("user_id", "id"),
        # The same bytes twice (a re-upload, an email fetched again) are one document.
        UniqueConstraint("user_id", "sha256"),
        # Deleting the account keeps its documents (the evidence), no longer linked to it.
        ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["accounts.user_id", "accounts.id"],
            ondelete="SET NULL (account_id)",
        ),
        CheckConstraint("octet_length(sha256) = 32", name="sha256_length"),
        CheckConstraint("size_bytes = octet_length(content)", name="size_matches"),
        # The processing queue's view: what still needs work.
        Index("ix_raw_documents_status", "status", postgresql_where=text("status = 'received'")),
    )
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    source: Mapped[DocumentSource] = mapped_column(text_enum(DocumentSource, "document_source"))
    # The account it belongs to, when the source says so (a statement uploaded into an account).
    account_id: Mapped[uuid.UUID | None]
    filename: Mapped[str | None]  # as the client named it; never used as a path
    mime_type: Mapped[str]
    size_bytes: Mapped[int]
    sha256: Mapped[bytes]
    # The original bytes, in Postgres for now (bytea, stored out of line by TOAST and covered by
    # the database backups). deferred: listing documents never loads the files. Moving to object
    # storage later = add a storage key, copy, then drop this column.
    content: Mapped[bytes] = mapped_column(deferred=True)
    status: Mapped[DocumentStatus] = mapped_column(
        text_enum(DocumentStatus, "document_status"), server_default="received"
    )
    error: Mapped[str | None]  # why processing failed; never contains document content
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Extraction(Base):
    __tablename__ = "extractions"
    __table_args__ = (
        UniqueConstraint("user_id", "id"),
        # One parser version reads the same bytes the same way: importing a file again reuses
        # its extraction. A new parser_version gets a new row next to the old one.
        UniqueConstraint("raw_document_id", "parser", "parser_version"),
        ForeignKeyConstraint(
            ["user_id", "raw_document_id"],
            ["raw_documents.user_id", "raw_documents.id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    raw_document_id: Mapped[uuid.UUID]
    parser: Mapped[str]  # e.g. "statement:cimb", "alert:maybank-card", later "llm:receipt"
    parser_version: Mapped[str]
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB)  # what the parser read
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TransactionSource(Base):
    __tablename__ = "transaction_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "transaction_id"],
            ["transactions.user_id", "transactions.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "extraction_id"],
            ["extractions.user_id", "extractions.id"],
            ondelete="CASCADE",
        ),
        # "Where did this extraction's transactions go" (the primary key covers the other way).
        Index("ix_transaction_sources_extraction_id", "extraction_id"),
    )

    transaction_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    extraction_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
