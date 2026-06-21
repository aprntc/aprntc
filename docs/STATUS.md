# STATUS — aprntc build log

> Updated at the end of every stage. A fresh session reads this to know exactly where to resume.
> Format per stage: what's DONE, what's NEXT, and any LIVE FINDINGS discovered during the work.

---

## Stage 0 — Skeleton + VikingDB signing gate ✅ DONE (2026-06-13)
**Done:**
- `aprntc` package under `src/` with dependency-light core + optional extras (`pyproject.toml`).
- `config.py` — stdlib `.env` loader + ModelArk/VikingDB settings (enforces judge ≠ policy).
- `byteplus/signing.py` — Volcengine **Signature-V4** signer (REST, no runtime SDK). **THE gate.**
- `byteplus/modelark.py` — thin OpenAI-compatible ModelArk client (seed of `LLMProvider`).
- `scripts/smoke_byteplus.py` — live gate.
- Tests: **14 passing**, incl. 5 **oracle** tests (byte-for-byte vs `volcengine` SDK).
- Branch `stage-0-skeleton` pushed; merged (ff) into `main`.

**Verified:**
- Offline: 14/14 tests green; signer == SDK byte-for-byte.
- Live: ModelArk policy (`ep-20260406222234-wpzvn`) + judge (`ep-20260613201727-5twvg`) both answer,
  judge ≠ policy confirmed. VikingDB signed call **accepted** (HTTP 400 wrong-shape, NOT 403).

**Live findings:**
- VikingDB **control plane needs `Action=`-style query params**, not RESTful paths (signed POST to
  `/api/v2/collection/list` → 400 "missing Action parameter" = signature OK, shape wrong). Data plane
  uses fixed paths. → matters for Stage 5 (VikingDB memory adapter).
- This `volcengine` SDK's `SignerV4.sign()` has no timestamp-pin kwarg; oracle test signs via SDK then
  aligns our `now` to the SDK's stamped `X-Date`.

**Next:** Stage 1.

---

## Project infrastructure — context + regression guards ✅ DONE (2026-06-13)
**Done:** `CLAUDE.md` (auto-loaded orientation), `docs/DESIGN.md` (canonical design), `docs/decisions/`
(ADRs), this `STATUS.md`, GitHub Actions CI (pytest on push/PR), `.claude/settings.json` PostToolUse
test hook. (Branch `chore/project-docs-ci`.)

---

## Stage 1 — Trajectory schema + Trajectory Store ✅ DONE (2026-06-13)
**Part 1 — schema:** designed (dedicated pass), locked as **ADR 0007**, implemented in
`src/aprntc/trajectory/schema.py` (stdlib dataclasses: Episode/Turn/Step/ContentPart/Label/Outcome
+ enums). Media **by reference**; `schema_version`; `partial` flags; per-step `source_fidelity`;
`pii_status`. 19 round-trip/edge-case tests.

**Part 2 — Trajectory Store + PII scrubber:**
- `src/aprntc/trajectory/pii.py` — regex PII scrubber (email/phone/card/SSN/IP/secrets), recurses into
  tool args/results; runs at ingest; idempotent; non-mutating. (Swappable for NER later, same entry point.)
- `src/aprntc/trajectory/store.py` — SQLite system of record: `put_episode` (scrub-at-ingest default),
  append-only `attach_label`/`attach_outcome`, `get_episode`/`labels_for`/`outcomes_for`/`query`/`count`,
  **`fused_reward`** (confidence-weighted, outcome>explicit>implicit>judge per ADR 0006),
  **`delete_by_subject`** (GDPR) + **`purge_expired`** (retention TTL).
- **Verified:** 55 tests total, all green (incl. 7 PII + 15 store). Episode body immutable; labels/
  outcomes attach over time; scrub proven before persistence; subject-delete cascades; TTL purge works.

**Live findings:** none (offline stage).
**Next:** Stage 2 — `AgentTap` + normalizer + collectors.

---

## Stage 2 — AgentTap + normalizer + SDK-wrapper collector ✅ DONE (2026-06-13)
**Scope note:** built the "richest-first" slice that's independently verifiable offline — the tap core
+ the **SDK wrapper** (full-fidelity, our-code path). The external collectors (LiteLLM proxy, OTel,
MCP/ContextForge) need the demo agents (Stage 3) + live services to integration-test, so they plug in
behind the same `AgentTap` core later. (ADR 0008.)

