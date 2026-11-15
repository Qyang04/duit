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

## Deploy

`deploy/` is Kustomize: `base/` (CNPG Postgres, migrate Job, API) plus one overlay per environment.
Argo CD (configured in [homelab](https://github.com/Qyang04/homelab), `clusters/prod/apps/duit-staging.yaml`)
syncs `deploy/overlays/staging` to https://duit-staging.luiqiuyang.com in sync waves:

1. Postgres (`Cluster duit-db`), wait until healthy. CNPG writes credentials to Secret `duit-db-app`.
2. `duit-migrate` Job: `alembic upgrade head`. If it fails, the sync stops and the old API keeps serving.
3. API Deployment, Service, Ingress.

Every push to `main` that changes the image deploys itself: after the scanned image is pushed, the
`deploy-staging` CI job writes its `sha-<commit>` tag into `images[].newTag` and commits it; Argo CD rolls it out.
Rollback = revert that commit.

## Settings

Environment variables prefixed `DUIT_` (see `backend/duit/core/settings.py`).

| Variable | Default | Meaning |
|---|---|---|
| `DUIT_ENVIRONMENT` | `dev` | `dev`, `staging` or `prod`; `/docs` is off in prod |
| `DUIT_DATABASE_URL` | (required) | `postgresql+asyncpg://user:password@host:5432/db` |
| `DUIT_DATA_KEY` | (required by the API) | 32 random bytes, base64. AES-GCM key for sensitive columns (TOTP secrets, later bank credentials). Losing it makes those columns unreadable: back it up |
| `DUIT_REGISTRATION_OPEN` | `false` | When false, only the first account can register (claim the instance right after deploying) |

`.env.example` holds a public dev-only data key; never reuse it outside dev.

## Secrets

Kubernetes Secrets live in Git, encrypted with SOPS + age (`.sops.yaml`, same two keys as
[homelab](https://github.com/Qyang04/homelab)): the personal key on the owner's PC, and the cluster key
that KSOPS uses inside Argo CD. Only values (`data`/`stringData`) are encrypted; names stay readable.

- Staging: `deploy/overlays/staging/secrets.enc.yaml` (Secret `duit-app-secrets`, key `data-key`),
  listed in `secret-generator.yaml`.
- Edit: `sops deploy/overlays/staging/secrets.enc.yaml` (decrypts in an editor, re-encrypts on save).
- The Argo CD Application uses `ServerSideApply=true`, so no plaintext copy ends up in a
  `last-applied-configuration` annotation.
- The database password is not here: CloudNativePG generates it (Secret `duit-db-app`).

## Auth

| Endpoint | What |
|---|---|
| `POST /auth/register` | Create an account (first account only, unless registration is open) |
| `POST /auth/login` | Email + password (+ `totp_code` once 2FA is on); sets the session cookie |
| `POST /auth/logout` | Deletes the server-side session |
| `GET /auth/me` | Current user, 401 if not logged in |
| `POST /auth/totp/setup`, `/auth/totp/confirm` | Enrol an authenticator app; 2FA is enforced after confirm |

- Passwords: argon2id (19 MiB, 2 passes, 1 lane; OWASP minimum), rehashed on login if parameters change.
- Sessions: random 256-bit token in a `__Host-duit_session` cookie (HttpOnly, Secure, SameSite=Strict);
  the database stores only its SHA-256. 14 days absolute, 2 days idle.
- TOTP: RFC 6238, ±1 step drift, each code usable once; secret encrypted with `DUIT_DATA_KEY`,
  bound to its user.
- Login failures return one message and take the same time whether or not the email exists.

## Health endpoints

Used by Kubernetes and monitoring, not by the app's users.

- `/healthz`: liveness. The process is up; checks nothing else, so a database outage never restarts API pods.
- `/readyz`: readiness. Returns 503 while Postgres is unreachable, which takes the pod out of the Service.
