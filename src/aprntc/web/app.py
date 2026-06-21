"""FastAPI app exposing the aprntc engine to the web dashboard.

Read endpoints serve dashboard data (gate report, lineage, trajectories, lessons);
write endpoints perform the human decisions (promote / rollback). State is held in
an injectable :class:`AppState` so tests drive it with in-memory fakes and the real
server wires the live store / lineage / review bundle.

Design notes:
* No business logic here — endpoints delegate to the engine (`promote`, `trajectory`,
  `memory`). The API is a thin transport, consistent with "the gate computes the bar,
  the UI only displays it and records the human decision".
* CORS is open in dev so the Vite dev server (localhost:5173) can call it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from aprntc.promote.lineage import LineageRegistry
from aprntc.trajectory.store import TrajectoryStore


@dataclass
class AppState:
    """Injectable backing state for the API (real or fake)."""

    lineage_path: str = "lineage.json"
    bundle_path: str = "review_bundle.json"
    store: TrajectoryStore | None = None
    # optional: a memory retriever (callable(query, k) -> list[dict]) for the lessons screen
    memory_search: Any | None = None
    # optional: a runner (agent_id, task) -> dict, for the "Try an agent" screen
    agent_run: Any | None = None
    # B0: per-agent playbook registry external agents fetch from (lazy default in from_env)
    playbooks: Any | None = None
    # B2: optional TenantResolver. When set, the data endpoints (review/lineage/
    # trajectories/lessons/feedback/agents/run) AND the playbook endpoints require auth
    # (X-API-Key, Bearer, or a valid dashboard session cookie) and serve each tenant's
    # ISOLATED data. When None, the API runs single-tenant/dev mode (existing behavior).
    tenant_resolver: Any | None = None
    # Factory that builds a tenant's agent-runner closure over its own TrajectoryStore.
    # Set by from_env (or tests) when an agent runner is configured. Shared client/model/kb
    # are captured by the factory; each call gets a tenant-scoped store + memory.
    agent_run_factory: Any | None = None
    # B4: human dashboard auth (Google sign-in). When all set, /api/auth/* is live and
    # /api/auth/me reflects the logged-in user. When None, dashboard runs open (dev).
    auth_provider: Any | None = None     # OAuthProvider (Google or Mock)
    session_signer: Any | None = None    # SessionSigner
    users: Any | None = None             # UserStore
    # Per-tenant caches (built lazily on first request, kept across requests).
    _tenant_stores: dict[str, TrajectoryStore] = field(default_factory=dict)
    _tenant_memory: dict[str, Any] = field(default_factory=dict)
    _tenant_agent_runs: dict[str, Any] = field(default_factory=dict)

    def tenant_store(self, ctx: Any) -> TrajectoryStore:
        """Cached per-tenant TrajectoryStore — reopens once per tenant per process."""
        tid = ctx.tenant_id
        if tid not in self._tenant_stores:
            self._tenant_stores[tid] = TrajectoryStore(ctx.db_path)
        return self._tenant_stores[tid]

    def tenant_memory(self, ctx: Any) -> Any | None:
        """Cached per-tenant memory_search closure (None if VikingDB unconfigured)."""
        tid = ctx.tenant_id
        if tid not in self._tenant_memory:
            self._tenant_memory[tid] = _build_memory_search(tenant_id=tid)
        return self._tenant_memory[tid]

    def tenant_agent_run(self, ctx: Any) -> Any | None:
        """Cached per-tenant agent runner that writes into the tenant's own store."""
        if self.agent_run_factory is None:
            return None
        tid = ctx.tenant_id
        if tid not in self._tenant_agent_runs:
            self._tenant_agent_runs[tid] = self.agent_run_factory(self.tenant_store(ctx))
        return self._tenant_agent_runs[tid]

    def lineage(self) -> LineageRegistry:
        return LineageRegistry(self.lineage_path)

    def bundle(self) -> dict[str, Any]:
        p = Path(self.bundle_path)
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    @classmethod
    def from_env(
        cls,
        *,
        db_path: str = "aprntc.db",
        lineage_path: str = "lineage.json",
        bundle_path: str = "review_bundle.json",
        load_dotenv: bool = True,
    ) -> "AppState":
        """Wire real persistent backends (SQLite store + VikingDB memory) from config.

        Degrades gracefully: a missing DB file still gives an (empty) store; if VikingDB
        config/creds are absent or httpx isn't installed, ``memory_search`` stays ``None``
        and the lessons screen shows its "not connected" state.
        """
        store = TrajectoryStore(db_path)
        memory_search = _build_memory_search(load_dotenv=load_dotenv)
        agent_run_factory = _build_agent_run_factory(load_dotenv=load_dotenv)
        agent_run = agent_run_factory(store) if agent_run_factory is not None else None
        from aprntc.serving import PlaybookRegistry
        auth_provider, session_signer, users = _build_auth(load_dotenv=load_dotenv)
        return cls(
            lineage_path=lineage_path,
            bundle_path=bundle_path,
            store=store,
            memory_search=memory_search,
            agent_run=agent_run,
            agent_run_factory=agent_run_factory,
            playbooks=PlaybookRegistry("playbooks.json"),
            auth_provider=auth_provider,
            session_signer=session_signer,
            users=users,
        )