**Done:**
- `providers/base.py` — `LLMProvider` Protocol + `CompletionResult` (the model-agnostic seam; ModelArk
  client already matches it).
- `tap/core.py` — `AgentTap` / `EpisodeRecorder` / `TurnRecorder`: accumulate turns+steps, normalize to
  an `Episode`, emit to a sink (e.g. `TrajectoryStore.put_episode`). **Fail-open** (sink errors never
  reach the parent; `on_error` hook); context-manager marks `partial` on exception.
- `tap/sdk_wrapper.py` — `wrap(provider, turn_provider=…)`: transparent LLMProvider wrapper recording
  each completion as a full-fidelity `model_call` step (messages, reasoning, tool_calls, tokens, timing);
  records errors then re-raises unchanged; recording failures can't break the call.
- **Verified:** 67 tests total (+12). Incl. end-to-end tap → real store with scrub-at-ingest; fail-open
  proven (sink throws / resolver throws → call + finish still succeed).

## Stage 3 — Two demo agents (synthetic data), tapped ✅ DONE (2026-06-13)
**Done:**
- `demos/corpus.py` — synthetic, zero-PII data: support KB + ORDERS; RAG `DOCS` corpus + frozen `GOLD`
  Q&A set (reference answers + expected cited doc ids; the promotion-gate ruler, never trained on) +
  tiny lexical `search_docs`.
- `demos/agents.py` — `DemoAgent` base (tapped model→tool loop) + `SupportAgent` (kb_lookup,
  order_status tools) + `RagAgent` (search_docs retrieval, cites doc ids). Provider-agnostic.
- `scripts/demo_agents.py` — live runner (real ModelArk → SQLite store).
- **Verified offline:** 73 tests (+6); agents record complete trajectories (tool_call + model_call
  steps, reasoning, context injection); partial episode on provider error.
- **Verified LIVE (real ModelArk):** both agents answered correctly; RAG cited the expected doc on all
  3 gold questions; 5 episodes stored — `sdk_wrapper`, **pii=scrubbed**, real endpoint id, model_call
  tokens + reasoning captured. Stages 0→3 compose end-to-end on live infra. ✅

## Stage 4 — Evaluation / Labeling ✅ DONE (2026-06-13)
**Done:**
- `eval/judge.py` — `PairwiseJudge` (recused; constructor RAISES if judge==policy). Order-randomization
  de-bias (child A/B slot de-mapped back), rubric + strict-JSON output (robust parse), → `judge` Label.
- `eval/outcomes.py` — the ANCHOR (deterministic, no LLM): `support_outcome` (grounded + answered, not
  punted) and `rag_outcome` (citation + retrieval + reference-match vs gold) → `outcome` Labels.
- `eval/health.py` — `judge_reference_agreement` (drift kill-switch signal) + `reward_hacking_alarm`
  (judge↑ while outcome flat).
- Fusion reuses the store's `fused_reward` (outcome > judge), proven anchored.
- **Verified offline:** 90 tests (+17). **Verified LIVE:** DeepSeek-V4-pro judge picked the better
  answer AND position-de-bias resolved correctly (child in slot B → winner=child, score 1.0).

**Next:** Stage 5 — Experience Memory (VikingDB REST adapter; control plane uses `Action=` params).

## Stage 5 — Experience Memory (VikingDB) ✅ LIVE WORKING (2026-06-13, after fixes)
**RESOLVED — the data-plane 403 was `service="air"`; the V2 API uses `service="vikingdb"`.**
The control/data plane V2 calling-process docs were the key. Our oracle test gave false confidence
(we fed the SDK the same wrong "air", so they agreed while both wrong vs the live server). Live API =
the real oracle. Also: V2 upsert key is **`data`** (not `fields`); collection uses **server-side
vectorize** (skylark-embedding-vision-251215, 2048-dim) → upsert TEXT in `situation`, query by TEXT via
`/api/vikingdb/data/search/multi_modal` (not raw dense_vector).

**LIVE VERIFIED:** auth OK (service=vikingdb), upsert OK (3 lessons, server-side embedded), semantic
search OK — "how do refunds work?" ranked the refund lesson top (0.615). Adapter refactored to the
text-vectorize shape; `service` default fixed to "vikingdb"; collection/index names default to
ankur_aprntc_collection / ankur_aprntc_index, dim 2048. 104 tests green.

