"""Alert emails: poll the mailbox every minute, store each new message, and process it in a job.

poll_inbox (periodic): reads messages with a UID above the cursor, oldest first. For each one, in
ONE database transaction: store it as a raw document (source=email), queue its process_email
job (duit.jobs.defer), and move the cursor past it. So after a crash at any point, a message is
either stored with its job queued and the cursor past it, or none of these, and the next run
fetches it again. A message seen twice (the same bytes under another UID, or after the server
renumbered the folder) is the same document, and is not processed again.

process_email: reads the stored message. Alert templates come next (Week 5 step 4); until then
every email is marked ignored.
"""

import asyncio
import email.policy
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from email.parser import BytesParser

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from duit.auth.models import User
from duit.core.settings import ImapConfig, get_settings
from duit.ingestion.documents import set_status, store_document
from duit.ingestion.imap import ImapInbox, Inbox
from duit.ingestion.models import DocumentSource, DocumentStatus, ImapCursor, RawDocument
from duit.jobs.app import app, sessionmaker
from duit.jobs.defer import defer_in_transaction

logger = logging.getLogger(__name__)

BATCH = 50  # messages per run; a backlog (the first run on a full mailbox) drains over runs
# Alerts are a few KB to a few hundred KB. Anything far bigger is not an alert, and a job holds
# the whole message in memory (the worker's limit is 192 MiB).
MAX_BYTES = 5 * 1024 * 1024
MIME_TYPE = "message/rfc822"

Sessions = Callable[[], AsyncSession]


@dataclass
class PollResult:
    stored: int = 0  # new documents, each with its process_email job queued
    duplicates: int = 0  # bytes already stored
    too_large: int = 0  # skipped (logged with UID and size)


async def poll(
    sessions: Sessions, inbox: Inbox, mailbox: str, owner_id: uuid.UUID, batch: int = BATCH
) -> PollResult:
    result = PollResult()
    async with sessions() as db:
        cursor = await db.get(ImapCursor, mailbox)
        known = (cursor.uidvalidity, cursor.last_uid) if cursor else None
    validity = await asyncio.to_thread(inbox.uidvalidity)
    last_uid = known[1] if known and known[0] == validity else 0
    if known and known[0] != validity:
        logger.warning("UIDVALIDITY of %s changed: reading the folder again", mailbox)

    for uid, size in await asyncio.to_thread(inbox.new_messages, last_uid, batch):
        content = None
        if size > MAX_BYTES:
            logger.warning("skipping UID %d of %s: %d bytes", uid, mailbox, size)
            result.too_large += 1
        else:
            content = await asyncio.to_thread(inbox.fetch, uid)
        async with sessions() as db:
            if content is not None:
                document_id, new = await store_document(
                    db, owner_id, source=DocumentSource.EMAIL, content=content, mime_type=MIME_TYPE
                )
                if new:
                    await defer_in_transaction(db, process_email, document_id=str(document_id))
                    result.stored += 1
                else:
                    result.duplicates += 1
            await db.execute(
                insert(ImapCursor)
                .values(mailbox=mailbox, uidvalidity=validity, last_uid=uid)
                .on_conflict_do_update(
                    index_elements=[ImapCursor.mailbox],
                    set_={
                        "uidvalidity": validity,
                        "last_uid": uid,
                        "updated_at": func.now(),
                    },  # onupdate: not for upserts,
                )
            )
            await db.commit()
    return result


async def process_document(db: AsyncSession, document_id: uuid.UUID) -> DocumentStatus | None:
    """Process one stored email; returns its new status, or None if there was nothing to do
    (already processed: a job can run twice, e.g. retried after a worker died mid-way)."""
    document = await db.scalar(
        select(RawDocument)
        .where(RawDocument.id == document_id)
        .options(undefer(RawDocument.content))
        .with_for_update()
    )
    if document is None or document.status != DocumentStatus.RECEIVED:
        return None
    message = BytesParser(policy=email.policy.default).parsebytes(
        document.content, headersonly=True
    )
    # No alert templates yet (Week 5 step 4). Log the sender's domain only: never addresses,
    # subjects or bodies, which may hold names, amounts and card digits.
    logger.info("no alert template for email from %s", sender_domain(message.get("From")))
    await set_status(db, document.id, DocumentStatus.IGNORED)
    return DocumentStatus.IGNORED


def sender_domain(header: object) -> str:
    try:
        return header.addresses[0].domain.lower() or "?"  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]
    except (AttributeError, IndexError):
        return "?"


# --- jobs ------------------------------------------------------------------------------------


@app.task(queue="ingestion", retry=3)
async def process_email(document_id: str) -> None:
    async with sessionmaker()() as db:
        await process_document(db, uuid.UUID(document_id))
        await db.commit()


@app.periodic(cron="* * * * *")  # every minute
# lock: never two polls at once (the cursor is read at the start); queueing_lock: at most one
# waiting, so a long outage does not pile up a run per minute.
@app.task(queue="ingestion", lock="poll_inbox", queueing_lock="poll_inbox")
async def poll_inbox(timestamp: int) -> None:
    config = get_settings().imap()
    if config is None:
        return  # no mailbox configured
    owner_id = await owner(config)
    with await asyncio.to_thread(ImapInbox, config) as inbox:
        result = await poll(sessionmaker(), inbox, config.key, owner_id)
    if result.stored or result.duplicates or result.too_large:
        logger.info("inbox: %s", result)


async def owner(config: ImapConfig) -> uuid.UUID:
    async with sessionmaker()() as db:
        user_id = await db.scalar(select(User.id).where(User.email == config.owner))
    if user_id is None:
        raise LookupError("DUIT_IMAP_OWNER is not a Duit user")
    return user_id
