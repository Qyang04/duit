"""Raw documents, extractions and transaction sources: a statement import keeps the PDF as
received and links every transaction to what the parser read; documents stay per user."""

import hashlib
import uuid
from typing import Any

import httpx2
import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from duit.auth.models import User
from duit.ingestion.models import Extraction, RawDocument, TransactionSource
from tests.fake_cimb import make_pdf
from tests.test_ledger_schema import add_account, add_transaction
from tests.test_statement_import import PDF_PASSWORD, upload

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def pdf() -> bytes:
    return make_pdf(PDF_PASSWORD)


@pytest.fixture
async def account(ana: httpx2.AsyncClient) -> str:
    response = await ana.post("/api/accounts", json={"type": "bank", "name": "CIMB savings"})
    return response.json()["id"]


async def documents(http: httpx2.AsyncClient, **params: Any) -> list[dict[str, Any]]:
    response = await http.get("/api/documents", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def count(conn: AsyncConnection, model: type[Any]) -> int:
    return await conn.scalar(select(func.count()).select_from(model)) or 0


# --- statement import keeps the evidence -----------------------------------------------------


async def test_import_stores_the_pdf_as_received(
    ana: httpx2.AsyncClient, account: str, pdf: bytes
) -> None:
    result = (await upload(ana, account, "import", pdf)).json()

    [doc] = await documents(ana)
    assert doc["id"] == result["document_id"]
    assert doc | {"id": None, "received_at": None} == {
        "id": None,
        "source": "upload",
        "account_id": account,
        "filename": "statement.pdf",
        "mime_type": "application/pdf",
        "size_bytes": len(pdf),
        "sha256": hashlib.sha256(pdf).hexdigest(),
        "status": "processed",
        "error": None,
        "received_at": None,
    }

    download = await ana.get(f"/api/documents/{doc['id']}/content")
    assert download.status_code == 200
    assert download.content == pdf  # byte for byte, still password-protected
    assert download.headers["content-type"] == "application/pdf"
    assert download.headers["content-disposition"] == (
        f'attachment; filename="document-{doc["id"]}.pdf"'
    )
    assert download.headers["x-content-type-options"] == "nosniff"


async def test_every_transaction_links_to_the_extraction(
    ana: httpx2.AsyncClient, account: str, pdf: bytes, conn: AsyncConnection
) -> None:
    await upload(ana, account, "import", pdf)
    await upload(ana, account, "import", pdf)  # again: nothing new anywhere

    assert await count(conn, RawDocument) == 1
    assert await count(conn, Extraction) == 1
    assert await count(conn, TransactionSource) == 4  # one per statement line
    extraction = (await conn.execute(select(Extraction.parser, Extraction.fields))).one()
    assert extraction.parser == "statement:cimb"
    assert len(extraction.fields["lines"]) == 4
    assert extraction.fields["opening_balance_sen"] == 100000


async def test_an_overlapping_statement_confirms_existing_transactions(
    ana: httpx2.AsyncClient, account: str, pdf: bytes, conn: AsyncConnection
) -> None:
    """Another file with the same lines (a re-download is a different PDF): a second document
    and extraction, and the same 4 transactions now have two sources each."""
    await upload(ana, account, "import", pdf)
    await upload(ana, account, "import", make_pdf(PDF_PASSWORD, title="Re-downloaded"))

    assert await count(conn, RawDocument) == 2
    assert await count(conn, TransactionSource) == 8
    assert len((await ana.get("/api/transactions")).json()["items"]) == 4


async def test_a_failed_parse_stores_nothing(
    ana: httpx2.AsyncClient, account: str, pdf: bytes
) -> None:
    assert (await upload(ana, account, "import", pdf, password="J@000000")).status_code == 422
    assert await documents(ana) == []


async def test_documents_page_newest_first(
    ana: httpx2.AsyncClient, account: str, pdf: bytes
) -> None:
    for title in ("one", "two", "three"):
        await upload(ana, account, "import", make_pdf(PDF_PASSWORD, title=title))
    first_page = await documents(ana, limit=2)
    second_page = await documents(ana, limit=2, before=first_page[-1]["id"])
    ids = [d["id"] for d in first_page + second_page]
    assert len(ids) == 3
    assert ids == sorted(ids, reverse=True)


# --- isolation -------------------------------------------------------------------------------


async def test_documents_are_per_user(
    ana: httpx2.AsyncClient, bob: httpx2.AsyncClient, account: str, pdf: bytes
) -> None:
    document_id = (await upload(ana, account, "import", pdf)).json()["document_id"]
    assert await documents(bob) == []
    assert (await bob.get(f"/api/documents/{document_id}/content")).status_code == 404
    assert (await bob.get(f"/api/documents/{uuid.uuid4()}/content")).status_code == 404


async def test_the_same_file_is_one_document_per_user(
    ana: httpx2.AsyncClient, bob: httpx2.AsyncClient, account: str, pdf: bytes
) -> None:
    """Deduplication by sha256 is per user: Bob's identical file is his own document."""
    ana_doc = (await upload(ana, account, "import", pdf)).json()["document_id"]
    bob_account = (await bob.post("/api/accounts", json={"type": "bank", "name": "CIMB"})).json()
    bob_doc = (await upload(bob, bob_account["id"], "import", pdf)).json()["document_id"]
    assert bob_doc != ana_doc


# --- database guarantees ---------------------------------------------------------------------


async def add_user(conn: AsyncConnection, email: str) -> uuid.UUID:
    return (
        await conn.execute(insert(User).values(email=email, password_hash="x").returning(User.id))
    ).scalar_one()


async def add_document(
    conn: AsyncConnection, user_id: uuid.UUID, content: bytes = b"x"
) -> uuid.UUID:
    return (
        await conn.execute(
            insert(RawDocument)
            .values(
                user_id=user_id,
                source="upload",
                mime_type="application/pdf",
                size_bytes=len(content),
                sha256=hashlib.sha256(content).digest(),
                content=content,
            )
            .returning(RawDocument.id)
        )
    ).scalar_one()


async def test_size_and_hash_are_checked(conn: AsyncConnection) -> None:
    ana = await add_user(conn, "ana@example.com")
    with pytest.raises(IntegrityError, match="ck_raw_documents_size_matches"):
        async with conn.begin_nested():
            await conn.execute(
                insert(RawDocument).values(
                    user_id=ana,
                    source="upload",
                    mime_type="text/plain",
                    size_bytes=99,
                    sha256=hashlib.sha256(b"x").digest(),
                    content=b"x",
                )
            )
    with pytest.raises(IntegrityError, match="ck_raw_documents_sha256_length"):
        await conn.execute(
            insert(RawDocument).values(
                user_id=ana,
                source="upload",
                mime_type="text/plain",
                size_bytes=1,
                sha256=b"short",
                content=b"x",
            )
        )


async def test_a_source_cannot_link_across_users(conn: AsyncConnection) -> None:
    ana = await add_user(conn, "ana@example.com")
    bob = await add_user(conn, "bob@example.com")
    bob_txn = await add_transaction(conn, bob, await add_account(conn, bob))
    ana_extraction = (
        await conn.execute(
            insert(Extraction)
            .values(
                user_id=ana,
                raw_document_id=await add_document(conn, ana),
                parser="statement:cimb",
                parser_version="1",
                fields={},
            )
            .returning(Extraction.id)
        )
    ).scalar_one()
    with pytest.raises(IntegrityError, match="fk_transaction_sources_user_id_transaction_id"):
        await conn.execute(
            insert(TransactionSource).values(
                user_id=ana, transaction_id=bob_txn, extraction_id=ana_extraction
            )
        )


async def test_deleting_the_account_keeps_its_documents(
    ana: httpx2.AsyncClient, account: str, pdf: bytes
) -> None:
    await upload(ana, account, "import", pdf)
    for txn in (await ana.get("/api/transactions")).json()["items"]:
        assert (await ana.delete(f"/api/transactions/{txn['id']}")).status_code == 204

    assert (await ana.delete(f"/api/accounts/{account}")).status_code == 204
    [doc] = await documents(ana)
    assert doc["account_id"] is None