**FULLY LIVE-VERIFIED (2026-06-13):** scalar fields added to the index (console) → FILTERED retrieval
works: query "how do refunds work?" + min_reward=0.8 → refund directive (0.617), order-status (0.7)
correctly excluded. Also verified: stable content-derived lesson ids (re-upsert updates, not
duplicates — `content_lesson_id`), and `/api/vikingdb/data/delete` by ids. Stage 5 = DONE, all ops
(auth, upsert/server-side-embed, filtered hybrid search, dedup, delete) proven on live infra. 105 tests.

### (historical) earlier diagnosis — kept for context
## Stage 5 — Experience Memory (VikingDB) ⏳ OFFLINE DONE, LIVE BLOCKED (2026-06-13)
**Done (offline, 104 tests green):**
- `memory/base.py` — `MemoryStore` Protocol + `Lesson`/`LessonType`/`RetrievedLesson`.
- `memory/mmr.py` — cosine + MMR diversification (pure).
- `memory/vikingdb.py` — REST adapter: control-plane `Action=` calls (CreateVikingdbCollection/Index),
  data-plane fixed paths (upsert/search/vector), `dense_weight` hybrid + recursive filter DSL,
  `pii_status=scrubbed` always enforced, 100-row upsert batching, MMR over candidates. **HTTP transport
  injectable** → request construction + parsing tested against the REAL adapter offline.
- 14 new tests (104 total). Signing still byte-for-byte == SDK oracle.

**LIVE BLOCKER — VikingDB data-plane auth (needs user/account action):**
- Control plane ACCEPTED our signature (returned business error `InvalidAction` code 100008 with
  authenticated ResponseMetadata) → signing + creds valid there, but **Action/Version string wrong**
  for this region (confirm exact V2 Action names + Version from console/docs).
- Data plane REJECTED the SAME signer: `403 AccessDenied "check signature failed"` on BOTH candidate
  hosts (`api-vikingdb.vikingdb.ap-southeast-1.bytepluses.com` and `api-vikingdb.mlp.ap-mya.byteplus.com`).
  Our signer is byte-for-byte == volcengine SDK (oracle), and the control plane accepts it → NOT a code
  bug. Likely: (1) data plane = a separately-provisioned VikingDB *instance* needing console
  setup/instance-specific credential; (2) credential type mismatch (VIKINGDB_AK is 47ch prefix `AKAP`,
  SK 59ch — may be instance-scoped); (3) a required header/param specific to the data gateway.
- NOTE: Stage-0 only ever tested the CONTROL host (got "missing Action"), never the data host — so no
  contradiction; this is the first real data-host auth test.
- `scripts/demo_memory.py` written (creates collection+index, upserts, hybrid search) — re-run once the
  data-plane credential/provisioning is sorted.

**Update (2026-06-13):** corrected VIKINGDB_SK (now 60ch, base64 `=`-terminated) — data plane STILL
403s. **Definitive test:** signed the same data-plane request with the OFFICIAL volcengine SDK and sent
it → also `403 AccessDenied`. So this is conclusively NOT our code (vendor SDK fails identically); it is
account/provisioning. Decision: **user creates the collection + index from the BytePlus Console**
(also resolves the control-plane `InvalidAction`, since the instance/Action/Version come from the console).

**Need from console after creation (to finish live verify):** (1) exact collection + index names,
(2) data-plane host/endpoint for the instance, (3) any instance-scoped AK/SK, (4) field schema incl.
vector field name + dim. Then re-run `scripts/demo_memory.py` (adjust collection/index/dim to match).

**Next:** proceed to Stage 6 (Distillation + Child runtime) against the proven MemoryStore interface in
parallel; finish VikingDB live verify when console provisioning is done.

## Stage 6 — Distillation + Child runtime ✅ DONE (2026-06-13) — LOOP CLOSED
**Done:**
- `distill/playbook.py` — `Playbook` (system prompt + exemplars + directives + watch_out, content-
  addressed hash, token-budgeted render) + `PlaybookDiff` (attributable: provenance lesson_id→item;
  `apply` increments generation + dedups + caps change/gen; `revert` = single-lesson rollback).
- `distill/distiller.py` — buckets scored episodes by fused reward (success/failure), mines lessons via
  LLM, success→directives / failure→watch_out, builds an attributable diff.
