"""Alert templates: a signed alert from a known sender becomes a pending transaction; a spoofed
one is rejected; anything else is ignored. Emails here are made up, in the layouts of the real
alerts (learned from masked copies)."""

import email.message
import email.policy
import uuid
from collections.abc import Callable
from datetime import date, datetime
from email.parser import BytesParser
from email.utils import format_datetime
from typing import Any

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from duit.auth.models import User
from duit.ingestion.documents import store_document
from duit.ingestion.inbox import process_document
from duit.ingestion.models import (
    DocumentSource,
    DocumentStatus,
    Extraction,
    RawDocument,
    TransactionSource,
)
from duit.ingestion.reprocess import requeue
from duit.ledger.models import Account, Transaction
from duit.parsers.alerts.engine import (
    MYT,
    Alert,
    NoMatch,
    NoTemplate,
    Rejected,
    Template,
    match,
    templates,
)

pytestmark = pytest.mark.anyio

GOOGLE = "mx.google.com"


def signed(domain: str, server: str = GOOGLE) -> str:
    return f"{server}; dkim=pass header.i=@{domain} header.s=s1; spf=pass; dmarc=pass"


def html_email(
    sender: str, subject: str, html: str, auth: list[str], sent: datetime | None = None
) -> bytes:
    message = email.message.EmailMessage()
    for value in auth:  # topmost first, as servers prepend them
        message["Authentication-Results"] = value
    message["From"] = sender
    message["Subject"] = subject
    message["Date"] = format_datetime(sent or datetime(2026, 11, 17, 9, 30, tzinfo=MYT))
    message.set_content(html, subtype="html")
    return message.as_bytes()


def gx_received(
    amount: str = "12.50", name: str = "TAN AH KOW", auth: list[str] | None = None, **kw: Any
) -> bytes:
    html = (
        '<html><head><style>p{}</style></head><body><img alt="GX Bank Logo">'
        "<h1>Money received</h1><p>Hi Ana Lim,</p>"
        f"<p>You’ve received RM{amount} from {name}.</p>"
        "<p>For enquiries, please visit our Help Centre.</p></body></html>"
    )
    return html_email(
        "GXBank <alerts@gxbank.my>",
        "Money received",
        html,
        [signed("gxbank.my"), signed("amazonses.com")] if auth is None else auth,
        **kw,
    )


def cimb_received(
    amount: str = "1,234.56", last4: str = "1234", when: str = "17NOV2026 14:03:22"
) -> bytes:
    html = (
        "<table><tr><td>Cash in summary</td></tr>"
        "<tr><td>Dear Valued Customer,</td></tr>"
        f"<tr><td>You've received RM{amount} in account ending ******{last4} on {when}.</td></tr>"
        "<tr><td>This email is automatically generated.</td></tr></table>"
    )
    return html_email(
        "CIMB Notification <noreply@notification.cimb.com>",
        "Transaction Notification",
        html,
        [signed("notification.cimb.com")],
    )


def parse(raw: bytes) -> email.message.EmailMessage:
    return BytesParser(policy=email.policy.default).parsebytes(raw)  # pyright: ignore[reportReturnType]


def read(raw: bytes) -> Any:
    return match(parse(raw), GOOGLE, templates())


# --- templates -------------------------------------------------------------------------------


def test_a_gx_alert_is_read() -> None:
    sent = datetime(2026, 11, 17, 23, 30, tzinfo=MYT)  # 15:30 UTC: the date is Malaysia's
    alert = read(gx_received(amount="1,012.50", sent=sent))
    assert isinstance(alert, Alert)
    assert alert.template.name == "gx-received"
    assert (alert.amount_sen, alert.merchant_raw, alert.last4) == (101250, "TAN AH KOW", None)
    assert alert.posted_on == date(2026, 11, 17)
    assert alert.occurred_at is None


def test_a_cimb_alert_is_read() -> None:
    alert = read(cimb_received())
    assert isinstance(alert, Alert)
    assert alert.template.name == "cimb-received"
    assert (alert.amount_sen, alert.last4) == (123456, "1234")
    assert alert.merchant_raw == "CIMB incoming transfer"
    assert alert.occurred_at == datetime(2026, 11, 17, 14, 3, 22, tzinfo=MYT)


@pytest.mark.parametrize(
    "auth",
    [
        [],  # no results at all
        [signed("gxbank.my", server="evil.example")],  # written by a server that is not ours
        # Ours says fail; a forged "pass" further down (added by the sender) is never read.
        [f"{GOOGLE}; dkim=fail header.i=@gxbank.my", signed("gxbank.my")],
        [signed("amazonses.com")],  # signed, but by someone else's domain
        [signed("evilgxbank.my")],  # a look-alike domain
    ],
)
def test_an_alert_without_the_senders_signature_is_rejected(auth: list[str]) -> None:
    assert isinstance(read(gx_received(auth=auth)), Rejected)


def test_a_subdomain_signature_counts() -> None:
    assert isinstance(read(gx_received(auth=[signed("mail.gxbank.my")])), Alert)


def test_other_senders_are_not_alerts() -> None:
    raw = html_email("Google <no-reply@accounts.google.com>", "Security alert", "<p>Hi</p>", [])
    assert read(raw) == NoTemplate("accounts.google.com")


def test_a_known_sender_with_an_unknown_layout_is_flagged() -> None:
    raw = html_email(
        "GXBank <alerts@gxbank.my>", "Money received", "<p>New layout</p>", [signed("gxbank.my")]
    )
    assert read(raw) == NoMatch("alerts@gxbank.my")


