"""Housekeeping that runs on a schedule.

Periodic tasks: every worker knows the schedule, and Procrastinate records each scheduled run
(procrastinate_periodic_defers), so with several workers a run is still queued only once.
Minutes are odd on purpose: not on the hour, when everything else on the server runs.
"""

import logging

from procrastinate import JobContext

from duit.auth.sessions import purge_expired
from duit.jobs.app import app, sessionmaker

logger = logging.getLogger(__name__)

HOUR = 1
DAY = 24 * HOUR


@app.periodic(cron="23 * * * *")  # hourly
@app.task(queue="maintenance", queueing_lock="purge_expired_sessions")
async def purge_expired_sessions(timestamp: int) -> None:
    """Delete login sessions past their absolute or idle timeout. lookup() deletes one only
    when its cookie comes back; a browser that never returns would leave its row forever."""
    async with sessionmaker()() as db:
        purged = await purge_expired(db)
        await db.commit()
    logger.info("purged %d expired sessions", purged)


@app.periodic(cron="41 3 * * *")  # daily, 03:41 UTC (after the 03:00 database backup)
@app.task(queue="maintenance", queueing_lock="prune_job_history", pass_context=True)
async def prune_job_history(context: JobContext, timestamp: int) -> None:
    """Finished jobs stay in procrastinate_jobs as a history. Successful ones are kept a week;
    failed, cancelled and aborted ones a month, long enough to notice and investigate."""
    jobs = context.app.job_manager
    await jobs.delete_old_jobs(nb_hours=7 * DAY)
    await jobs.delete_old_jobs(
        nb_hours=30 * DAY, include_failed=True, include_cancelled=True, include_aborted=True
    )
