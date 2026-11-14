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
cd backend
uv sync                                      # create .venv from uv.lock
uv run uvicorn duit.main_api:app --reload    # http://127.0.0.1:8000/healthz, /docs
```

Checks (CI runs the same):

```powershell
uv run ruff check . ; uv run ruff format --check . ; uv run pyright ; uv run deptry . ; uv run pytest
```

## Settings

Environment variables prefixed `DUIT_` (see `backend/duit/core/settings.py`).

| Variable | Default | Meaning |
|---|---|---|
| `DUIT_ENVIRONMENT` | `dev` | `dev`, `staging` or `prod`; `/docs` is off in prod |