def _build_memory_search(*, load_dotenv: bool = True, tenant_id: str | None = None) -> Any | None:
    """Return a ``(query, k) -> list[dict]`` retriever backed by VikingDB, or None.

    When ``tenant_id`` is set, use a tenant-specific collection name so each
    tenant's lessons are isolated. The collection must exist (provisioned out
    of band); if missing, retrieval returns the "no results" path and the
    lessons endpoint reports the error rather than 500-ing.
    """
    try:
        from aprntc.config import Settings
        from aprntc.memory.vikingdb import VikingDBMemoryStore

        settings = Settings.from_env(dotenv=".env" if load_dotenv else None)
        settings.vikingdb.validate()  # raises if creds missing
        if tenant_id:
            collection = f"{tenant_id}_aprntc_collection"
            index = f"{tenant_id}_aprntc_index"
        else:
            collection, index = "ankur_aprntc_collection", "ankur_aprntc_index"
        mem = VikingDBMemoryStore(
            settings.vikingdb,
            collection=collection,
            index=index,
            dim=2048,
        )
    except Exception:
        return None

    def search(query: str, k: int) -> list[dict[str, Any]]:
        results = mem.retrieve(query=query or "lesson", k=k, min_reward=0.0)
        return [
            {
                "lesson_id": r.lesson.lesson_id,
                "content": r.lesson.content,
                "lesson_type": r.lesson.lesson_type.value,
                "reward": r.lesson.reward,
                "score": r.score,
            }
            for r in results
        ]

    return search


def _build_agent_run_factory(*, load_dotenv: bool = True) -> Any | None:
    """Return a ``(store) -> runner`` factory backed by live ModelArk, or None.

    The expensive setup (ModelArk client, model id, BytePlus KB) is captured once
    and reused; each call to the factory binds a specific TrajectoryStore. Used by
    both single-tenant (one shared store) and multi-tenant (one runner per tenant) modes.
    """
    try:
        from aprntc.byteplus.modelark import ModelArkClient
        from aprntc.config import Settings

        settings = Settings.from_env(dotenv=".env" if load_dotenv else None)
        settings.modelark.validate()  # raises if keys missing
        client = ModelArkClient(settings.modelark)
        model = settings.modelark.policy_model
    except Exception:
        return None

    # Build the BytePlus knowledge base lazily once (it's ~641 chunks; reused across runs).
    _kb_cache: dict[str, Any] = {}

    def _byteplus_kb() -> Any:
        if "kb" not in _kb_cache:
            from aprntc.demos.byteplus import build_kb
            _kb_cache["kb"] = build_kb()
        return _kb_cache["kb"]

    def factory(store: TrajectoryStore) -> Any:
        return _make_agent_run(store, client, model, _byteplus_kb)

    return factory


def _build_agent_run(store: TrajectoryStore, *, load_dotenv: bool = True) -> Any | None:
    """Return an ``(agent_id, task) -> dict`` runner — single-store convenience.

    Thin wrapper over the factory above for the legacy dev-mode path; behaves
    exactly like the original (the factory is the new shape, this preserves callers).
    """
    factory = _build_agent_run_factory(load_dotenv=load_dotenv)
    return factory(store) if factory is not None else None


