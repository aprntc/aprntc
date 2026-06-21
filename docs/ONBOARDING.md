# aprntc — Customer onboarding (end-to-end)

Wire your existing agent into aprntc in ~5 minutes. The choice of integration path
depends on how your agent talks to its model and tools:

| Your agent uses … | Pick this path | Demo script |
|---|---|---|
| LiteLLM (or proxies model traffic through one) | **LiteLLM proxy collector** | `scripts/demo_external_agent.py` |
| OpenTelemetry / OpenLLMetry / OpenInference instrumentation | **OTel ingester** | `scripts/demo_otel_agent.py` |
| MCP tools through a gateway (e.g. ContextForge) | **MCP interceptor** | `scripts/demo_mcp_agent.py` |
| Our SDK directly in your own code | **SDK wrapper** | `scripts/demo_agents.py` |

All four paths land Episodes in the **same** TrajectoryStore — you can mix collectors
freely. The dashboard surfaces what came in via which path (Trajectories → filter chips).

> **Invariant — aprntc never sits in your request path.** Collectors are async +
> fail-open. If aprntc is down, your agent's response is unaffected.

---

## Prerequisites (one-time)

1. **Install aprntc** with the extras you need:
   ```bash
   pip install 'aprntc[byteplus,web]'                  # core + dashboard
   pip install 'aprntc[byteplus,web,proxy]'            # + LiteLLM collector
   pip install 'aprntc[byteplus,web,otel]'             # + OTel collector
   pip install 'aprntc[byteplus,web,postgres]'         # + Postgres backend for multi-worker
   pip install 'aprntc[all]'                           # everything
   ```

2. **Run the aprntc server** (single container — see [DEPLOY.md](DEPLOY.md)):
   ```bash
   docker compose up --build       # → http://localhost:8000
   ```

3. **Register your agent's initial playbook (G0)** via the config-fetch API. From
   your agent's environment, call once:
   ```bash
   curl -X POST http://aprntc:8000/api/playbooks/$AGENT_ID/register \
     -H "Content-Type: application/json" \
     -d '{"system_prompt": "<your current system prompt verbatim>"}'
   ```
   This becomes the agent's G0 (the parent). Future generations get promoted on top
   via the dashboard's Promotion-review screen.

---

## Path 1 — LiteLLM proxy collector (closed-source agents)

If your agent already uses LiteLLM, this is the smallest change. **5 lines:**

```python
import litellm
from aprntc.tap.egress_proxy import make_proxy_logger
from aprntc.trajectory import TrajectoryStore

store = TrajectoryStore("aprntc.db")        # or your Postgres URL
litellm.callbacks = [make_proxy_logger(store.put_episode)]
```

Every `litellm.completion(…)` call from your agent now writes an Episode with
`collector=egress_proxy`. Your agent code that calls litellm doesn't change.

**Fetch the active playbook on each request** (or cache for N minutes):
```python
import requests
active = requests.get(f"http://aprntc:8000/api/playbooks/{AGENT_ID}/active").json()
system_prompt = active["rendered_prompt"]   # use this as your system message
```

Promotion in the dashboard flips what this returns — no redeploy.

**Reference demo:** [`scripts/demo_external_agent.py`](../scripts/demo_external_agent.py)
— live-verified end-to-end (real ModelArk call → Episode `ep_b4857cab…`).

---

## Path 2 — OpenTelemetry ingester (instrumented agents)

If your agent is auto-instrumented (LangChain via OpenLLMetry, OpenInference,
CrewAI, LlamaIndex, …) the model calls already emit OTel spans with GenAI
attributes. You feed them to the ingester.

**Two steps:**

1. **Point the OpenTelemetry Collector** at aprntc (or run an in-process
   exporter that calls aprntc directly):
   ```python
   from aprntc.tap import spans_to_episodes
   from aprntc.trajectory import TrajectoryStore

   store = TrajectoryStore("aprntc.db")

   # Called from your OTel exporter / batch processor:
   def on_export(batch_of_span_dicts):
       for ep in spans_to_episodes(batch_of_span_dicts):
           store.put_episode(ep, scrub=False)
   ```

2. **Span attributes** — `spans_to_episodes` reads a tolerant union of
   OpenLLMetry (`gen_ai.*`), OpenInference (`llm.*`), and Traceloop attributes:
   - `gen_ai.request.model` / `llm.model_name`
   - `gen_ai.prompt` / `llm.input_messages`
   - `gen_ai.completion` / `llm.output_messages`
   - `gen_ai.usage.{input,output}_tokens`

Episodes land with `collector=otel`. Fidelity is `partial` (we see what your
instrumentation emits, not local tool execution — pair with the MCP path below
if you also want tool steps).

