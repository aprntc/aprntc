# aprntc

> An **apprentice** agent that shadows a live **parent** agent, distills lessons from its
> behavior, becomes a measurably better version, and — gated by a human — is **promoted**
> to replace it. Then the cycle repeats. Codename **"Shadow."**

**Status:** Feature-complete (MVP + v1 + productionization). **348 tests passing**
(+13 Postgres-gated). All four collector paths (SDK wrapper / LiteLLM egress proxy /
OpenTelemetry / MCP) live-verified end-to-end against real ModelArk + real SDKs.
Postgres backend + multi-worker uvicorn live-verified against Postgres 16. Runnable
as a single container with Google sign-in and per-tenant isolation.

## What it is

A standalone, framework-agnostic, self-improving-agent product:
**observe → label → distill → evaluate → promote**, with a human only at the promotion gate.

- **Learning (Phase 1):** Experience Memory (RAG) + **playbook distillation** — no fine-tuning, so it
  works on **closed-source** LLMs (the only thing that changes between generations is a **Playbook** =
  system prompt + directives + exemplars + lessons; promotion = a versioned config swap, instantly
  reversible).
- **Quality is anchored on outcomes**, fused with explicit user feedback and a **recused LLM judge**
  (judge ≠ policy); fusion weights are learned from each source's agreement with the anchor.
- **NOT a monitoring/observability product** — it learns from the parent's overall *response quality*
  to produce a better successor (see `docs/PRODUCTION.md`).

## The core idea

The "child" is **not a separate program** — it's the same engine (model + retrieval + tools) running a
candidate **Playbook** distilled from the parent's good/bad trajectories. Promotion points the live
agent at the new Playbook; rollback points it back. This is why it works on closed models and why
every change is attributable and reversible.

## What's built

| Area | Capability |
|---|---|
| **Loop (MVP)** | Trajectory schema + store (SQLite OR Postgres), AgentTap, evaluation/labeling, VikingDB memory, distiller, promotion gate, lineage |
| **Tap collectors** | SDK wrapper · LiteLLM egress proxy · OpenTelemetry ingester · MCP gateway · A2A interceptor — tap any external agent (3 of 4 live-verified end-to-end with reference demo scripts) |
| **Online** | Shadow (judge child vs parent on live traffic, **gated on A3 judge-trust**) · A/B canary with auto-rollback |
| **Autonomy** | Learned fusion weights → **derives the trust signal that gates A4 auto-promotion + live shadow** · auto-promotion policy (default-off, hard-gated, with append-only audit log) · multi-agent fleets + lesson sharing |
| **Integration** | Playbook registry + config-fetch API (external agents pull their active playbook) — see `docs/ONBOARDING.md` for the 5-line wiring per collector |
| **Knowledge** | Local TF-IDF KB + **VikingDB semantic retriever** (drop-in swap; A/B eval against the GOLD set) |
| **Product** | React dashboard (7 screens incl. Fleet + Shadow & canary + 👍/👎 feedback + auto-promote audit panel) · Google sign-in · per-tenant isolation (data + audit + memory) · Docker deploy · **Postgres backend + multi-worker uvicorn** · scheduler/retry/metrics |

## Quick start

### Run the dashboard
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[byteplus,web]'
cp .env.example .env                 # fill real keys (gitignored)

# backend (API + UI on one port)
cd web && npm install && npm run build && cd ..
APRNTC_STATIC_DIR=$PWD/web/dist .venv/bin/python -m uvicorn aprntc.web.app:app
# → http://localhost:8000
```
Dev mode (hot-reload UI): run `uvicorn aprntc.web.app:app --reload` and, in another terminal,
`cd web && npm run dev` → http://localhost:5173.

Docker: `docker compose up --build` → http://localhost:8000 (see `docs/DEPLOY.md`).

### Try the loop on a real agent
```bash
.venv/bin/python scripts/demo_byteplus_loop.py   # thin parent → distill → child → gate (live)
.venv/bin/python scripts/seed_dashboard.py       # seed demo data for the dashboard
```

### Wire an external agent through each collector path (live demos)
```bash
.venv/bin/python scripts/demo_external_agent.py  # LiteLLM proxy → collector=egress_proxy
.venv/bin/python scripts/demo_otel_agent.py      # OpenTelemetry SDK → collector=otel
.venv/bin/python scripts/demo_mcp_agent.py       # MCP SDK → collector=mcp
```
Each script doubles as the production-wiring example. See `docs/ONBOARDING.md`.

### Multi-worker production deploy (Postgres backend)
```bash
APRNTC_DB_URL=postgresql://user:pwd@db:5432/aprntc \
  .venv/bin/python -m uvicorn aprntc.web.app:app --workers 4 --port 8000
