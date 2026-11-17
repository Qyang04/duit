"""Queue a job inside our own database transaction.

Procrastinate's defer_async() inserts the job through its own psycopg pool, which is a separate
transaction: if ours then rolled back, the job would run for a row that does not exist, and if
the job was queued after our commit and that failed, the row would never be processed.

Here the job row is inserted by our session, so it commits or rolls back together with the data
it is about (the transactional outbox pattern, with the queue table itself as the outbox). The
NOTIFY that wakes a worker is sent by a trigger on that insert, and Postgres delivers it only
when the transaction commits, so a worker never sees a job before its data.

It calls procrastinate_defer_jobs_v1, the same SQL function defer_async() ends up in. That
function is part of the schema copy our migrations apply (see test_jobs: an upgrade that changes
the schema fails CI until it is migrated).
"""

import json
from typing import Any

from procrastinate.tasks import Task
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DEFER = text(
    """
    SELECT (procrastinate_defer_jobs_v1(ARRAY[ROW(
        CAST(:queue AS varchar), CAST(:task AS varchar), CAST(:priority AS integer),
        CAST(:lock AS text), CAST(:queueing_lock AS text), CAST(:args AS jsonb),
        NULL::timestamptz
    )]::procrastinate_job_to_defer_v1[]))[1]
    """
)


async def defer_in_transaction(
    db: AsyncSession, task: Task[Any, Any, Any], *, queue: str | None = None, **kwargs: Any
) -> int:
    """Queue task(**kwargs) when db commits; nothing if it rolls back. Returns the job id.
    Arguments must be JSON (pass ids as strings)."""
    result = await db.execute(
        DEFER,
        {
            "queue": queue or task.queue,
            "task": task.name,
            "priority": task.priority,
            "lock": task.lock,
            "queueing_lock": task.queueing_lock,
            "args": json.dumps(kwargs),
        },
    )
    return result.scalar_one()
