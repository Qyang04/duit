"""raw documents, extractions, transaction sources

Storage for every input as received (bytea for now), parser results, and the link from each
transaction to the extractions that produced it. See duit/ingestion/models.py.

Revision ID: f4d357af6170
Revises: d2db981322c5
Create Date: 2026-11-17 14:14:01.357314

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f4d357af6170"
down_revision: str | Sequence[str] | None = "d2db981322c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # First: transaction_sources' composite foreign key refers to it.
    op.create_unique_constraint(
        op.f("uq_transactions_user_id_id"), "transactions", ["user_id", "id"]
    )
    op.create_table(
        "raw_documents",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "upload",
                "email",
                "telegram",
                name="document_source",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("filename", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.LargeBinary(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "received",
                "processed",
                "failed",
                "ignored",
                name="document_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="received",
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(sha256) = 32", name=op.f("ck_raw_documents_sha256_length")
        ),
        sa.CheckConstraint(
            "size_bytes = octet_length(content)", name=op.f("ck_raw_documents_size_matches")
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["accounts.user_id", "accounts.id"],
            name=op.f("fk_raw_documents_user_id_account_id_accounts"),
            ondelete="SET NULL (account_id)",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_raw_documents_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raw_documents")),
        sa.UniqueConstraint("user_id", "id", name=op.f("uq_raw_documents_user_id_id")),
        sa.UniqueConstraint("user_id", "sha256", name=op.f("uq_raw_documents_user_id_sha256")),
    )
    op.create_index(
        "ix_raw_documents_status",
        "raw_documents",
        ["status"],
        unique=False,
        postgresql_where=sa.text("status = 'received'"),
    )
    op.create_table(
        "extractions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("raw_document_id", sa.Uuid(), nullable=False),
        sa.Column("parser", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "raw_document_id"],
            ["raw_documents.user_id", "raw_documents.id"],
            name=op.f("fk_extractions_user_id_raw_document_id_raw_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_extractions_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extractions")),
        sa.UniqueConstraint(
            "raw_document_id",
            "parser",
            "parser_version",
            name=op.f("uq_extractions_raw_document_id_parser_parser_version"),
        ),
        sa.UniqueConstraint("user_id", "id", name=op.f("uq_extractions_user_id_id")),
    )
    op.create_table(
        "transaction_sources",
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "extraction_id"],
            ["extractions.user_id", "extractions.id"],
            name=op.f("fk_transaction_sources_user_id_extraction_id_extractions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "transaction_id"],
            ["transactions.user_id", "transactions.id"],
            name=op.f("fk_transaction_sources_user_id_transaction_id_transactions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_transaction_sources_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "transaction_id", "extraction_id", name=op.f("pk_transaction_sources")
        ),
    )
    op.create_index(
        "ix_transaction_sources_extraction_id",
        "transaction_sources",
        ["extraction_id"],
        unique=False,
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index("ix_transaction_sources_extraction_id", table_name="transaction_sources")
    op.drop_table("transaction_sources")
    op.drop_table("extractions")
    op.drop_index(
        "ix_raw_documents_status",
        table_name="raw_documents",
        postgresql_where=sa.text("status = 'received'"),
    )
    op.drop_table("raw_documents")
    op.drop_constraint(op.f("uq_transactions_user_id_id"), "transactions", type_="unique")  # last
    # ### end Alembic commands ###