def _make_agent_run(store: TrajectoryStore, client: Any, model: str, kb_fn: Any) -> Any:
    """Build the agent-run closure for one TrajectoryStore (shared or per-tenant)."""

    def run(agent_id: str, task: str) -> dict[str, Any]:
        from aprntc.demos.agents import RagAgent, SupportAgent
        from aprntc.demos.corpus import GOLD
        from aprntc.eval.outcomes import rag_outcome, support_outcome
        from aprntc.tap import AgentTap
        from aprntc.trajectory import Collector

        tap = AgentTap(store.put_episode, collector=Collector.SDK_WRAPPER)
        cited: list[str] = []

        if agent_id == "byteplus":
            from aprntc.demos.byteplus import ByteplusSupportAgent, GOLD as BP_GOLD, byteplus_outcome
            agent = ByteplusSupportAgent(client, tap, kb_fn(), model=model, rich=True)
            res = agent.run(task, generation_id="try")
            ep = store.get_episode(res.episode_id)
            cited = res.cited
            gold = next((g for g in BP_GOLD if g.question.lower() == task.lower()), None)
            label = byteplus_outcome(ep, gold) if gold else None
        elif agent_id == "support":
            agent = SupportAgent(client, tap, model=model)
            res = agent.run(task, generation_id="try")
            ep = store.get_episode(res.episode_id)
            label = support_outcome(ep)
        else:
            agent = RagAgent(client, tap, model=model)
            res = agent.run(task, generation_id="try")
            ep = store.get_episode(res.episode_id)
            gold = next((g for g in GOLD if g.question.lower() == task.lower()), None)
            label = rag_outcome(ep, gold) if gold else support_outcome(ep)

        if label is not None:
            store.attach_label(res.episode_id, label)

        steps = [
            {
                "type": s.type.value,
                "tool_name": s.tool_name,
                "tool_args": s.tool_args,
                "duration_ms": round(s.duration_ms, 1) if s.duration_ms else None,
            }
            for turn in ep.turns
            for s in turn.steps
        ]
        return {
            "answer": res.answer,
            "episode_id": res.episode_id,
            "agent_id": agent_id,
            "reward": label.score if label is not None else None,
            "reward_rationale": label.rationale if label is not None else None,
            "cited": cited,
            "reasoning": next((t.reasoning_content for t in ep.turns if t.reasoning_content), None),
            "steps": steps,
        }

    return run


def _build_auth(*, load_dotenv: bool = True):
    """Wire human dashboard auth (Google + session) from env, or (None, None, None).

    Returns (provider, signer, users). Degrades gracefully: if GOOGLE_* /
    APRNTC_SESSION_SECRET aren't set, the dashboard runs open (dev mode).
    """
    try:
        from aprntc.config import load_dotenv as _ld
        env = _ld(".env") if load_dotenv else {}
        import os
        getenv = lambda k: os.environ.get(k) or env.get(k)  # noqa: E731

        secret = getenv("APRNTC_SESSION_SECRET")
        cid = getenv("GOOGLE_CLIENT_ID")
        csec = getenv("GOOGLE_CLIENT_SECRET")
        redirect = getenv("GOOGLE_REDIRECT_URI")
        if not (secret and cid and csec and redirect):
            return None, None, None

        from aprntc.auth import GoogleProvider, SessionSigner, UserStore
        provider = GoogleProvider(client_id=cid, client_secret=csec, redirect_uri=redirect)
        return provider, SessionSigner(secret), UserStore("users.json")
    except Exception:
        return None, None, None