- `distill/child.py` — `ChildAgent`: clone of parent driven by the Playbook (rendered system prompt) +
  optional memory retrieval (records a `memory_retrieve` step; best-effort — memory errors don't break it).
- 12 new tests (117 total). 
- **LIVE END-TO-END (`scripts/demo_loop.py`):** parent runs → episodes scored (support_outcome) →
  distiller mines 3 lessons → upserted to VikingDB → child G1 built (playbook diff applied) → child
  retrieves from memory at inference. The observe→label→distill→improve loop is CLOSED on live infra. ✅

## Stage 7 — Promotion gate + Lineage + UI ✅ DONE (2026-06-13) — MVP COMPLETE
**Done:**
- `promote/stats.py` — Wilson score CI for win-rate + `GateReport` (all-gates-must-hold `passed`).
- `promote/gate.py` — `PromotionGate`: parent vs child on a held-out set, recused pairwise judge,
  order-balanced (alternating A/B slot), acceptance bar (win-rate≥55%, CI-low>50%, loss<10%) + ZERO-
  tolerance hard gates (regression/safety via per-case checkers). Offline replay.
- `promote/lineage.py` — `LineageRegistry`: generation DAG (G0→G1→…), promote appends + advances
  `current`, rollback reverts to parent gen; JSON-persisted.
- `ui/review_app.py` — Streamlit human gate: shows diff + metrics + hard-gate flags, Promote (disabled
  until bar passes) / Rollback; reads a JSON review bundle (decoupled from live calls).
- 15 new tests (132 total).
- **Fix:** ModelArk client default timeout 60s→300s; judge calls now `thinking="disabled"` (judge scores
  from a rubric, doesn't need CoT) — fixed a judge read-timeout on the deep-reasoning DeepSeek model.
- **LIVE END-TO-END (`scripts/demo_promotion.py`):** parent→distill→child(playbook+memory)→GATE→lineage.
  Gate correctly **REJECTED** an underperforming child (win 25%, CI [5%,70%]) and kept parent at G0 —
  the safety gate working as designed. Review bundle written for the UI. ✅

**THE MVP IS COMPLETE.** The full observe→label→distill→evaluate→promote loop runs end-to-end on live
BytePlus infra, with the human-gated acceptance bar protecting against bad promotions.

### Next: the deliverable below (design doc) — UPDATE it to reflect Stage 7 complete.

## BytePlus agent in dashboard + user feedback (2026-06-14)
- **BytePlus support agent in "Try an agent":** now the default agent; runs live (rich config,
  doc-grounded, cites sources). Backend runner extended (`agent_id="byteplus"`, KB cached); listed in
  `/api/agents`.
- **👍/👎 user feedback:** new `POST /api/feedback` attaches a `USER_EXPLICIT` label (up=1.0, down=0.0)
  — explicit feedback OUTRANKS the judge in fusion (ADR 0006), so it's real learning signal. UI: thumbs
  buttons on each answer + "Sources cited" panel.
- **Verified live in browser:** ran "How do I enable deep reasoning in ModelArk?" → correct doc-grounded
  answer (reward 67%); clicked 👍 → persisted as USER_EXPLICIT(1.0), fused reward moved to 0.79. +4 web
  tests (169 total).

## Trajectories-detail feedback (2026-06-14)
👍/👎 now also on the **Trajectories detail view** (expand an episode → thumbs by the final answer),
not just "Try an agent". Reuses `POST /api/feedback` (USER_EXPLICIT label); reflects any prior vote,
refetches to show the updated label + fused reward. Verified live: 👎 → user_explicit(0.0) persisted,
fused reward moved to 0.62. Roadmap sequencing locked: (0)✓ → A1 → A2 → [pause for real BytePlus data]
→ A3 → A4 → A6 → (B); A5 deferred (closed-source LLMs can't be fine-tuned — discuss before any A5).

## A1 — External tap collectors ✅ DONE (2026-06-14)
Three collectors behind the AgentTap core, all normalize → Episode, all unit-tested offline:
- `tap/egress_proxy.py` (LiteLLM CustomLogger; closed-source agents) — `handle_event` pure core +
  `make_proxy_logger` (needs `[proxy]`).
- `tap/otel_ingest.py` (`span_to_episode`/`spans_to_episodes`; GenAI spans, OpenLLMetry+OpenInference).
- `tap/mcp_ingest.py` (`mcp_record_to_step`/`mcp_records_to_episode`; MCP gateway tool logs, full fidelity).
- shared `tap/normalize.py`; `tap/README.md`; +19 tests (188 total).
- Live-verify deferred (each needs its external service); mapping logic proven. → A2 next.

### Postgres backend LIVE-VERIFIED (2026-06-21)
The multi-worker store path is now proven against real Postgres 16 (Docker):
```
APRNTC_TEST_PG_URL="postgresql://postgres:test@localhost:5432/postgres" \
  pytest tests/test_pg_store.py -v
→ 13 passed in 0.37s
```
The full parity suite cleared on the live database — schema auto-creation,
upsert (`ON CONFLICT DO UPDATE`), PII scrub, append-only labels, outcome join,
`counts_by_collector`, fused-reward weighting, `delete_by_subject` cascade, TTL
purge. Customers can now point `APRNTC_DB_URL=postgresql://…` and run uvicorn
with `--workers N` for real concurrency — no SQLite write-lock bottleneck.

### BytePlus RAG quality — semantic retriever shipped (2026-06-21)
ROADMAP "Known issues" flagged keyword retrieval (TF-IDF + title-boost) as topping out
around 1040 chunks. Concrete baseline now measured: **keyword recall@4 = 10/20 (50%)**
on the BytePlus GOLD set (`scripts/eval_byteplus_retrieval.py --keyword-only`).
Misses are the paraphrased-question class: "deep reasoning" → expected `Deepreasoning`
but matched `Pricing`; "auth header" → matched signing docs instead of Chat API; etc.

**Built (offline-verified):**
- `demos/byteplus/kb_semantic.py` — `SemanticKnowledgeBase` mirrors the keyword KB's
  `search(query, k)` interface but uses VikingDB server-side vectorize (skylark
  embedding, same machinery as the lessons memory adapter). Dedicated collection
  schema (chunk_id PK, text=vector field, doc/section/chunk_ord scalar). Drop-in
  swap for the agent — no agent code change. 9 new unit tests (offline via
  RecordingTransport, mirrors `tests/test_memory.py` pattern). 327 tests total.
- `scripts/index_byteplus_kb.py` — one-time indexer. Idempotent; rate-printed.
- `scripts/eval_byteplus_retrieval.py` — A/B recall@k between keyword and
  semantic on the GOLD set; prints per-question hits + disagreements + summary.

**LIVE-BLOCKER (expected, same as Stage 5):** programmatic control-plane create
hits `InvalidActionOrVersion` on this VikingDB instance — the user provisions
the collection from the BytePlus VikingDB console (schema documented in
`demos/byteplus/README.md`). Once provisioned, the indexer + A/B eval run live
without code changes. The keyword baseline (50%) is now the documented number
to beat.
The reference external-agent integration loop is proven on live infra:
- `scripts/demo_external_agent.py` — a small "external customer" program (no aprntc imports beyond
  `make_proxy_logger`) fetches its playbook via HTTP (`GET /api/playbooks/{id}/active`, B0), makes a
  real ModelArk call through `litellm.completion(...)` with the `AprntcProxyLogger` as a callback,
  and verifies the trajectory landed.
- **Verified:** 3.2 s call → episode `ep_b4857cab5b034efe9298ea3ba29e6b4e` captured with
  `collector=egress_proxy`, model_id, latency 3185 ms, tokens 76/178, surfaced in
  `GET /api/trajectories?collector=egress_proxy` (the A1 filter chip now shows
  `egress_proxy: 1` alongside `sdk_wrapper`). Five-line wiring example documented in `tap/README.md`.
- Two new LiteLLM-integration tests confirm the `make_proxy_logger()` return value is a real
  `CustomLogger` subclass + `log_success_event`/`log_failure_event` dispatch correctly against the
  live LiteLLM library (catches contract drift on upgrades). 318 tests total.
- OTel + MCP + A2A live-verify still gated on their external services (separate work).

### A1 — MCP ingester LIVE-VERIFIED end-to-end (2026-06-21)
The MCP tool-boundary path is now proven against the live MCP SDK:
- `scripts/demo_mcp_agent.py` — self-contained: a tiny `FastMCP` server with a
  `kb_lookup` tool, an "external agent" that calls the tool via the live MCP
  SDK, each call+result wrapped as a gateway-style record (the same shape a
  real OSS MCP gateway like IBM ContextForge would log), records flow through
  `mcp_records_to_episode` into the trajectory store with `collector=mcp`.
- **Verified:** real `FastMCP.call_tool` invocation → 1 tool step → Episode
  `ep_759224921ba3…` with `collector=mcp`, full fidelity (tool name, args,
  result text, duration). Surfaces under the A1 filter chip alongside
  `egress_proxy` + `otel` + `sdk_wrapper`.
- Plus an integration test (`test_mcp_ingest_against_real_fastmcp_tool`) that
  exercises `mcp_record_to_step` and `mcp_records_to_episode` against the
  live MCP SDK call shape — catches contract drift on SDK upgrades.

### A1 — OTel ingester LIVE-VERIFIED end-to-end (2026-06-21)
The OTel-instrumented external agent path is also proven:
- `scripts/demo_otel_agent.py` — a self-contained customer program instruments a real
  OpenAI-compatible model call with the OpenTelemetry SDK (OpenLLMetry-style
  `gen_ai.*` attributes), exports finished spans via `InMemorySpanExporter`, feeds
  them to `spans_to_episodes`, and writes the Episodes into TrajectoryStore — same
  data path a real OTel Collector would take in production, condensed into one
  process for the demo.
- **Verified:** 4.8 s real ModelArk call (51 → 290 tokens) → Episode
  `ep_f693255992a7…` captured with `collector=otel` + model_id + task_input +
  final_output + tokens. Surfaces under the A1 filter chip alongside
  `egress_proxy` + `sdk_wrapper`.
- Plus an integration test (`test_otel_ingests_real_sdk_span_via_inmemory_exporter`)
  that exercises `span_to_episode` against the live SDK's `ReadableSpan` — catches
  GenAI-attribute schema drift on SDK upgrades. 347 tests total.

## A2 — Online shadow / A-B canary ✅ DONE (2026-06-14)
`src/aprntc/online/`: `ShadowRunner` (shadow child vs parent on live requests, fail-open + sampled +
position-debiased, live win-rate/CI, `ready_to_promote()`); `CanaryController` (staged rollout
5%→25%→50%→100%, deterministic hash routing, auto-rollback on degradation). +11 tests (199 total).
Live-verify needs real traffic (wire at deploy).

**⏸ AT THE PAUSE (user sequencing):** A0/A0b/(0)/A1/A2 done. Next is A3 (learned fusion weights) — but
user will first do **real testing with the BytePlus support agent** to generate real data. Do NOT start
A3 until the user signals. A5 still deferred (closed-source LLMs; discuss first).

## A3 — Learned fusion weights ✅ DONE (2026-06-14)
`eval/fusion.py` `learn_weights()`: calibrate each label source by its agreement with the anchor
(outcome>human>explicit) on the same episode → auto-down-weight a biased judge (blend + min_n cold-start
guard). `store.fused_reward(weights=...)` takes the learned map; `store.learn_fusion_weights()` learns
from history. +9 tests (211). Verified: a judge contradicting real feedback over 12 eps drops 0.40→0.23.
Activation needs judge+anchor on the same episode; real data so far (21 eps/9 thumbs) has anchors only
→ priors hold safely until shadow/gate runs add judge labels. KB RAG quality flagged for later (ROADMAP
"Known issues"). → A4 next.

## A4 — Auto-promotion ✅ DONE (2026-06-14)
`promote/auto.py` `AutoPromotionPolicy.decide(gate, diff, trust)` → AUTO_PROMOTE / HUMAN_REVIEW /
REJECT. Default-OFF (opt-in). Auto only if ALL: enabled + gate passed + margin above bar (win≥60/
CI>55/loss<5) + zero regression/safety + low-risk additive diff + trust≥0.80. Else HUMAN_REVIEW (never
silent reject); failed gate → REJECT; reports every blocked guardrail. +11 tests (222). → A6 next
(A5 deferred: closed-source LLMs, discuss first). Live-flow/dashboard wiring is a small follow-up.

## A6 — Multi-agent fleets ✅ DONE (2026-06-14)
`src/aprntc/fleet/`: `Fleet`+`AgentRef` (many parents, each own lineage; `by_domain` scoping,
JSON-persisted) + `share_lessons`/`shareable_lessons` (offer one agent's lessons to another — reward
gate, type gate [no source-specific exemplars], dedup by content id, `shared_from` provenance). +9 tests
(231 total). **A-TRACK COMPLETE except A5** (fine-tuning deferred — closed-source LLMs, user to decide
drop vs park). Next: (B) productionization, starting with B0 (Playbook registry + Config-fetch API).
Follow-ups noted: wire A4/A6 + A1/A2 into the live flow/dashboard during B-track.

## B0 — Playbook registry + Config-fetch API ✅ DONE (2026-06-14)
A5 PARKED (open-weight only; n/a to closed-source stack). `src/aprntc/serving/`: `PlaybookRegistry`
(per-agent versions + active pointer; register G0 / infer-from-traffic fallback / promote / rollback,
JSON-persisted) + REST: `POST/GET /api/playbooks/{id}/register|active|rollback`. The OUTBOUND half of
external integration — an external agent fetches its active playbook one-line; promotion flips what's
served (no redeploy). +12 tests (243). Verified live over HTTP (register G0 → fetch active). Remaining
B: auth/multi-tenancy (B2), deploy (B1), ops (B3), UI polish (B4); optional tiny client SDK helper.

## B2 — Auth + multi-tenancy ✅ DONE (2026-06-14)
`src/aprntc/tenancy/`: `TenantStore` (API keys HASHED, create/issue/revoke/rotate/deactivate/auth) +
`TenantResolver` (key → tenant → isolated paths `{root}/{tenant_id}/...`). Web API: optional
`AppState.tenant_resolver` — when set, playbook endpoints require X-API-Key/Bearer + serve per-tenant
isolated registries; when unset, single-tenant/dev mode unchanged. +14 tests (257). Verified isolation
(tenant B can't read tenant A's same-agent_id data). Follow-up: extend tenant scoping to the other
endpoints (trajectories/lineage/lessons/feedback) + dashboard login (B4). Remaining B: B1 deploy, B3 ops.

## B1 — Deploy ✅ DONE (2026-06-14)
Single-container: multi-stage Dockerfile (node builds web/dist → python serves API + static UI on :8000),
docker-compose.yml (.env + /data volume), .dockerignore, docs/DEPLOY.md. `_mount_static` serves the SPA
(APRNTC_STATIC_DIR; optional → dev/tests unaffected; /api/* never shadowed). +3 tests (260). Verified
live: uvicorn serves /, /trajectories (SPA), /api/health, /assets/* on one port. Docker build itself
unrun (Docker not installed here). Remaining B: B3 ops (scheduler/Postgres/observability), B4 UI polish.

## B3 — Robustness/ops ✅ DONE (2026-06-14)
`src/aprntc/ops/`: `Scheduler` (interval jobs = the nightly distill cadence; bg-thread or cron
`run_due()`; fail-isolated; injectable clock), `with_retry` (bounded backoff for flaky ModelArk/VikingDB),
`OpsMetrics`/`METRICS` (thread-safe counters; `/api/ops` health snapshot). `scripts/run_scheduler.py`
runs the live nightly distill loop. +12 tests (272). Remaining: Postgres/multi-worker at scale (noted
in DEPLOY.md), rate/cost controls. **Only B4 (UI polish) left in the planned roadmap.**

## B4 (part 1) — Dashboard auth: Google sign-in (backend) ✅ DONE (2026-06-14)
HUMAN login for the console (distinct from B2 machine API keys). `src/aprntc/auth/`:
`SessionSigner` (HMAC signed cookie sessions, stdlib), `UserStore` (one user = one tenant mapping),
pluggable `OAuthProvider` — `GoogleProvider` (live OIDC) + `MockProvider` (offline tests). Web API:
`/api/auth/config|login|google/callback|me|logout`; wired in `from_env` from GOOGLE_*/APRNTC_SESSION_SECRET
(degrades to open dev mode if unset). +16 tests (288). Verified REAL .env wires google provider (correct
endpoint/scope/redirect/client-id); live "click Sign in with Google" verification is the user's final step.
Decisions: anyone-with-Google can sign in (External); one user = one tenant. User owns aprntc.com Workspace.
**B4 part 2 — frontend ✅ DONE (2026-06-14):** `screens/Login.tsx` (full-screen "Sign in with Google"
gate) + `App.tsx` gates the whole dashboard on `/api/auth/me` when auth is enabled; sidebar shows the
signed-in user (name/avatar/tenant) + Sign out. `api.ts`: authConfig/me/logout. Build clean (45 modules).
**Verified live in-browser:** login gate renders (screenshot), /api/auth/login 307-redirects to the REAL
Google consent screen (correct client_id/redirect/scope), zero console errors. Only the actual Google
click-through is the user's step (needs a real browser session as the test user).
**This completes the planned roadmap (MVP + A-track + B0–B4).** Remaining are optional follow-ups:
tenant-scope the non-playbook endpoints, surface A1/A2/A4/A6 in the UI, at-scale Postgres/workers.

## 📌 DELIVERABLE (user request, 2026-06-13): on Stage 6 completion
When Stage 6 completes, produce a **Design & Solution document** (committed `.md`) — a comprehensive
write-up of the built system: architecture, the closed observe→label→distill→evaluate→promote loop,
component design, data flow, the BytePlus integration (ModelArk + VikingDB), key decisions (link ADRs),
and how the apprentice produces a better child. Target: `docs/DESIGN_AND_SOLUTION.md`. Do NOT skip.

## Web dashboard (React + FastAPI) — ⏳ backend DONE, frontend SCAFFOLDED (2026-06-14)
User wanted a modern, professional UI (Streamlit replaced). Decision: **React + FastAPI**, full
dashboard (4 screens), **light/dark theme toggle**.
- **Backend (DONE, runnable + tested):** `src/aprntc/web/app.py` — FastAPI REST API over the engine:
  `/api/review` (gate report + diff), `/api/lineage` + promote/rollback, `/api/trajectories[/{id}]`,
  `/api/lessons`. Injectable `AppState` (real store/lineage/memory or fakes). New `[web]` extra
  (fastapi+uvicorn). 13 API tests (145 total). **Bug fixed:** TrajectoryStore now `check_same_thread=
  False` (FastAPI services requests on a thread pool — would have broken the live server).
- **Frontend (SCAFFOLDED, ready to run — needs Node):** `web/` — Vite + React + TS + Tailwind, light/dark
  theme tokens (`src/index.css`), typed API client, app shell + sidebar nav, 4 screens (Review, Lineage,
  Trajectories, Lessons). Linear/Vercel-style. **Node not installed on build machine** → `cd web && npm
  install && npm run dev` (proxies /api → uvicorn). Cross-file refs verified by hand (couldn't run tsc).
- Streamlit UI (`src/aprntc/ui/`) retained as the no-Node fallback.

**✅ VERIFIED LIVE (2026-06-14, Node 26 installed):** `npm install` + `npm run build` clean (tsc -b +
vite, 0 errors, 43 modules). Ran backend (uvicorn) + frontend (vite) together: `/api/review` proxy
works, real gate data renders. **Screenshotted the running app** — Promotion review screen shows the
rejected-by-gate verdict, red metrics (25% win/5% CI/75% loss), green hard-gate banners, the 3 distilled
directives with lesson-id provenance, Promote locked / Roll back active. **Both dark AND light themes
verified** via the toggle. Routing works (Trajectories empty-state renders cleanly). Zero console errors.
The dashboard is real and working.

**Persistent data wired (2026-06-14):** `AppState.from_env()` wires a persistent SQLite TrajectoryStore
+ VikingDB memory into the module-level `app` (degrades gracefully if creds absent). `scripts/
seed_dashboard.py` seeds 8 demo episodes + 3 lessons. **Verified live:** Trajectories screen renders all
8 persisted episodes (titles, ids, collector, step counts); Lessons screen returns live VikingDB
semantic search ("refund" → refund lesson at 0.62). 146 tests. Dashboard fully data-backed.

**MERGED to main (2026-06-14):** the web dashboard (FastAPI + React, light/dark, 4 screens, live data).

**"Try an agent" screen added (2026-06-14):** 5th dashboard screen — pick support/RAG agent, type or
click an example question → runs LIVE on ModelArk, shows answer + reward (outcome-scored) + tool steps
+ reasoning, captures the trajectory to the store. Backend: `/api/agents` + `/api/agents/run`,
`AppState.agent_run` wired via `_build_agent_run` (from_env). 5 new tests (151 total). Verified live in
browser: ran "What is your refund policy?" → "Refunds…within 30 days", reward 100%, kb_lookup step
captured. NOTE: these demo agents are the PARENT (G0); answers scored by outcome anchors (support=
grounded+answered; rag=citation/retrieval/reference vs gold). Data in `src/aprntc/demos/corpus.py`.

## Backlog / later stages (per docs/DESIGN.md §8)
- Stage 2 collectors (deferred within stage): LiteLLM proxy → OTel ingester → MCP/ContextForge.
- Stage 3: the two demo agents (support-chat + RAG-Q&A, synthetic data).
- Stage 4: Evaluation/Labeling (pairwise judge, outcome-joiner, fusion).
- Stage 5: Experience Memory (VikingDB REST adapter — remember control-plane `Action=` finding).
- Stage 6: Distillation + Child runtime.
- Stage 7: Promotion gate + Lineage + Streamlit UI.
