"""procrastinate job queue schema

Procrastinate (the background job queue) keeps its jobs in this database. Its schema is applied
from a copy committed next to the migrations (migrations/sql/procrastinate/), never read from
the installed package: a migration must do the same thing every time it runs, whichever
Procrastinate version happens to be installed then.

Upgrading Procrastinate later: add an Alembic migration that runs the new files from the
package's sql/migrations/ directory (copied here too), in order. tests/test_jobs.py fails until
that is done.

The tables live in `public` with the prefix procrastinate_; env.py hides them from autogenerate.

Revision ID: d2db981322c5
Revises: 567b8dbd4f1a
Create Date: 2026-11-17 13:35:56.884670

"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op
from sqlalchemy.util import await_only

# revision identifiers, used by Alembic.
revision: str = "d2db981322c5"
down_revision: str | Sequence[str] | None = "567b8dbd4f1a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = Path(__file__).parent.parent / "sql" / "procrastinate" / "3.11.0-schema.sql"

# Procrastinate ships no "drop" script. Tables first (CASCADE takes their triggers), then every
# function and type with the prefix, so a downgrade leaves nothing behind.
DROP = """
DROP TABLE procrastinate_events, procrastinate_periodic_defers, procrastinate_jobs,
    procrastinate_workers CASCADE;
DO $$
DECLARE f regprocedure;
BEGIN
    FOR f IN SELECT p.oid::regprocedure FROM pg_proc p
             WHERE p.pronamespace = 'public'::regnamespace AND p.proname LIKE 'procrastinate\\_%'
    LOOP
        EXECUTE 'DROP FUNCTION ' || f;
    END LOOP;
END $$;
DROP TYPE procrastinate_job_to_defer_v1, procrastinate_job_event_type, procrastinate_job_status;
"""


def run_script(sql: str) -> None:
    """Run a multi-statement SQL script inside the migration's transaction.

    Through SQLAlchemy, asyncpg sends every statement as a prepared statement, and a prepared
    statement holds exactly one command. asyncpg's own execute() without arguments uses the
    simple query protocol instead, which takes a whole script."""
    raw = op.get_bind().connection.driver_connection
    assert raw is not None  # noqa: S101  (always set inside a migration)
    await_only(raw.execute(sql))


def upgrade() -> None:
    run_script(SCHEMA.read_text(encoding="utf-8"))


def downgrade() -> None:
    run_script(DROP)