def create_app(state: AppState | None = None) -> FastAPI:
    state = state or AppState()
    app = FastAPI(title="aprntc dashboard API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _SESSION_COOKIE = "aprntc_session"

    # -- tenant resolution (B2 extension) -------------------------------------
    # When `state.tenant_resolver` is set, every data endpoint is gated: callers
    # must present either an API key (X-API-Key / Bearer — machine clients) or a
    # valid dashboard session cookie (human signed in via Google). Otherwise → 401.
    # In dev/single-tenant mode (no resolver) the helpers fall back to shared state.
    def _resolve_ctx(request: "Request") -> Any | None:
        if state.tenant_resolver is None:
            return None
        # 1) machine path
        if (key := _api_key(request)) is not None:
            ctx = state.tenant_resolver.resolve(key)
            if ctx is not None:
                return ctx
        # 2) dashboard session path
        if state.session_signer is not None:
            from aprntc.auth import SessionError
            try:
                payload = state.session_signer.verify(request.cookies.get(_SESSION_COOKIE))
                ctx = state.tenant_resolver.resolve_tenant_id(payload.get("tid"))
                if ctx is not None:
                    return ctx
            except SessionError:
                pass
        raise HTTPException(status_code=401, detail="invalid or missing credentials")

    def _resolve_store(request: "Request") -> TrajectoryStore:
        ctx = _resolve_ctx(request)
        if ctx is not None:
            return state.tenant_store(ctx)
        if state.store is None:
            raise HTTPException(status_code=503, detail="no store configured")
        return state.store

    def _resolve_lineage(request: "Request") -> LineageRegistry:
        ctx = _resolve_ctx(request)
        return LineageRegistry(ctx.lineage_path) if ctx is not None else state.lineage()

    def _resolve_bundle(request: "Request") -> dict[str, Any]:
        ctx = _resolve_ctx(request)
        if ctx is None:
            return state.bundle()
        p = Path(ctx.bundle_path)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _resolve_memory_search(request: "Request") -> Any | None:
        ctx = _resolve_ctx(request)
        return state.tenant_memory(ctx) if ctx is not None else state.memory_search

    def _resolve_agent_run(request: "Request") -> Any | None:
        ctx = _resolve_ctx(request)
        return state.tenant_agent_run(ctx) if ctx is not None else state.agent_run

    # -- health ----------------------------------------------------------
    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "aprntc"}

    @app.get("/api/ops")
    def ops() -> dict[str, Any]:
        """Self-observability snapshot (B3): job runs, counters, last errors."""
        from aprntc.ops import METRICS
        snap = METRICS.snapshot()
        # surface store size if available (cheap health signal)
        try:
            snap["episodes"] = state.store.count() if state.store is not None else None
        except Exception:
            snap["episodes"] = None
        return snap

    # -- review / gate ---------------------------------------------------
    @app.get("/api/review")
    def get_review(request: Request) -> dict[str, Any]:
        """The current candidate review bundle: gate report + attributable diff."""
        bundle = _resolve_bundle(request)
        gate = bundle.get("gate", {})
        passed = gate.get("passed")
        if passed is None:
            passed = all(gate.get(k, False) for k in
                         ("win_rate_ok", "ci_ok", "loss_ok", "regression_ok", "safety_ok"))
        return {
            "available": bool(bundle),
            "candidate_playbook_hash": bundle.get("candidate_playbook_hash"),
            "gate": gate,
            "passed": passed,
            "diff": bundle.get("diff", {}),
        }

    # -- lineage ---------------------------------------------------------
    @app.get("/api/lineage")
    def get_lineage(request: Request) -> dict[str, Any]:
        reg = _resolve_lineage(request)
        cur = reg.current
        return {
            "current": cur.to_dict() if cur else None,
            "generations": [g.to_dict() for g in reg.history()],
        }

    @app.post("/api/lineage/promote")
    def promote(body: dict[str, Any], request: Request) -> dict[str, Any]:
        playbook_hash = body.get("playbook_hash")
        if not playbook_hash:
            raise HTTPException(status_code=400, detail="playbook_hash required")
        reg = _resolve_lineage(request)
        if reg.current is None:
            reg.register_parent(playbook_hash, note="seeded at first promote")
            return {"action": "registered_parent", "current": reg.current.to_dict()}
        gen = reg.promote(playbook_hash, gate_summary=body.get("gate_summary", ""),
                          note=body.get("note", "human-approved"))
        return {"action": "promoted", "current": gen.to_dict()}

    @app.post("/api/lineage/rollback")
    def rollback(request: Request) -> dict[str, Any]:
        reg = _resolve_lineage(request)
        try:
            gen = reg.rollback()
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"action": "rolled_back", "current": gen.to_dict()}

    # -- trajectories ----------------------------------------------------
    @app.get("/api/trajectories")
    def list_trajectories(request: Request, limit: int = 50,
                          generation: str | None = None,
                          collector: str | None = None) -> dict[str, Any]:
        # In dev mode an unconfigured store yields an empty list (existing behavior);
        # in tenant mode `_resolve_store` enforces auth and returns the tenant store.
        if state.tenant_resolver is None and state.store is None:
            return {"episodes": [], "count": 0, "by_collector": {}}
        store = _resolve_store(request)
        eps = store.query(generation_id=generation, collector=collector, limit=limit)
        return {
            "count": store.count(),
            "by_collector": store.counts_by_collector(),
            "episodes": [_episode_summary(e, store) for e in eps],
        }

    @app.get("/api/trajectories/{episode_id}")
    def get_trajectory(episode_id: str, request: Request) -> dict[str, Any]:
        if state.tenant_resolver is None and state.store is None:
            raise HTTPException(status_code=404, detail="no store configured")
        store = _resolve_store(request)
        try:
            ep = store.get_episode(episode_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no episode {episode_id}")
        data = ep.to_dict()
        data["labels"] = [l.to_dict() for l in store.labels_for(episode_id)]
        fused = store.fused_reward(episode_id)
        data["fused_reward"] = {"reward": fused[0], "confidence": fused[1]} if fused else None
        return data

    # -- lessons (experience memory) ------------------------------------
    @app.get("/api/lessons")
    def search_lessons(request: Request, q: str = "", k: int = 10) -> dict[str, Any]:
        search = _resolve_memory_search(request)
        if search is None:
            return {"lessons": [], "query": q, "available": False}
        try:
            results = search(q, k)
        except Exception as e:  # memory is best-effort; surface the error, don't 500
            return {"lessons": [], "query": q, "available": True, "error": str(e)}
        return {"lessons": results, "query": q, "available": True}

    # -- try an agent ---------------------------------------------------
    @app.get("/api/agents")
    def list_agents() -> dict[str, Any]:
        """Available demo agents + a few example prompts for the UI."""
        return {
            "available": state.agent_run is not None,
            "agents": [
                {
                    "id": "byteplus",
                    "name": "BytePlus support",
                    "description": "Answers BytePlus AI-stack questions (ModelArk, VikingDB, image/"
                                   "video/speech, files) grounded in the real docs, with citations.",
                    "examples": [
                        "How do I enable deep reasoning in ModelArk?",
                        "Which VikingDB index types are supported?",
                        "How do I pass an image to the model?",
                    ],
                },
                {
                    "id": "support",
                    "name": "Support chat",
                    "description": "Customer-support agent. Tools: KB lookup, order status.",
                    "examples": [
                        "What is your refund policy?",
                        "Where is order A1001?",
                        "How do I cancel my order?",
                    ],
                },
                {
                    "id": "rag",
                    "name": "RAG-Q&A",
                    "description": "Answers from a small doc corpus (Sun, Earth, Mars, Jupiter) with citations.",
                    "examples": [
                        "How old is the Sun?",
                        "Which planet is the Red Planet?",
                        "What is the largest planet?",
                    ],
                },
            ],
        }

    _AGENT_IDS = ("byteplus", "support", "rag")

    @app.post("/api/agents/run")
    def run_agent(body: dict[str, Any], request: Request) -> dict[str, Any]:
        agent_id = body.get("agent_id")
        task = (body.get("task") or "").strip()
        if agent_id not in _AGENT_IDS:
            raise HTTPException(status_code=400, detail=f"agent_id must be one of {_AGENT_IDS}")
        if not task:
            raise HTTPException(status_code=400, detail="task is required")
        runner = _resolve_agent_run(request)
        if runner is None:
            raise HTTPException(status_code=503, detail="agent runtime not configured (needs ModelArk keys)")
        try:
            return runner(agent_id, task)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"agent run failed: {e}")

    @app.post("/api/feedback")
    def feedback(body: dict[str, Any], request: Request) -> dict[str, Any]:
        """Record a user's 👍/👎 on an answer as an EXPLICIT-feedback label.

        Explicit feedback outranks the judge in fusion (ADR 0006), so this is real
        learning signal — not just UI. 'up' → score 1.0, 'down' → 0.0.
        """
        from aprntc.trajectory import Label, LabelSource

        episode_id = body.get("episode_id")
        vote = body.get("vote")
        if vote not in ("up", "down"):
            raise HTTPException(status_code=400, detail="vote must be 'up' or 'down'")
        if state.tenant_resolver is None and state.store is None:
            raise HTTPException(status_code=503, detail="no store configured")
        store = _resolve_store(request)
        try:
            store.attach_label(
                episode_id,
                Label(source=LabelSource.USER_EXPLICIT,
                      score=1.0 if vote == "up" else 0.0,
                      rubric_dim="thumbs", confidence=0.8,
                      rationale=f"user {vote}vote"),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no episode {episode_id}")
        fused = store.fused_reward(episode_id)
        return {"ok": True, "fused_reward": fused[0] if fused else None}

    # -- B0/B2: playbook serving — tenant-isolated when a resolver is configured --
    def _registry(request: "Request"):
        """Resolve the playbook registry for this request.

        With a TenantResolver (B2): require a valid API key (X-API-Key / Bearer) and
        return that tenant's ISOLATED registry. Without one: single-tenant/dev mode
        using the shared registry. Returns (registry, tenant_id_or_None).
        """
        if state.tenant_resolver is not None:
            ctx = state.tenant_resolver.resolve(_api_key(request))
            if ctx is None:
                raise HTTPException(status_code=401, detail="invalid or missing API key")
            from aprntc.serving import PlaybookRegistry
            return PlaybookRegistry(ctx.playbooks_path), ctx.tenant_id
        if state.playbooks is None:
            raise HTTPException(status_code=503, detail="playbook registry not configured")
        return state.playbooks, None

    @app.get("/api/playbooks/{agent_id}/active")
    def get_active_playbook(agent_id: str, request: Request) -> dict[str, Any]:
        """The endpoint an EXTERNAL agent calls each request (or caches) to get its
        active playbook — promotion/rollback just changes what this returns."""
        registry, _ = _registry(request)
        try:
            return registry.get_active(agent_id).to_dict()
        except KeyError:
            raise HTTPException(status_code=404,
                                detail=f"no playbook registered for agent {agent_id!r}")

    @app.post("/api/playbooks/{agent_id}/register")
    def register_playbook(agent_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
        """Register an external agent's initial G0 (its current system prompt)."""
        from aprntc.distill.playbook import Playbook

        registry, _ = _registry(request)
        sp = (body.get("system_prompt") or "").strip()
        if not sp:
            raise HTTPException(status_code=400, detail="system_prompt is required for G0")
        pb = Playbook(
            system_prompt=sp,
            directives=list(body.get("directives", [])),
            exemplars=list(body.get("exemplars", [])),
            watch_out=list(body.get("watch_out", [])),
        )
        return registry.register(agent_id, pb).to_dict()

    @app.post("/api/playbooks/{agent_id}/rollback")
    def rollback_playbook(agent_id: str, request: Request) -> dict[str, Any]:
        registry, _ = _registry(request)
        try:
            return registry.rollback(agent_id).to_dict()
        except (KeyError, RuntimeError) as e:
            raise HTTPException(status_code=409, detail=str(e))

    # -- B4: human dashboard auth (Google sign-in) ----------------------------
    # (_SESSION_COOKIE is defined above so the tenant resolver can read it too.)

    @app.get("/api/auth/config")
    def auth_config() -> dict[str, Any]:
        """Tells the frontend whether login is enabled (so it shows the button)."""
        return {"enabled": state.auth_provider is not None,
                "provider": getattr(state.auth_provider, "name", None)}

    @app.get("/api/auth/login")
    def auth_login() -> Any:
        """Redirect the browser to the provider's consent screen."""
        from fastapi.responses import RedirectResponse
        if state.auth_provider is None:
            raise HTTPException(status_code=503, detail="auth not configured")
        # state param (CSRF) — minimal here; a nonce store can harden it later
        url = state.auth_provider.authorize_url(state="aprntc")
        return RedirectResponse(url)

    @app.get("/api/auth/google/callback")
    def auth_callback(code: str = "", state_param: str = "") -> Any:
        """Google redirects here with a code; exchange it, set the session cookie."""
        from fastapi.responses import RedirectResponse
        if state.auth_provider is None or state.session_signer is None or state.users is None:
            raise HTTPException(status_code=503, detail="auth not configured")
        if not code:
            raise HTTPException(status_code=400, detail="missing code")
        try:
            profile = state.auth_provider.exchange_code(code)
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"oauth exchange failed: {e}")
        user, _ = state.users.upsert_from_oauth(
            provider=profile.provider, sub=profile.sub, email=profile.email,
            name=profile.name, picture=profile.picture)
        token = state.session_signer.issue(user_id=user.user_id, tenant_id=user.tenant_id)
        resp = RedirectResponse(url="/")   # back to the dashboard, now logged in
        resp.set_cookie(_SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=7*24*3600)
        return resp

    @app.get("/api/auth/me")
    def auth_me(request: Request) -> dict[str, Any]:
        """Who is logged in (the frontend calls this on load)."""
        if state.session_signer is None:
            return {"authenticated": False, "auth_enabled": False}
        from aprntc.auth import SessionError
        try:
            payload = state.session_signer.verify(request.cookies.get(_SESSION_COOKIE))
        except SessionError:
            return {"authenticated": False, "auth_enabled": True}
        u = state.users.get(payload["uid"]) if state.users else None
        return {
            "authenticated": True, "auth_enabled": True,
            "user_id": payload["uid"], "tenant_id": payload["tid"],
            "email": getattr(u, "email", None), "name": getattr(u, "name", None),
            "picture": getattr(u, "picture", None),
        }

    @app.post("/api/auth/logout")
    def auth_logout() -> Any:
        from fastapi.responses import JSONResponse
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(_SESSION_COOKIE)
        return resp

    # -- B1: serve the built React frontend (single-container deploy) ----------
    # Mounted LAST so /api/* routes win. Optional: only if a build dir exists, so
    # dev (Vite on :5173) and tests are unaffected. Set APRNTC_STATIC_DIR to override.
    _mount_static(app)

    return app


def _mount_static(app: FastAPI) -> None:
    """Serve web/dist as an SPA (index.html fallback) if it's been built."""
    import os

    env_dir = os.environ.get("APRNTC_STATIC_DIR")
    if env_dir:
        # explicit override is authoritative — don't fall back to the repo build
        candidates = [Path(env_dir)]
    else:
        # repo layout: src/aprntc/web/app.py -> ../../../web/dist
        candidates = [Path(__file__).resolve().parents[3] / "web" / "dist"]

    dist = next((d for d in candidates if d.is_dir() and (d / "index.html").exists()), None)
    if dist is None:
        return  # no build → API-only (dev/tests). Not an error.

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    # hashed assets under /assets
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        # never shadow the API; let unknown /api/* 404 as JSON
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")
        candidate = dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(dist / "index.html"))  # SPA client-side routing


def _api_key(request: "Request") -> str | None:
    """Extract the API key from X-API-Key or an Authorization: Bearer header."""
    key = request.headers.get("x-api-key")
    if key:
        return key
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _episode_summary(ep: Any, store: TrajectoryStore) -> dict[str, Any]:
    fused = store.fused_reward(ep.episode_id)
    return {
        "episode_id": ep.episode_id,
        "task_input": ep.task_input,
        "final_output": ep.final_output,
        "collector": ep.collector.value,
        "generation_id": ep.generation_id,
        "ts_start": ep.ts_start,
        "partial": ep.partial,
        "n_turns": len(ep.turns),
        "n_steps": sum(len(t.steps) for t in ep.turns),
        "reward": fused[0] if fused else None,
    }


# Module-level app for `uvicorn aprntc.web.app:app` — wires the real persistent
# SQLite store + VikingDB memory from env/cwd (degrades gracefully if absent).
app = create_app(AppState.from_env())
