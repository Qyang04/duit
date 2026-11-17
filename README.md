# Duit

Personal finance and tax copilot for Malaysia. Runs on the [homelab](https://github.com/Qyang04/homelab) platform.

| Folder | What |
|---|---|
| `backend/` | One Python package (`duit`), two entrypoints: API (FastAPI) and worker |
| `docs/` | Architecture notes, ADRs, dependency budget |
| `Dockerfile.api` | API image (no OCR libraries; those live only in the worker image) |
| `Dockerfile.web` | Web image: Next.js static export on nginx-unprivileged, CSP computed at build |
| `web/` | Next.js web app (static export) |

## Develop

Needs [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 itself).

```powershell
docker compose up -d --wait                  # Postgres 18 on 127.0.0.1:5432 (dev only)
cd backend
copy .env.example .env                       # local DUIT_DATABASE_URL (gitignored)
uv sync                                      # create .venv from uv.lock
uv run alembic upgrade head                  # apply migrations
uv run uvicorn duit.main_api:app --reload    # http://127.0.0.1:8000/healthz, /readyz, /docs
uv run python -m procrastinate --app=duit.jobs.app.app worker   # background jobs (optional)
```

New migration after changing a model in `duit/`: `uv run alembic revision --autogenerate -m "what changed"`,
then **read the generated file** (renames show up as drop + add) before `alembic upgrade head`.

Checks (CI runs the same):

```powershell
uv run ruff check . ; uv run ruff format --check . ; uv run pyright ; uv run deptry . ; uv run alembic check ; uv run pytest
```

Tests expect an empty database (registration is first-account-only). If your dev database has an
account, run them against a separate one:

```powershell
docker exec duit-postgres-1 psql -U duit -c "CREATE DATABASE duit_test"
$env:DUIT_DATABASE_URL = "postgresql+asyncpg://duit:duit@127.0.0.1:5432/duit_test"
uv run alembic upgrade head ; uv run pytest
```

## Deploy

`deploy/` is Kustomize: `base/` (CNPG Postgres, migrate Job, API, web) plus one overlay per environment.
Argo CD (configured in [homelab](https://github.com/Qyang04/homelab), `clusters/prod/apps/duit-staging.yaml`)
syncs `deploy/overlays/staging` to https://duit-staging.luiqiuyang.com in sync waves:

1. Postgres (`Cluster duit-db`), wait until healthy. CNPG writes credentials to Secret `duit-db-app`.
   Backups: the CNPG Barman Cloud plugin archives WAL continuously to Cloudflare R2
   (`ObjectStore duit-backups`, `s3://homelab-backups/duit-staging/`), plus a nightly base backup
   (`ScheduledBackup duit-db-nightly`, 03:00 UTC); 14-day retention. R2 key: `backup-r2.enc.yaml` (SOPS).
2. `duit-migrate` Job: `alembic upgrade head`. If it fails, the sync stops and the old API keeps serving.
3. API and web Deployments and Services; one Ingress: `/api` to the API, everything else to the web app.
   `/healthz` and `/readyz` are not routed publicly (only the kubelet probes them).

Every push to `main` that changes an image deploys itself: CI builds, scans and pushes both images
(`duit-api`, `duit-web`, one matrix job), then `deploy-staging` writes the `sha-<commit>` tag into both
`images[].newTag` and commits it; Argo CD rolls it out.
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
| `POST /api/auth/register` | Create an account (first account only, unless registration is open) |
| `POST /api/auth/login` | Email + password (+ `totp_code` once 2FA is on); sets the session cookie |
| `POST /api/auth/logout` | Deletes the server-side session |
| `GET /api/auth/me` | Current user, 401 if not logged in |
| `POST /api/auth/totp/setup`, `/api/auth/totp/confirm` | Enrol an authenticator app; 2FA is enforced after confirm |

- Passwords: argon2id (19 MiB, 2 passes, 1 lane; OWASP minimum), rehashed on login if parameters change.
- Sessions: random 256-bit token in a `__Host-duit_session` cookie (HttpOnly, Secure, SameSite=Strict);
  the database stores only its SHA-256. 14 days absolute, 2 days idle.
- TOTP: RFC 6238, ±1 step drift, each code usable once; secret encrypted with `DUIT_DATA_KEY`,
  bound to its user.
- Login failures return one message and take the same time whether or not the email exists.

## Ledger

Tables: `accounts`, `categories` (two levels, seeded with defaults at registration), `transactions`,
`category_rules`.

| Endpoint | What |
|---|---|
| `GET/POST /api/accounts`, `GET/PATCH/DELETE /accounts/{id}` | Accounts; `account_number` and `statement_password` are write-only (only `account_last4` and `has_*` flags come back) |
| `GET/POST /api/categories`, `PATCH/DELETE /categories/{id}` | Flat list with `parent_id`; rename, move (`parent_id`, `null` = top level), delete |
| `GET/POST /api/transactions`, `GET/PATCH/DELETE /transactions/{id}` | Filters: `account_id`, `category_id` (a parent includes its children), `uncategorised`, `date_from`, `date_to`, `q`; `limit` + `cursor` |
| `GET/POST /api/rules`, `PATCH/DELETE /rules/{id}` | Categorisation rules; every change re-applies them and returns `recategorised` |

- Another user's row is a 404, never a 403, so ids cannot be probed. Request bodies reject unknown
  fields (no mass assignment of `user_id`, `dedupe_key`, ...).
- PATCH: omitted field = unchanged, `null` = cleared (rejected for required fields).
- Categories: two levels; subcategories take `is_income` from their parent; a category with
  children cannot be deleted or moved under another one.
- Transactions: currency defaults to the account's; a foreign currency needs `fx_rate`
  (account currency per unit; sent and returned as a string, never a float).
- Pagination is keyset on `(posted_on, id)`: `next_cursor` is opaque, `null` on the last page.

- Money is a signed `bigint` in sen (negative = money out) plus an ISO currency code; never floats.
- `posted_on` is a `date`, not a timestamp: statements give dates, and UTC midnight is the wrong
  day in Malaysia.
- Tenant isolation in the database: references between ledger tables are composite foreign keys
  on `(user_id, id)`, so a row can never point at another user's account or category.
- Deleting an account with transactions fails; deleting a category un-categorises its
  transactions (`ON DELETE SET NULL (category_id)`); deleting a user removes the whole ledger.
- `dedupe_key` is unique per account, so re-importing a statement line is a no-op.
- Account numbers and statement passwords are AES-GCM encrypted with `DUIT_DATA_KEY`.

## Categorisation rules (corrections become rules)

- `transactions.merchant_key` is a stored generated column, `merchant_key(merchant_raw)`: a SQL
  function that uppercases and keeps only letters, so `POS DEBIT / KK SUPER MART / 31/10/2026 2359`
  and next week's swipe both become `POS DEBIT KK SUPER MART`.
- Rules (`category_rules`): `merchant` = key equals the pattern (learned), `contains` = key
  contains it (written by you, at least 3 letters). Patterns are normalised the same way; a CHECK
  constraint enforces it. The most specific rule wins: exact merchant, then the longest contains.
- Setting a category by hand (PATCH, or POST with `category_id`) upserts a learned merchant rule
  (latest correction wins) and re-stamps that merchant's other rule-managed rows.
- `category_rule_id` records which rule set a category; NULL with a category means you set it,
  and rules never touch it again. Import, manual create and every rule change apply rules in one
  SQL statement (`duit/ledger/rules.py`); each rule counts its hits.
- Web: rule-set categories show as a pencilled (graphite) stamp; stamping inks it.

## Background jobs (Procrastinate)

- The queue lives in Postgres (`procrastinate_*` tables in `public`): no Redis, and the CNPG
  backups cover queued jobs. The worker (`deploy/base/worker.yaml`) is the API image running
  `python -m procrastinate --app=duit.jobs.app.app worker` (`python -m`, so `/app` is importable).
- Schema: Alembic migration `d2db981322c5` runs a copy of Procrastinate 3.11.0's `schema.sql`
  (`backend/migrations/sql/procrastinate/`), never the installed package's, so it replays
  identically. `env.py` hides the tables from autogenerate. Upgrading Procrastinate: copy its new
  `sql/migrations/*.sql` files and run them from a new Alembic migration;
  `tests/test_jobs.py` fails until you do.
- Driver: Procrastinate needs psycopg. The image uses Debian's `libpq5` (patched by `apt-get
  upgrade`, seen by Trivy); `psycopg[binary]` is a dev-only dependency for Windows and CI.
  CI's smoke test checks the image's psycopg loads it.
- Jobs (`duit/jobs/maintenance.py`, queue `maintenance`): purge expired login sessions (hourly
  at :23), prune job history (daily 03:41 UTC: successes after 7 days, failures after 30).
- Queue names: at most 40 characters (Postgres NOTIFY channels are 63 bytes, and Procrastinate
  prefixes 23).
- Inspect: `uv run python -m procrastinate --app=duit.jobs.app.app shell` (list jobs, retry).

## Statement import

`POST /api/accounts/{id}/statements/preview` and `/import` (multipart: `file`, optional `password`,
query `bank=cimb`). Preview parses and flags lines already in the ledger, saving nothing; import
parses the same PDF again on the server and inserts the new lines.

- Parsers (`duit/parsers/statements/`) are pure functions from positioned words to a statement;
  `read_pdf` (pdfplumber) is the only PDF code. CIMB: columns from each page's header row, a
  transaction starts with a date left of the Date header, indented lines are its description.
- Reconciliation: opening balance + every amount must reproduce every printed balance and the
  closing balance, or the statement is rejected. Error messages never contain amounts or names.
- `dedupe_key` = SHA-256 of account, date, amount, running balance, description and an occurrence
  count; inserts use `ON CONFLICT DO NOTHING`, so re-importing (even concurrently) is a no-op.
- The PDF password comes from the request or the account's stored (encrypted) statement password.
- Limits: 10 MB, `%PDF-` magic bytes, 60 pages. Parsing runs in a thread pool.
- Local tools (run on your own files, nothing leaves your machine):
  `uv run python tools/statement_layout.py <pdf>` prints a masked layout for writing a parser;
  `uv run python -m tools.parse_statement <pdf>` prints a shareable summary (`--show`: private detail).

## Web (`web/`)

Next.js (App Router) as a static export (ADR-002): `npm run build` writes plain files to `out/`.
The API lives under `/api` on the same origin, so the `SameSite=Strict` session cookie just works.

```bash
cd backend && uv run uvicorn duit.main_api:app --port 8000   # API
cd web && npm ci && npm run dev                               # http://localhost:3000, proxies /api
```

- Typed client: `npm run gen:api` turns `web/openapi.json` into `src/api/schema.d.ts`;
  `uv run python -m tools.export_openapi` (backend) refreshes the schema. CI fails on drift.
- Design, "Buku Akaun": a bank passbook. Paper and ink tokens with a dark "night ledger"
  (`src/app/globals.css`), Young Serif / Hanken Grotesk / IBM Plex Mono via `next/font`
  (self-hosted, no Google requests), categories as rubber stamps, rows that "print".
  No component library: native `popover` and form controls; motion respects reduced-motion.
- Keyboard: `/` search, `j`/`k` move, `c` or Enter stamps a category, Esc.
- Production: `Dockerfile.web` builds `out/` and serves it with nginx-unprivileged (UID 101, port
  8080, read-only root, `/tmp` emptyDir). `web/scripts/csp.mjs` hashes the inline scripts Next.js
  emits and writes the CSP header (`script-src 'self'` + those hashes, no `unsafe-inline` for
  scripts), plus `nosniff`, `Referrer-Policy`, `Permissions-Policy`, COOP. Hashed assets are cached
  for a year, pages revalidate on every load.

## Health endpoints

Used by Kubernetes and monitoring, not by the app's users.

- `/healthz`: liveness. The process is up; checks nothing else, so a database outage never restarts API pods.
- `/readyz`: readiness. Returns 503 while Postgres is unreachable, which takes the pod out of the Service.
