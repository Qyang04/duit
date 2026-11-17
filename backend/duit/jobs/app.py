"""The Procrastinate app: the job queue connection and the list of task modules.

Two connection pools, each small:
- Procrastinate's own (psycopg, which it requires): fetching jobs, recording results, and one
  connection held open to LISTEN for new jobs, so a deferred job starts at once, not on a poll.
- Our SQLAlchemy engine (asyncpg), which task code uses exactly like the API does.
"""

from functools import cache

from procrastinate import App, PsycopgConnector
from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from duit.core.db import create_engine
from duit.core.settings import get_settings


def psycopg_conninfo(raw: str) -> str:
    """DUIT_DATABASE_URL may name SQLAlchemy's driver (postgresql+asyncpg://); psycopg wants a
    plain libpq URL."""
    return make_url(raw).set(drivername="postgresql").render_as_string(hide_password=False)


app = App(
    connector=PsycopgConnector(
        conninfo=psycopg_conninfo(get_settings().database_url.get_secret_value()),
        min_size=2,  # the LISTEN connection + one for fetching and finishing jobs
        max_size=4,
    ),
    import_paths=["duit.jobs.maintenance"],
    worker_defaults={
        "concurrency": 2,  # parallel jobs per worker; our jobs mostly wait on I/O
        # On SIGTERM (a rollout, a node drain) running jobs get this long to finish before
        # they are aborted. Shorter than the pod's terminationGracePeriodSeconds.
        "shutdown_graceful_timeout": 25,
    },
)


@cache
def sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Database sessions for task code, from one engine per worker process."""
    return async_sessionmaker(create_engine(get_settings()), expire_on_commit=False)
