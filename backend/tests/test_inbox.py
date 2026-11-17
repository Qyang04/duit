"""Alert emails: the poller stores each new message with its processing job in one transaction,
reads every message once, and the IMAP client stays read-only."""

import email.message
import hashlib
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from procrastinate import App, PsycopgConnector
from procrastinate.jobs import Status
from pydantic import SecretStr
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from duit.auth.models import User
from duit.core.db import create_engine
from duit.core.settings import ImapConfig, Settings, get_settings
from duit.ingestion import imap, inbox
from duit.ingestion.inbox import MAX_BYTES, poll, process_document, process_email
from duit.ingestion.models import DocumentStatus, ImapCursor, RawDocument
from duit.jobs.app import psycopg_conninfo
from duit.jobs.defer import defer_in_transaction

pytestmark = pytest.mark.anyio

MAILBOX = "alerts@example.com@imap.example.com/INBOX"


def alert(n: int, sender: str = "alerts@bank.example") -> bytes:
    message = email.message.EmailMessage()
    message["From"] = f"Bank <{sender}>"
    message["Subject"] = f"Transaction alert {n}"
    message.set_content(f"You spent RM{n}.00")
    return message.as_bytes()


class FakeInbox:
    """A mailbox as the poller sees it, recording what was fetched."""

    def __init__(self, messages: dict[int, bytes], validity: int = 1) -> None:
        self.messages = messages
        self.validity = validity
        self.sizes: dict[int, int] = {}  # override a message's reported size
        self.fetched: list[int] = []

    def uidvalidity(self) -> int:
        return self.validity

    def new_messages(self, after_uid: int, limit: int) -> list[tuple[int, int]]:
        uids = sorted(u for u in self.messages if u > after_uid)[:limit]
        return [(u, self.sizes.get(u, len(self.messages[u]))) for u in uids]

    def fetch(self, uid: int) -> bytes:
        self.fetched.append(uid)
        return self.messages[uid]


@pytest.fixture
async def owner(conn: AsyncConnection) -> uuid.UUID:
    return (
        await conn.execute(
            insert(User).values(email="ana@example.com", password_hash="x").returning(User.id)
        )
    ).scalar_one()


