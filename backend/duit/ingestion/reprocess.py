"""Process stored emails again, e.g. after adding a template or creating a missing account.

    kubectl -n duit-staging exec deploy/duit-worker -- python -m duit.ingestion.reprocess
    ... -- python -m duit.ingestion.reprocess --status failed

Sets the emails back to received and queues a process_email job for each, in one transaction.
Processing an email again never adds its transaction twice (dedupe_key alert:<document id>).
"""

import argparse
import asyncio

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from duit.ingestion.inbox import process_email
from duit.ingestion.models import DocumentSource, DocumentStatus, RawDocument
from duit.jobs.app import sessionmaker
from duit.jobs.defer import defer_in_transaction


async def requeue(db: AsyncSession, statuses: list[DocumentStatus]) -> int:
    ids = await db.scalars(
        update(RawDocument)
        .where(RawDocument.source == DocumentSource.EMAIL, RawDocument.status.in_(statuses))
        .values(status=DocumentStatus.RECEIVED, error=None)
        .returning(RawDocument.id)
    )
    count = 0
    for document_id in ids.all():
        await defer_in_transaction(db, process_email, document_id=str(document_id))
        count += 1
    return count


async def run(statuses: list[DocumentStatus]) -> None:
    async with sessionmaker()() as db:
        count = await requeue(db, statuses)
        await db.commit()
    print(f"queued {count} emails")


def main() -> None:
    parser = argparse.ArgumentParser(description="Process stored emails again.")
    parser.add_argument(
        "--status",
        action="append",
        choices=[DocumentStatus.IGNORED.value, DocumentStatus.FAILED.value],
        help="which emails (repeatable; default: ignored and failed)",
    )
    args = parser.parse_args()
    statuses = [DocumentStatus(s) for s in (args.status or ["ignored", "failed"])]
    asyncio.run(run(statuses))


if __name__ == "__main__":
    main()
