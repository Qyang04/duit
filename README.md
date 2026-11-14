# Duit

Personal finance and tax copilot for Malaysia. Runs on the [homelab](https://github.com/Qyang04/homelab) platform.

| Folder | What |
|---|---|
| `backend/` | One Python package (`duit`), two entrypoints: API (FastAPI) and worker |
| `docs/` | Architecture notes, ADRs, dependency budget |
| `Dockerfile.api` | API image (no OCR libraries; those live only in the worker image) |

## Develop

Needs [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 itself).

```powershell
docker compose up -d --wait                  # Postgres 18 on 127.0.0.1:5432 (dev only)
cd backend
copy .env.example .env                       # local DUIT_DATABASE_URL (gitignored)
uv sync                                      # create .venv from uv.lock
uv run alembic upgrade head                  # apply migrations
uv run uvicorn duit.main_api:app --reload    # http://127.0.0.1:8000/healthz, /readyz, /docs
```

New migration after changing a model in `duit/`: `uv run alembic revision --autogenerate -m "what changed"`,
then **read the generated file** (renames show up as drop + add) before `alembic upgrade head`.

Checks (CI runs the same):

```powershell
uv run ruff check . ; uv run ruff format --check . ; uv run pyright ; uv run deptry . ; uv run alembic check ; uv run pytest
```

## Settings

Environment variables prefixed `DUIT_` (see `backend/duit/core/settings.py`).

| Variable | Default | Meaning |
|---|---|---|
| `DUIT_ENVIRONMENT` | `dev` | `dev`, `staging` or `prod`; `/docs` is off in prod |
| `DUIT_DATABASE_URL` | (required) | `postgresql+asyncpg://user:password@host:5432/db` |

## Health endpoints

- `/healthz`: liveness. The process is up; checks nothing else, so a database outage never restarts API pods.
- `/readyz`: readiness. Returns 503 while Postgres is unreachable, which takes the pod out of the Service.
