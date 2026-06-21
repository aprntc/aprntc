# Example deployment — ShopMate × aprntc, end-to-end

This is the **full production-flavoured workflow** for a customer using
aprntc to improve their parent agent. We use [ShopMate](../examples/ecommerce-agent/README.md)
— a small e-commerce customer-support agent — as the concrete example.
Every step here maps directly onto what a real customer does.

## Architecture (one laptop, two services)

```
   customer browser
        │
        ▼
   ┌─────────────────────┐         HTTP          ┌──────────────────────┐
   │ ShopMate            │ ◄───── playbook ─────│ aprntc server        │
   │  Streamlit :8501    │ ──── trajectory ────►│  FastAPI :8000        │
   │  + OpenAI client    │       feedback        │  + React dashboard   │
   │  + 3 tools          │                       │  + trajectory store  │
   └─────────────────────┘                       │  + VikingDB memory   │
        │                                        └──────────────────────┘
        │ chat                                          ▲
        ▼                                               │ open in
   shopper conversation                                 │ another tab
                                                   You (operator)
```

Two services on two ports. They communicate over HTTP only — no shared
DB, no shared filesystem. This is exactly how a real deployment looks
where the customer's agent runs in their infrastructure and aprntc runs
in yours (or theirs).

---

## Phase 1 — Stand both services up (5 min)

### 1a · Start aprntc

```bash
# In the aprntc repo root:
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[byteplus,web,proxy]'        # core + dashboard + LiteLLM (used elsewhere)
cp .env.example .env                           # fill ARK_API_KEY, APRNTC_POLICY_MODEL, VIKINGDB_*

# Build the React dashboard (one-time)
cd web && npm install && npm run build && cd ..

# Run the aprntc API + UI together on :8000
APRNTC_STATIC_DIR=$PWD/web/dist .venv/bin/python -m uvicorn aprntc.web.app:app --port 8000
# → http://localhost:8000  (sign in with Google if .env has GOOGLE_* set)
```

### 1b · Start ShopMate (in another terminal)

```bash
# Still in the aprntc repo root — ShopMate reuses the same .venv:
.venv/bin/pip install -r examples/ecommerce-agent/requirements.txt
.venv/bin/streamlit run examples/ecommerce-agent/streamlit_app.py
# → http://localhost:8501
```

Open both URLs in your browser. The aprntc dashboard is empty at first —
that's expected.

---

## Phase 2 — Register the agent's initial playbook (zero clicks)

The very first time ShopMate runs, it does:

```http
GET  /api/playbooks/ecommerce-support/active     → 404 (not registered yet)
POST /api/playbooks/ecommerce-support/register   → registers G0
```

This is **automatic** — `examples/ecommerce-agent/agent.py:fetch_or_register_playbook`
seeds the initial system prompt from `G0_SYSTEM_PROMPT` if aprntc returns
404. In a real deployment, the customer either:

- Lets the agent auto-register on first run (what ShopMate does), or
- Pre-registers via a single curl (cleaner for multi-instance deploys):

  ```bash
  curl -X POST http://localhost:8000/api/playbooks/ecommerce-support/register \
    -H "Content-Type: application/json" \
    -d '{"system_prompt": "You are ShopMate, the customer-support agent…"}'
  ```

Open ShopMate's sidebar in the browser → "Active system prompt (fetched
from aprntc)" — the prompt you just registered is what the agent will
use until you promote a new generation.

---

## Phase 3 — Real customer interactions → trajectories flow into aprntc

In the ShopMate UI try a few of these (the README has more):

- `Hi! What's the status of order A1001?`
- `I need to refund my purchase A1003.`
- `Do you have noise-canceling headphones?`
- `Where is my order A1002?`

For each turn, ShopMate:

1. Runs the OpenAI tool loop (model picks tools, executes them locally,
   synthesizes the final answer).
2. Builds a canonical `Episode` (turns + steps + tokens + latency).
3. **POSTs it to aprntc:**
   ```http
   POST /api/trajectories
   { "task_input": "...", "collector": "egress_proxy", "turns": [...], ... }
   → { "episode_id": "ep_…", "fused_reward": null }
   ```

In the aprntc dashboard → **Trajectories** screen, you'll see:

- A new row per turn within ~1 second.
- The `egress_proxy` collector badge (the HTTP boundary).
- Expand a row → full tool-call trace: `search_products(query="…")`,
  `get_order(order_id="A1001")`, etc., with args + results visible.

Rate each answer 👍 or 👎 in either UI — both write the same
`USER_EXPLICIT` label back to aprntc and (via fusion) update the fused
reward shown in the trajectory row.

---

## Phase 4 — Distill lessons from the accumulated trajectories

