# Deploying aprntc (B1)

aprntc ships as a **single container**: a multi-stage build compiles the React
frontend, and FastAPI/uvicorn serves both the API and the static UI on one port.

## Quick start (Docker)
```bash
cp .env.example .env        # fill ARK_API_KEY, VIKINGDB_AK/SK, model ids
docker compose up --build   # → http://localhost:8000  (UI + API)
```
Persistent data (SQLite trajectory store, per-tenant namespaces, playbooks) lives in
the `aprntc-data` volume mounted at `/data`.

## What the image does
- **Stage 1 (node:22):** `npm install && npm run build` → `web/dist`.
- **Stage 2 (python:3.12):** `pip install -e '.[web,byteplus]'`, copies `web/dist`,
  sets `APRNTC_STATIC_DIR=/app/web/dist`, runs
  `uvicorn aprntc.web.app:app --host 0.0.0.0 --port 8000`.
- The final image has **no Node toolchain** (build artifacts only).

## Single-port serving (verified)
FastAPI serves everything on :8000 — no separate Vite in production:
- `/` and client-side routes (`/trajectories`, …) → the SPA (`index.html`)
- `/assets/*` → hashed static bundles
- `/api/*` → JSON API (never shadowed by the SPA fallback)
Local equivalent (no Docker):
```bash
cd web && npm run build && cd ..
APRNTC_STATIC_DIR=$PWD/web/dist .venv/bin/python -m uvicorn aprntc.web.app:app --port 8000
```

## Configuration (env)
- **Secrets:** `ARK_API_KEY`, `APRNTC_POLICY_MODEL`, `APRNTC_JUDGE_MODEL`, `VIKINGDB_AK`,
  `VIKINGDB_SK` (see `.env.example`). Provide via `--env-file`/compose `env_file`, never baked in.
- **`APRNTC_STATIC_DIR`** — path to the built frontend (set by the image; overridable).
- **`APRNTC_DB_URL`** — trajectory store URL. Default is the SQLite file at
  `/data/aprntc.db`. Set to a `postgresql://user:password@host:5432/db` URL for the
  Postgres backend (required for multi-worker uvicorn — see "Multi-worker" below).
- **Multi-tenancy (B2):** wire a `TenantResolver` in `AppState.from_env` (per-tenant data under
  the data root); external agents then authenticate with `X-API-Key` / `Bearer`.

## Multi-worker (Postgres + uvicorn `--workers N`)

SQLite uses a process-wide file lock — fine for a single uvicorn worker, but
concurrent writers from multiple workers serialize on it. For real concurrency,
swap the store to Postgres:

1. Provision a Postgres database (any 13+ instance: managed RDS/Cloud SQL/Neon,
   or self-hosted). Grant the deploy user `CREATE TABLE` privileges — the store
   creates its schema on first connect.

2. Install the extra:
   ```bash
   pip install 'aprntc[web,byteplus,postgres]'
   ```

3. Set `APRNTC_DB_URL` in `.env` / your deployment config:
   ```
   APRNTC_DB_URL=postgresql://aprntc:secret@db.internal:5432/aprntc
   ```
   The store auto-creates its tables (`episodes`, `labels`, `outcomes`,
   `subject_episodes`) and indexes on first connect — same schema as SQLite,
   translated to Postgres syntax (`BIGSERIAL`, `ON CONFLICT` upserts).

4. Run uvicorn with workers. Each worker opens its own connection at startup;
   Postgres handles concurrent writes:
   ```bash
   uvicorn aprntc.web.app:app --host 0.0.0.0 --port 8000 --workers 4
   ```

The frontend, API surface, and dashboard behavior are unchanged — the swap is
transparent. Same `make_trajectory_store(url)` factory dispatches based on the
URL prefix; everything downstream treats the store generically.

**Validating the parity:** the suite ships env-gated parity tests
(`tests/test_pg_store.py`) — `APRNTC_TEST_PG_URL=postgresql://… pytest -q` runs
them, otherwise they skip. Use a separate scratch DB; the fixture drops + recreates
the tables on each run.

## Production notes / follow-ups
- **State:** SQLite + JSON files on a volume is fine for a single-worker
  instance. For multi-worker / HA, use the Postgres backend above; playbooks /
  tenants / fleet metadata remain JSON files (small, infrequent writes — fine
  on a shared volume).
- **Scheduler:** nightly distillation isn't auto-run yet (B3) — trigger via cron/job for now.
- **TLS / ingress:** terminate TLS at your load balancer / reverse proxy in front of :8000.