**Reference demo:** [`scripts/demo_otel_agent.py`](../scripts/demo_otel_agent.py)
— live-verified: real OTel SDK span → Episode `ep_f693255992a7…`.

---

## Path 3 — MCP interceptor (MCP-tool agents)

If your agent's tools are MCP-based, run an OSS MCP gateway between the agent
and its MCP servers (e.g. IBM ContextForge). The gateway logs every call+result;
feed those logs to the ingester.

```python
from aprntc.tap import mcp_records_to_episode
from aprntc.trajectory import TrajectoryStore

store = TrajectoryStore("aprntc.db")

# Called by your gateway log subscriber (one batch per task / per session):
def on_gateway_records(records, *, task_input, trace_id=None):
    ep = mcp_records_to_episode(records, task_input=task_input, trace_id=trace_id)
    if ep is not None:
        store.put_episode(ep, scrub=False)
```

The ingester accepts a tolerant union of attribute names — same record any
gateway log produces:
```python
{
  "tool.name":      "kb_lookup",
  "tool.arguments": {"query": "refund policy"},
  "tool.result":    "Refunds within 30 days …",
  "duration_ms":    12.3,
}
```

Episodes land with `collector=mcp`. This is the **highest-fidelity tool path**
— you see the direct tool boundary (name + args + result + timing).

**Reference demo:** [`scripts/demo_mcp_agent.py`](../scripts/demo_mcp_agent.py)
— live-verified against the real MCP SDK.

---

## Path 4 — SDK wrapper (you control the agent code)

If you're writing the agent in our process, just wrap the provider:

```python
from aprntc.byteplus.modelark import ModelArkClient
from aprntc.tap import AgentTap, wrap
from aprntc.trajectory import Collector, TrajectoryStore

store = TrajectoryStore("aprntc.db")
tap = AgentTap(store.put_episode, collector=Collector.SDK_WRAPPER)
tapped_provider = wrap(ModelArkClient(...), turn_provider=lambda: ...)
```

Highest fidelity — direct visibility into the model + tool loop.

---

## What you get after wiring

Once trajectories are flowing in, the dashboard at `http://aprntc:8000` shows:

- **Trajectories** — every Episode, filterable by collector. 👍/👎 each answer.
- **Try an agent** — drive the BytePlus support agent against your KB live.
- **Promotion review** — once a child has been distilled and gated, approve or
  reject (with the auto-promotion verdict + per-guardrail reasons in view).
- **Lineage** — the generation DAG (G0 → G1 → …) with one-click rollback.
- **Lessons** — the distilled lessons in Experience Memory (VikingDB).
- **Fleet** — many agents under one apprentice (each its own lineage).
- **Shadow & canary** — live shadow win-rate + CI + canary stage when wired
  for online evaluation.

---

## End-to-end loop you'll see in production

```
your live agent (any path above)
  → Episode lands in TrajectoryStore  (collector= sdk_wrapper | egress_proxy | otel | mcp)
  → 👍/👎 + outcome anchors accumulate as labels
  → nightly Distiller mines lessons      (Experience Memory in VikingDB)
  → Child runtime builds candidate playbook (G+1) with retrieved lessons
  → Offline / shadow gate evaluates child vs parent (recused judge)
  → Promotion review: human (or A4 auto when guardrails clear) promotes
  → External agent's next GET /api/playbooks/<id>/active fetches the new prompt
  → Loop repeats
```

The single point of integration on your side is the **playbook fetch** — every
collector path feeds the same loop. Promotion flips what `/active` returns;
your agent picks up the new prompt on the next request (or after your cache TTL).

---

## Multi-tenancy (B2)

If you're hosting aprntc as a service for multiple customers, enable the
TenantResolver and issue API keys (see [tenancy/auth.py](../src/aprntc/tenancy/auth.py)).
Each tenant gets isolated storage paths; the playbook + dashboard endpoints
require `X-API-Key` / `Bearer` (or a signed session cookie for the human
dashboard via Google OAuth).

---

## Where to look next

- **[EXAMPLE_DEPLOYMENT.md](EXAMPLE_DEPLOYMENT.md)** — end-to-end walkthrough using
  the ShopMate e-commerce agent (provision → register → run → distill → promote → rollback)
- [DEPLOY.md](DEPLOY.md) — single-container deploy, Postgres + multi-worker setup
- [PRODUCTION.md](PRODUCTION.md) — the "aprntc sits beside, not in front" mental model
- [`tap/README.md`](../src/aprntc/tap/README.md) — collector internals + fidelity table
- [STATUS.md](STATUS.md) — what's live-verified end-to-end (egress proxy, OTel, MCP all proven)
