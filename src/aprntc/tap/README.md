# Tap collectors

`AgentTap` captures a parent agent's behavior into the canonical `Trajectory`
(ADR 0004, 0007). Collectors intercept at stable protocol boundaries and all
normalize into an `Episode` → a sink (e.g. `TrajectoryStore.put_episode`).

| Collector | File | For | Fidelity | External dep |
|---|---|---|---|---|
| SDK wrapper | `sdk_wrapper.py` | agents we own (our demos) | full | none |
| Egress proxy | `egress_proxy.py` | closed-source agents | inferred (model traffic) | LiteLLM (`[proxy]`) |
| OTel ingester | `otel_ingest.py` | OTel-instrumented frameworks | partial | OTel Collector (`[otel]`) |
| MCP interceptor | `mcp_ingest.py` | MCP-tool agents | full (tools) | MCP gateway, e.g. ContextForge |
| A2A interceptor | `a2a_ingest.py` | multi-agent systems over A2A | coarse (task/artifact) | A2A server/SDK (`[a2a]`) |

Design: each collector is a **pure normalizer** (`normalize.py` is shared) — the
external service only delivers raw dicts, so the mapping logic is fully unit-tested
offline. We never hard-import the external libs; the collector degrades gracefully
if the extra isn't installed.

## Wiring (production)
- **Egress proxy:** run a self-hosted LiteLLM gateway; point the agent's model
  `base_url` at it; register `make_proxy_logger(store.put_episode)` via
  `litellm.callbacks`. Closed-source agents need zero code change.
- **OTel:** run the OpenTelemetry Collector; have it export GenAI spans to a small
  endpoint that calls `spans_to_episodes(spans)` → sink. Source instrumentation:
  OpenLLMetry / OpenInference (free, OSS).
- **MCP:** run an OSS MCP gateway (ContextForge) between agent and MCP servers;
  feed its tool-call logs to `mcp_records_to_episode(...)` → sink.
- **A2A:** sit an A2A server/proxy in front of the agents; feed each completed
  task to `a2a_task_to_episode(task)` → sink. Coarsest tap — task/artifact
  granularity; the remote agent's internal tool calls are not visible to the caller.

Cross-collector correlation (model stream + tool stream → one trajectory) is
**best-effort** by design (ADR 0005) — aprntc learns from response *quality*, not
exhaustive tracing.

## End-to-end integration recipe (egress proxy)

Live-verified reference: [`scripts/demo_external_agent.py`](../../../scripts/demo_external_agent.py).
A small "external customer" agent (no aprntc imports beyond `make_proxy_logger`)
fetches its system prompt from the aprntc Playbook Registry, makes a LiteLLM
model call, and the trajectory lands in the store with `collector=egress_proxy`.

The five lines an external customer agent adds to wire themselves in:

```python
import litellm
from aprntc.tap.egress_proxy import make_proxy_logger
from aprntc.trajectory import TrajectoryStore

store = TrajectoryStore("aprntc.db")           # or your tenant's DB path
litellm.callbacks = [make_proxy_logger(store.put_episode)]   # one line
# … now every litellm.completion(...) is recorded as an egress_proxy Episode.
```

Plus the one HTTP fetch on each request (B0 config-fetch API):
```
GET /api/playbooks/{agent_id}/active   →  rendered_prompt
```

Run the demo (needs `ARK_API_KEY`/`APRNTC_POLICY_MODEL` in `.env`, a running
aprntc server, and the `[proxy]` extra):

```
.venv/bin/python scripts/demo_external_agent.py --aprntc http://localhost:8000
```

The script verifies the trajectory landed and prints the captured Episode's
metadata (collector, model, latency, tokens). Live-verified: real ModelArk
call → egress_proxy Episode → visible in `/api/trajectories?collector=egress_proxy`.