After ~10-30 real conversations (more is better; outcome anchors need data
to fuse on), kick off distillation. In this demo we drive it from a
script; in production a scheduler runs this nightly (see `scripts/run_scheduler.py`).

```bash
# Sample distillation — produces a Playbook for the ecommerce-support agent
# from the trajectories the store has accumulated.
.venv/bin/python scripts/demo_loop.py        # quick demo (synthetic — not the ecommerce data)

# For the actual ecommerce-support distillation (when you have real ShopMate runs):
# See scripts/run_scheduler.py and adapt the agent_id. The scheduler reads from
# the same TrajectoryStore the dashboard reads.
```

The distiller mines:
- **Concrete exemplars** from high-reward turns (the answers ShopMate got
  right that you liked).
- **Directives** from clusters of similar failures ("when a refund is
  rejected, always state the reason from the result.message field").
- **Failure-patterns** from low-reward turns (the explicit
  user_explicit:0.0 thumbs you flagged).

The result is a new candidate **G1 playbook** — same base system prompt
+ directives + exemplars + watch-outs.

---

## Phase 5 — Review & promote in the dashboard

In the aprntc dashboard → **Promotion review**:

- See the candidate G1 diff (added directives / exemplars / watch-outs)
  with the trajectory ids that motivated each one (provenance).
- The acceptance bar shows win-rate, 95% CI lower bound, loss-rate,
  regression checks, safety checks (offline replay against the held-out
  gold set).
- The **A4 auto-promotion banner** says either "Eligible for auto-promotion"
  (if guardrails clear) or "Needs human review" with the blocked
  guardrails listed.

Click **Promote** when satisfied → aprntc appends G1 to the lineage,
makes it the active playbook in the registry.

The audit log (collapsible panel on the same screen) records the
decision with timestamp, candidate hash, blocked guardrails, and trust
signal.

---

## Phase 6 — ShopMate picks up the new playbook

ShopMate's sidebar has a **"↻ Refresh playbook from aprntc"** button.
Click it (in production: restart the agent / reduce its playbook-cache
TTL).

Or just kick off a new chat session — ShopMate fetches the active
playbook lazily on first start. The "Active system prompt" expander in
the sidebar now shows the new G1 (you'll see additional bullet lines for
directives, watch-outs, etc.).

**This is the loop closed:**
```
ShopMate G0 → real customer interactions → trajectories + 👍/👎
            → distillation mines lessons
            → candidate G1 playbook
            → review + promote (human or A4-auto)
            → ShopMate fetches G1 → next conversation uses it
```

No redeploy. The agent's behaviour just… improved.

---

## Phase 7 — Rollback (one click, if G1 turns out worse)

In the **Lineage** screen click **Rollback** on the candidate. aprntc
moves the active pointer back to G0; ShopMate's next playbook fetch
returns the old prompt. The new generation stays in history (you can
re-promote later after fixes).

---

## What about A1's other collector paths?

ShopMate uses the simplest production-grade path: **HTTP POST per turn**
to `/api/trajectories`. The same agent could equally use:

- **LiteLLM proxy** — if ShopMate switched to `litellm.completion(...)`,
  registering `make_proxy_logger(post_to_aprntc)` as a callback would
  achieve the same effect with one less line of agent code. See
  `scripts/demo_external_agent.py`.
- **OpenTelemetry** — if ShopMate were OTel-instrumented (LangChain via
  OpenLLMetry, OpenInference, etc.), the spans go straight to aprntc's
  ingester. See `scripts/demo_otel_agent.py`.
- **MCP gateway** — if ShopMate's tools were MCP, a gateway (e.g. IBM
  ContextForge) sees every tool call and feeds the records to aprntc.
  See `scripts/demo_mcp_agent.py`.

All four paths land in the **same trajectory store** with the **same
schema** — just different `collector` labels. The dashboard surfaces
them via the collector filter chips so an operator can see at a glance
which path delivered what.

---

## What if aprntc is down?

ShopMate is **fail-open**. If aprntc is unreachable:
- The playbook fetch raises → ShopMate falls back to its embedded G0
  prompt (you'll still get sensible responses).
- The trajectory POST returns `None` → the customer's reply ships
  uninterrupted; only the learning loop misses one data point.

The user-facing experience is never blocked on aprntc — aprntc is
**beside** the agent, not in front of it.

---

## Cleanup

```bash
# Stop streamlit + uvicorn with Ctrl-C in their terminals.
# All data (trajectories, playbooks) lives in aprntc.db (SQLite) by default.
```

---

## Where to go next

- `docs/ONBOARDING.md` — the 5-line wiring per collector path
- `docs/DEPLOY.md` — multi-worker + Postgres for real load
- `docs/PRODUCTION.md` — the "beside, not in front" mental model
- `examples/ecommerce-agent/README.md` — ShopMate's own README with the
  scenario menu