```
SQLite remains the zero-deps default for single-worker. See `docs/DEPLOY.md`.

## Configuration

All config via `.env` (see `.env.example`). Region `ap-southeast-1`.
- **ModelArk:** `ARK_API_KEY`, `APRNTC_POLICY_MODEL` (Seed-2.0-pro), `APRNTC_JUDGE_MODEL`
  (DeepSeek-V4-pro — must differ from policy).
- **VikingDB:** `VIKINGDB_AK`, `VIKINGDB_SK` (+ hosts/region defaulted).
- **Auth (optional):** `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`,
  `APRNTC_SESSION_SECRET` — when set, the dashboard requires Google sign-in.

## Layout
```
src/aprntc/
  config.py        env/.env settings
  byteplus/        VikingDB SigV4 signer (the gate) + ModelArk client
  trajectory/      canonical schema + SQLite store + Postgres store + PII scrub
  providers/       LLMProvider seam (model-agnostic)
  tap/             AgentTap + collectors (sdk_wrapper, egress_proxy, otel, mcp, a2a)
  eval/            recused judge, outcome scorers, health, learned fusion
  memory/          MemoryStore + VikingDB REST adapter + MMR
  distill/         playbook + diff, distiller, child runtime
  promote/         gate (Wilson CI) + lineage + auto-promotion policy + audit log
  online/          shadow runner + canary controller
  fleet/           multi-agent registry + cross-agent lesson sharing
  serving/         playbook registry + config-fetch API (external integration)
  tenancy/         API-key auth + per-tenant isolation
  auth/            Google sign-in (session, users, OAuth providers)
  ops/             scheduler, retry, self-metrics
  demos/           synthetic demos + the real BytePlus support agent
  web/             FastAPI app (API + serves the built UI)
web/               React + Vite + Tailwind dashboard (7 screens, light/dark)
scripts/           live demos (one per collector path) + the scheduler runner + KB indexer/eval
docs/              ONBOARDING, DESIGN, DESIGN_AND_SOLUTION, ROADMAP, STATUS, PRODUCTION, DEPLOY, decisions/ (ADRs)
tests/             348 tests (unit + signing oracle + offline integration; +13 Postgres-gated)
```

## Tests
```bash
.venv/bin/python -m pytest              # full suite (348 + 13 Postgres-gated when env set)
.venv/bin/python -m pytest -m oracle    # signing byte-for-byte vs the volcengine SDK

# Postgres parity (needs a running Postgres — e.g. docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=test postgres:16)
APRNTC_TEST_PG_URL="postgresql://postgres:test@localhost:5432/postgres" \
  .venv/bin/python -m pytest tests/test_pg_store.py -v
```

## Documentation
- `docs/ONBOARDING.md` — **5-min customer onboarding**: pick a collector (LiteLLM / OTel / MCP / SDK) + wire it in (5 lines)
- `docs/DESIGN_AND_SOLUTION.md` — the full design & solution write-up (the source of truth for *what* + *why*)
- `docs/PRODUCTION.md` — how aprntc connects to a real external agent in production
- `docs/DEPLOY.md` — single-container deploy, Postgres + multi-worker setup
- `docs/ROADMAP.md` / `docs/STATUS.md` — what's planned / what's done
- `docs/decisions/` — 8 ADRs (the load-bearing decisions + rejected alternatives)
- `docs/design/` — the design & solution document in HTML and Word (with diagrams)
- `docs/research/` — **the technical report** (`aprntc-paper.pdf` / `.html` / `.md`): aprntc framed as
  outcome-anchored apprenticeship distillation, in the style of an LLM model paper

## License
MIT.