@pytest.fixture
def sessions(conn: AsyncConnection) -> Callable[[], AsyncSession]:
    """Sessions in the test's transaction: their commits are savepoints, rolled back at the end."""
    return lambda: AsyncSession(
        bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


async def stored(conn: AsyncConnection) -> list[tuple[str, str]]:
    """(document status, sha256) of every email document, oldest first."""
    rows = await conn.execute(
        select(RawDocument.status, RawDocument.sha256)
        .where(RawDocument.source == "email")
        .order_by(RawDocument.id)
    )
    return [(status, sha.hex()) for status, sha in rows]


async def queued_jobs(conn: AsyncConnection) -> list[str]:
    """document_id of every queued process_email job for documents in this test."""
    rows = await conn.scalars(
        text(
            "SELECT j.args->>'document_id' FROM procrastinate_jobs j"
            " JOIN raw_documents d ON d.id::text = j.args->>'document_id'"
            " WHERE j.task_name = :task AND j.status = 'todo' ORDER BY j.id"
        ),
        {"task": process_email.name},
    )
    return list(rows)


async def cursor(conn: AsyncConnection) -> tuple[int, int] | None:
    row = (
        await conn.execute(
            select(ImapCursor.uidvalidity, ImapCursor.last_uid).where(ImapCursor.mailbox == MAILBOX)
        )
    ).one_or_none()
    return None if row is None else (row[0], row[1])


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# --- polling ---------------------------------------------------------------------------------


async def test_each_new_message_is_stored_with_its_job(
    conn: AsyncConnection, sessions: Callable[[], AsyncSession], owner: uuid.UUID
) -> None:
    mailbox = FakeInbox({3: alert(1), 7: alert(2)}, validity=42)
    result = await poll(sessions, mailbox, MAILBOX, owner)

    assert (result.stored, result.duplicates, result.too_large) == (2, 0, 0)
    assert await stored(conn) == [("received", sha(alert(1))), ("received", sha(alert(2)))]
    documents = await conn.scalars(
        select(RawDocument.id).where(RawDocument.source == "email").order_by(RawDocument.id)
    )
    assert await queued_jobs(conn) == [str(d) for d in documents]  # one job per document
    assert await cursor(conn) == (42, 7)


async def test_a_message_is_read_once(
    conn: AsyncConnection, sessions: Callable[[], AsyncSession], owner: uuid.UUID
) -> None:
    mailbox = FakeInbox({1: alert(1)})
    await poll(sessions, mailbox, MAILBOX, owner)
    assert (await poll(sessions, mailbox, MAILBOX, owner)).stored == 0

    mailbox.messages[2] = alert(2)
    assert (await poll(sessions, mailbox, MAILBOX, owner)).stored == 1
    assert mailbox.fetched == [1, 2]  # the cursor, not the hashes, kept 1 from being fetched again
    assert len(await queued_jobs(conn)) == 2


async def test_the_same_email_twice_is_one_document_and_one_job(
    conn: AsyncConnection, sessions: Callable[[], AsyncSession], owner: uuid.UUID
) -> None:
    """E.g. the alert copied into the folder again: a new UID, the same bytes."""
    result = await poll(sessions, FakeInbox({1: alert(1), 2: alert(1)}), MAILBOX, owner)
    assert (result.stored, result.duplicates) == (1, 1)
    assert len(await stored(conn)) == 1
    assert len(await queued_jobs(conn)) == 1
    assert await cursor(conn) == (1, 2)


async def test_a_renumbered_folder_is_read_again_without_duplicates(
    conn: AsyncConnection, sessions: Callable[[], AsyncSession], owner: uuid.UUID
) -> None:
    await poll(sessions, FakeInbox({5: alert(1), 6: alert(2)}, validity=1), MAILBOX, owner)
    # The server rebuilt the folder: new UIDVALIDITY, the same messages under new UIDs.
    renumbered = FakeInbox({1: alert(1), 2: alert(2), 3: alert(3)}, validity=2)
    result = await poll(sessions, renumbered, MAILBOX, owner)

    assert renumbered.fetched == [1, 2, 3]
    assert (result.stored, result.duplicates) == (1, 2)
    assert len(await queued_jobs(conn)) == 3
    assert await cursor(conn) == (2, 3)


async def test_a_backlog_drains_in_batches(
    conn: AsyncConnection, sessions: Callable[[], AsyncSession], owner: uuid.UUID
) -> None:
    mailbox = FakeInbox({uid: alert(uid) for uid in range(1, 6)})
    assert (await poll(sessions, mailbox, MAILBOX, owner, batch=2)).stored == 2
    assert await cursor(conn) == (1, 2)
    assert (await poll(sessions, mailbox, MAILBOX, owner, batch=2)).stored == 2
    assert (await poll(sessions, mailbox, MAILBOX, owner, batch=2)).stored == 1
    assert len(await stored(conn)) == 5


async def test_an_oversized_message_is_skipped_unread(
    conn: AsyncConnection, sessions: Callable[[], AsyncSession], owner: uuid.UUID
) -> None:
    mailbox = FakeInbox({1: alert(1), 2: alert(2)})
    mailbox.sizes[1] = MAX_BYTES + 1
    result = await poll(sessions, mailbox, MAILBOX, owner)

    assert (result.stored, result.too_large) == (1, 1)
    assert mailbox.fetched == [2]  # never downloaded
    assert await cursor(conn) == (1, 2)


async def test_a_failed_fetch_leaves_the_message_for_the_next_run(
    conn: AsyncConnection, sessions: Callable[[], AsyncSession], owner: uuid.UUID
) -> None:
    class Flaky(FakeInbox):
        def fetch(self, uid: int) -> bytes:
            if uid == 2:
                raise imap.ImapError("UID FETCH: connection reset")
            return super().fetch(uid)

    with pytest.raises(imap.ImapError):
        await poll(sessions, Flaky({1: alert(1), 2: alert(2)}), MAILBOX, owner)
    assert await cursor(conn) == (1, 1)  # 1 is stored and its job queued; 2 is not
    assert len(await queued_jobs(conn)) == 1

    await poll(sessions, FakeInbox({1: alert(1), 2: alert(2)}), MAILBOX, owner)
    assert len(await stored(conn)) == 2


# --- processing ------------------------------------------------------------------------------


async def test_processing_marks_an_email_without_a_template_ignored(
    conn: AsyncConnection, sessions: Callable[[], AsyncSession], owner: uuid.UUID
) -> None:
    await poll(sessions, FakeInbox({1: alert(1)}), MAILBOX, owner)
    document_id = (await conn.scalars(select(RawDocument.id))).one()

    async with sessions() as db:
        assert await process_document(db, document_id) == DocumentStatus.IGNORED
        await db.commit()
    async with sessions() as db:  # a retried or duplicate job: nothing to do
        assert await process_document(db, document_id) is None
        assert await process_document(db, uuid.uuid4()) is None
    assert [status for status, _ in await stored(conn)] == ["ignored"]


# --- queueing in our transaction -------------------------------------------------------------


async def test_a_job_is_queued_only_if_the_transaction_commits(
    sessions: Callable[[], AsyncSession], conn: AsyncConnection
) -> None:
    async with sessions() as db:
        job_id = await defer_in_transaction(db, process_email, document_id="x")
        job = (
            await db.execute(
                text(
                    "SELECT queue_name, task_name, args, status"
                    " FROM procrastinate_jobs WHERE id = :id"
                ),
                {"id": job_id},
            )
        ).one()
        assert tuple(job) == ("ingestion", process_email.name, {"document_id": "x"}, "todo")
        await db.rollback()
    jobs = await conn.scalar(
        text("SELECT count(*) FROM procrastinate_jobs WHERE id = :id"), {"id": job_id}
    )
    assert jobs == 0


async def test_a_worker_runs_a_job_queued_in_our_transaction() -> None:
    """The row our SQL writes is one a real worker fetches and runs (committed rows, own queue)."""
    queue = f"test-{uuid.uuid4().hex[:12]}"
    settings = get_settings()
    test_app = App(
        connector=PsycopgConnector(
            conninfo=psycopg_conninfo(settings.database_url.get_secret_value()),
            min_size=1,
            max_size=2,
        )
    )
    ran: list[str] = []

    @test_app.task(queue=queue)
    async def note(word: str) -> None:
        ran.append(word)

    engine = create_engine(settings)
    try:
        async with AsyncSession(engine) as db:
            job_id = await defer_in_transaction(db, note, word="hello")
            await db.commit()
        async with test_app.open_async():
            try:
                await test_app.run_worker_async(
                    queues=[queue], wait=False, install_signal_handlers=False
                )
                assert await test_app.job_manager.get_job_status_async(job_id) == Status.SUCCEEDED
                assert ran == ["hello"]
            finally:
                await test_app.job_manager.delete_old_jobs(nb_hours=0, queue=queue)
    finally:
        await engine.dispose()


# --- settings and the IMAP client ------------------------------------------------------------


def test_polling_is_off_until_fully_configured() -> None:
    base: dict[str, Any] = {"database_url": "postgresql://x", "imap_host": "imap.example.com"}
    assert Settings(**base).imap() is None
    config = Settings(
        **base,
        imap_username="a@example.com",
        imap_password=SecretStr("p"),
        imap_owner="Ana@Example.com",
    ).imap()
    assert config is not None
    assert config.owner == "ana@example.com"
    assert config.key == "a@example.com@imap.example.com/INBOX"


class FakeImap:
    """imaplib.IMAP4_SSL as the server would answer, recording every command."""

    sent: list[tuple[Any, ...]] = []

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.sent.clear()
        self.sent.append(("connect", host, port, kwargs["ssl_context"].check_hostname))
        self.messages = {7: alert(7), 9: alert(9)}

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.sent.append(("login", user))
        return "OK", [b"LOGIN completed"]

    def select(self, mailbox: str, readonly: bool) -> tuple[str, list[bytes]]:
        self.sent.append(("select", mailbox, readonly))
        return "OK", [b"2"]

    def response(self, code: str) -> tuple[str, list[bytes | None]]:
        return code, [b"777"]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        self.sent.append(("uid", command, *args))
        if command == "SEARCH":
            low = int(args[1].split(":")[0])
            found = [u for u in self.messages if u >= low] or [max(self.messages)]  # N:* rule
            return "OK", [" ".join(map(str, found)).encode()]
        if args[1] == "(RFC822.SIZE)":
            uids = [int(u) for u in args[0].split(",")]
            return "OK", [
                f"{i} (UID {u} RFC822.SIZE {len(self.messages[u])})".encode()
                for i, u in enumerate(uids, 1)
            ]
        u = int(args[0])
        return "OK", [
            (f"1 (UID {u} BODY[] {{{len(self.messages[u])}}}".encode(), self.messages[u]),
            b")",
        ]

    def logout(self) -> tuple[str, list[bytes]]:
        self.sent.append(("logout",))
        return "BYE", [b""]

    def shutdown(self) -> None: ...


def test_the_imap_client_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imap.imaplib, "IMAP4_SSL", FakeImap)
    config = ImapConfig(
        host="imap.example.com",
        port=993,
        username="a@example.com",
        password=SecretStr("p"),
        folder='Bank "alerts"',
        owner="ana@example.com",
    )
    with imap.ImapInbox(config) as mailbox:
        assert mailbox.uidvalidity() == 777
        assert mailbox.new_messages(0, 10) == [(7, len(alert(7))), (9, len(alert(9)))]
        assert mailbox.new_messages(7, 10) == [(9, len(alert(9)))]
        assert mailbox.new_messages(9, 10) == []  # "10:*" still answers 9
        assert mailbox.fetch(9) == alert(9)

    assert FakeImap.sent[0] == ("connect", "imap.example.com", 993, True)  # host name checked
    assert ("select", '"Bank \\"alerts\\""', True) in FakeImap.sent  # EXAMINE: read-only
    fetches = [s[3] for s in FakeImap.sent if s[:2] == ("uid", "FETCH")]
    assert "(BODY.PEEK[])" in fetches  # PEEK: never sets \Seen
    assert not {"STORE", "COPY", "MOVE", "EXPUNGE"} & {s[1] for s in FakeImap.sent if s[0] == "uid"}
    assert FakeImap.sent[-1] == ("logout",)


async def test_polling_does_nothing_without_a_mailbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inbox, "get_settings", lambda: Settings(database_url=SecretStr("x")))
    monkeypatch.setattr(inbox, "ImapInbox", None)  # would fail if it tried to connect
    await inbox.poll_inbox(timestamp=0)