def test_every_template_loads() -> None:
    names = {t.name for t in templates()}
    assert {"gx-received", "cimb-received"} <= names


def test_a_template_must_capture_an_amount() -> None:
    with pytest.raises(ValueError, match="amount"):
        Template(
            name="x",
            version=1,
            sender="a@b.my",
            dkim="b.my",
            institution="B",
            direction="in",
            subject="x",
            body="no groups",
            merchant="x",
        )


# --- into the ledger -------------------------------------------------------------------------


@pytest.fixture
def sessions(conn: AsyncConnection) -> Callable[[], AsyncSession]:
    return lambda: AsyncSession(
        bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


@pytest.fixture
async def owner(conn: AsyncConnection) -> uuid.UUID:
    return (
        await conn.execute(
            insert(User).values(email="ana@example.com", password_hash="x").returning(User.id)
        )
    ).scalar_one()


async def add_account(conn: AsyncConnection, owner: uuid.UUID, **values: Any) -> uuid.UUID:
    return (
        await conn.execute(
            insert(Account)
            .values(user_id=owner, type="bank", name=values.pop("name", "Main"), **values)
            .returning(Account.id)
        )
    ).scalar_one()


async def receive(
    sessions: Callable[[], AsyncSession], owner: uuid.UUID, raw: bytes
) -> tuple[uuid.UUID, DocumentStatus | None]:
    async with sessions() as db:
        document_id, _ = await store_document(
            db, owner, source=DocumentSource.EMAIL, content=raw, mime_type="message/rfc822"
        )
        status = await process_document(db, document_id)
        await db.commit()
    return document_id, status


async def test_an_alert_becomes_a_pending_transaction(
    conn: AsyncConnection, sessions: Callable[[], AsyncSession], owner: uuid.UUID
) -> None:
    await add_account(conn, owner, name="Old CIMB", institution="CIMB", account_last4="9999")
    account = await add_account(conn, owner, institution="cimb", account_last4="1234")

    document_id, status = await receive(sessions, owner, cimb_received())

    assert status == DocumentStatus.PROCESSED
    txn = (
        await conn.execute(
            select(
                Transaction.id,
                Transaction.account_id,
                Transaction.amount_sen,
                Transaction.status,
                Transaction.posted_on,
                Transaction.merchant_raw,
            )
        )
    ).one()
    assert (txn.account_id, txn.amount_sen, txn.status) == (account, 123456, "pending")
    assert (txn.posted_on, txn.merchant_raw) == (date(2026, 11, 17), "CIMB incoming transfer")
    extraction = (
        await conn.execute(
            select(Extraction.id, Extraction.parser, Extraction.parser_version, Extraction.fields)
        )
    ).one()
    assert (extraction.parser, extraction.parser_version) == ("alert:cimb-received", "1")
    assert extraction.fields["amount_sen"] == 123456
    link = (
        await conn.execute(
            select(TransactionSource.transaction_id, TransactionSource.extraction_id)
        )
    ).one()
    assert (link.transaction_id, link.extraction_id) == (txn.id, extraction.id)
    document = await conn.scalar(
        select(RawDocument.account_id).where(RawDocument.id == document_id)
    )
    assert document == account


async def test_without_its_account_an_alert_waits_to_be_processed_again(
    conn: AsyncConnection, sessions: Callable[[], AsyncSession], owner: uuid.UUID
) -> None:
    document_id, status = await receive(sessions, owner, gx_received())
    assert status == DocumentStatus.FAILED
    error = await conn.scalar(select(RawDocument.error).where(RawDocument.id == document_id))
    assert error is not None and "GX Bank" in error and "12.50" not in error

    await add_account(conn, owner, name="GX", institution="GX Bank")
    async with sessions() as db:
        assert await requeue(db, [DocumentStatus.FAILED]) == 1
        assert await process_document(db, document_id) == DocumentStatus.PROCESSED
        await db.commit()
    assert len((await conn.execute(select(Transaction))).all()) == 1


async def test_processing_an_alert_again_adds_nothing(
    conn: AsyncConnection, sessions: Callable[[], AsyncSession], owner: uuid.UUID
) -> None:
    await add_account(conn, owner, name="GX", institution="GX Bank")
    await receive(sessions, owner, gx_received())
    async with sessions() as db:  # back to received, as reprocess does
        await db.execute(update(RawDocument).values(status=DocumentStatus.RECEIVED))
        document_id = await db.scalar(select(RawDocument.id))
        assert document_id is not None
        assert await process_document(db, document_id) == DocumentStatus.PROCESSED
        await db.commit()
    assert len((await conn.execute(select(Transaction))).all()) == 1


async def test_a_spoofed_alert_fails_and_adds_nothing(
    conn: AsyncConnection, sessions: Callable[[], AsyncSession], owner: uuid.UUID
) -> None:
    await add_account(conn, owner, name="GX", institution="GX Bank")
    document_id, status = await receive(sessions, owner, gx_received(auth=[]))
    assert status == DocumentStatus.FAILED
    error = await conn.scalar(select(RawDocument.error).where(RawDocument.id == document_id))
    assert error is not None and "DKIM" in error
    assert (await conn.execute(select(Transaction))).all() == []


async def test_an_email_from_anyone_else_is_ignored(
    sessions: Callable[[], AsyncSession], owner: uuid.UUID
) -> None:
    raw = html_email("TeamViewer <news@mails.teamviewer.com>", "Payment due", "<p>x</p>", [])
    assert (await receive(sessions, owner, raw))[1] == DocumentStatus.IGNORED
