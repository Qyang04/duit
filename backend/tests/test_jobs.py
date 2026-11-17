"""The job queue: Procrastinate's schema is migrated and current, a real worker runs jobs
against it, and the maintenance jobs do their work."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import procrastinate
import pytest
from procrastinate import App, PsycopgConnector
from procrastinate.jobs import Status
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from duit.auth.models import User, UserSession
from duit.auth.sessions import IDLE_TIMEOUT, purge_expired
from duit.core.settings import get_settings
from duit.jobs.app import app, psycopg_conninfo

pytestmark = pytest.mark.anyio

# The newest Procrastinate SQL migration our Alembic migrations include. When an upgrade
# brings newer files, add an Alembic migration that runs them (copied into
# migrations/sql/procrastinate/), then move this marker.
SCHEMA_INCLUDES = "03.11.00_01_pre_fetch_job_skip_null_lock.sql"


def test_every_procrastinate_migration_is_in_our_migrations() -> None:
    shipped = Path(procrastinate.__file__).parent / "sql" / "migrations"
    newer = sorted(p.name for p in shipped.glob("*.sql") if p.name > SCHEMA_INCLUDES)
    assert newer == [], f"Procrastinate was upgraded: migrate the database with {newer}"


def test_the_maintenance_jobs_are_scheduled() -> None:
    app.perform_import_paths()
    periodic: list[Any] = list(app.periodic_registry.periodic_tasks.values())  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    scheduled = {p.task.name: p.cron for p in periodic}
    assert scheduled == {
        "duit.jobs.maintenance.purge_expired_sessions": "23 * * * *",
        "duit.jobs.maintenance.prune_job_history": "41 3 * * *",
    }


async def test_a_worker_runs_a_job_end_to_end() -> None:
    """A real worker on the migrated database: defer, fetch, run, record the result.
    Uses its own committed rows (not the rolled-back test transaction) and its own queue, so a
    dev worker running at the same time never takes the job."""
    queue = f"test-{uuid.uuid4().hex[:12]}"  # queue names: at most 40 characters (NOTIFY channel)
    conninfo = psycopg_conninfo(get_settings().database_url.get_secret_value())
    test_app = App(connector=PsycopgConnector(conninfo=conninfo, min_size=1, max_size=2))
    ran: list[int] = []

    @test_app.task(queue=queue)
    async def add(a: int, b: int) -> None:  # pyright: ignore[reportUnusedFunction]
        ran.append(a + b)

    async with test_app.open_async():
        job_id = await add.defer_async(a=2, b=3)
        try:
            await test_app.run_worker_async(
                queues=[queue], wait=False, install_signal_handlers=False
            )
            job = await test_app.job_manager.get_job_status_async(job_id)
            assert job == Status.SUCCEEDED
            assert ran == [5]
        finally:
            await test_app.job_manager.delete_old_jobs(nb_hours=0, queue=queue)


async def test_expired_and_idle_sessions_are_purged(conn: AsyncConnection) -> None:
    user = (
        await conn.execute(
            insert(User).values(email="ana@example.com", password_hash="x").returning(User.id)
        )
    ).scalar_one()
    now = datetime.now(UTC)
    sessions = {
        "live": (now + timedelta(days=1), now),
        "expired": (now - timedelta(seconds=1), now),
        "idle": (now + timedelta(days=1), now - IDLE_TIMEOUT - timedelta(seconds=1)),
    }
    for name, (expires_at, last_seen_at) in sessions.items():
        await conn.execute(
            insert(UserSession).values(
                user_id=user,
                token_hash=name.encode(),
                expires_at=expires_at,
                last_seen_at=last_seen_at,
            )
        )

    async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as db:
        assert await purge_expired(db, now) == 2
        await db.commit()

    left = await conn.scalars(select(UserSession.token_hash).where(UserSession.user_id == user))
    assert left.all() == [b"live"]
