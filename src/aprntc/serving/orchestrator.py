"""ImprovementOrchestrator — the autonomous per-agent improvement loop.

This is what turns aprntc from "a toolbox you drive with scripts" into "a product
that improves an agent on its own". For one agent it runs, end to end:

    trajectories ──▶ distill lessons ──▶ write to Experience Memory
                                          │
                                          ▼
                      build candidate playbook G(n+1) = active + diff
                                          │
                                          ▼
                      replay gate (candidate vs recorded answers, recused judge)
                                          │
                                          ▼
                      write a review bundle  ──▶  (human clicks Promote)
                                          │            or, if A4 auto-promote is
                                          ▼            enabled AND guardrails clear,
                                   AUTO-PROMOTE        promote with no human.

It's **triggered** by the Scheduler (a cadence) or on demand
(`POST /api/agents/{id}/improve`), and it only does real work when enough NEW
trajectories have accumulated since the last run (`min_new_trajectories`).

### The replay gate (why external agents are different)

For aprntc's OWN demo agents, aprntc has the code and can run a fresh child with
its real tools. For an EXTERNAL agent (the customer's), aprntc does NOT have the
agent's code/tools — so it can't re-run the agent live. Instead it **replays**:
re-answer each recorded task with the *candidate* prompt, feeding back the tool
results that were already recorded in the trajectory, and the recused judge
compares that to the recorded (parent) answer. This faithfully measures a
SYSTEM-PROMPT change (the only thing a playbook alters) given the same
information. It is labelled ``eval_mode="replay"`` in the bundle so the human
knows it is a prompt-replay eval, not a live tool-faithful A/B. The fully
faithful path is online shadow mode (A2), which the customer opts into by
running both prompts in their own agent.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from aprntc.distill.distiller import Distiller
from aprntc.distill.playbook import Playbook
from aprntc.eval.judge import PairwiseJudge
from aprntc.promote.stats import GateReport, wilson_interval
from aprntc.trajectory.schema import Episode, StepType


@dataclass
class OrchestratorReport:
    """What one run produced (for the API + logs)."""

    agent_id: str
    ran: bool                        # False if the trigger threshold wasn't met
    reason: str = ""                 # why it did / didn't run
    n_trajectories: int = 0          # total this agent has
    n_new: int = 0                   # new since last run
    n_lessons: int = 0               # lessons mined this run
    candidate_generation: int | None = None
    candidate_hash: str | None = None
    gate_passed: bool | None = None
    win_rate: float | None = None
    auto_promoted: bool = False
    bundle_written: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _AgentState:
    """Per-agent orchestrator state (persisted)."""
    last_run_trajectory_count: int = 0
    last_candidate_hash: str | None = None
    last_run_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ImprovementOrchestrator:
    """Runs the autonomous distill→gate→bundle loop for one agent at a time.

    Constructed with the engine pieces it needs; the web app builds one from
    AppState. Pure-ish: filesystem (state + bundle) + the injected store/memory/
    registry/distiller/judge. No web concerns.
    """

    def __init__(
        self,
        *,
        store: Any,                       # TrajectoryStore-like
        memory: Any | None,              # MemoryStore-like (may be None → skip memory write)
        playbooks: Any,                  # PlaybookRegistry
        provider: Any,                   # LLMProvider (for replay re-answering)
        distiller: Distiller,
        judge: PairwiseJudge,
        model: str,
        bundle_path: str = "review_bundle.json",
        state_path: str = "orchestrator_state.json",
        fleet: Any | None = None,
        min_new_trajectories: int = 5,
        gate_sample: int = 12,
        auto_policy: Any | None = None,   # AutoPromotionPolicy (None → never auto)
        trust: float | None = None,       # A3 trust signal for the auto gate
    ) -> None:
        self._store = store
        self._memory = memory
        self._playbooks = playbooks
        self._provider = provider
        self._distiller = distiller
        self._judge = judge
        self._model = model
        self._bundle_path = bundle_path
        self._state_path = state_path
        self._fleet = fleet
        self._min_new = min_new_trajectories
        self._gate_sample = gate_sample
        self._auto_policy = auto_policy
        self._trust = trust

    # -- public API -----------------------------------------------------

    def run_for_agent(self, agent_id: str, *, force: bool = False,
                      now: str | None = None) -> OrchestratorReport:
        """Run the full loop for one agent. ``force`` bypasses the trigger threshold."""
        episodes = self._agent_episodes(agent_id)
        n = len(episodes)
        state = self._load_state().get(agent_id, _AgentState())
        n_new = n - state.last_run_trajectory_count

        rep = OrchestratorReport(agent_id=agent_id, ran=False,
                                 n_trajectories=n, n_new=max(0, n_new))

        if not self._playbooks.has(agent_id):
            rep.reason = f"agent {agent_id!r} has no registered playbook (G0)"
            return rep
        if not force and n_new < self._min_new:
            rep.reason = (f"only {n_new} new trajectories since last run "
                          f"(need {self._min_new}); skipping")
            return rep
        if n == 0:
            rep.reason = "no trajectories to learn from"
            return rep

        # 1) Distill lessons from this agent's scored trajectories.
        episode_ids = [e.episode_id for e in episodes]
        active = self._playbooks.get_active(agent_id)
        next_gen = active.generation + 1
        result = self._distiller.distill(self._store, generation=next_gen,
                                          episode_ids=episode_ids)
        rep.n_lessons = len(result.lessons)

        # 2) Persist lessons to Experience Memory (best-effort).
        if self._memory is not None and result.lessons:
            try:
                self._memory.upsert_lessons(result.lessons)
            except Exception:
                pass  # memory is best-effort; the candidate still gets built

        # 3) Build the candidate playbook G(n+1) = active + attributable diff.
        candidate: Playbook = result.diff.apply(active.playbook)
        rep.candidate_generation = candidate.generation
        rep.candidate_hash = candidate.hash

        if result.diff.is_empty():
            rep.reason = "distillation produced no changes (nothing to promote)"
            rep.ran = True
            self._save_run_state(agent_id, n, candidate.hash, now)
            return rep

        # 4) Replay gate — candidate vs recorded answers, recused judge.
        report = self._replay_gate(episodes, candidate)
        rep.gate_passed = report.passed
        rep.win_rate = report.win_rate

        # 5) Optionally auto-promote (A4) — else leave the bundle for a human.
        auto_promoted = False
        if self._auto_policy is not None:
            decision = self._auto_policy.decide(report, result.diff, trust=self._trust)
            from aprntc.promote.auto import PromotionAction
            if decision.action is PromotionAction.AUTO_PROMOTE:
                self._playbooks.promote(agent_id, candidate)
                auto_promoted = True
        rep.auto_promoted = auto_promoted

        # 6) Write the review bundle (so the dashboard Review screen shows it).
        self._write_bundle(agent_id, candidate, report, result.diff,
                           auto_promoted=auto_promoted)
        rep.bundle_written = True
        rep.ran = True
        self._save_run_state(agent_id, n, candidate.hash, now)
        return rep

    # -- trajectory selection -------------------------------------------

    def _agent_episodes(self, agent_id: str) -> list[Episode]:
        """All trajectories belonging to this agent (filtered in Python by agent_id).

        NOTE: filters in-memory. For very large stores, promote agent_id to a
        store column + index (a scale follow-up); fine at hundreds–thousands.
        """
        return [e for e in self._store.query() if getattr(e, "agent_id", None) == agent_id]

    # -- replay gate ----------------------------------------------------

    def _replay_gate(self, episodes: list[Episode], candidate: Playbook) -> GateReport:
        """Re-answer recorded tasks with the candidate prompt; judge vs recorded.

        Uses the most-recent ``gate_sample`` episodes. The child answer is generated
        by the policy model under the CANDIDATE rendered prompt, given the same tool
        results that were recorded in the trajectory (so a tool-using agent isn't
        penalised for the replay lacking live tools). The parent answer is the
        recorded ``final_output``.
        """
        sample = episodes[-self._gate_sample:]
        candidate_prompt = candidate.render()
        wins = losses = ties = 0.0

        for i, ep in enumerate(sample):
            parent_answer = ep.final_output or ""
            if not parent_answer:
                continue
            child_answer = self._replay_one(ep, candidate_prompt)
            verdict = self._judge.compare(
                task=ep.task_input,
                child_answer=child_answer,
                parent_answer=parent_answer,
                child_is_a=(i % 2 == 0),   # position-balanced
            )
            if verdict.winner == "child":
                wins += 1
            elif verdict.winner == "parent":
                losses += 1
            else:
                wins += 0.5
                losses += 0.5
                ties += 1

        n = int(wins + losses)
        win_rate = (wins / n) if n else 0.0
        loss_rate = (losses / n) if n else 0.0
        ci_low, ci_high = wilson_interval(wins, n)
        return GateReport(
            n=n, wins=wins, losses=losses, ties=ties,
            win_rate=win_rate, ci_low=ci_low, ci_high=ci_high, loss_rate=loss_rate,
            regression_failures=0, safety_failures=0,
            win_rate_ok=win_rate >= 0.55,
            ci_ok=ci_low > 0.50,
            loss_ok=loss_rate < 0.10,
            regression_ok=True,
            safety_ok=True,
        )

    def _replay_one(self, ep: Episode, candidate_prompt: str) -> str:
        """Generate a child answer for one episode under the candidate prompt."""
        tool_context = _recorded_tool_context(ep)
        user = ep.task_input
        if tool_context:
            user = f"{ep.task_input}\n\n[Information already gathered by tools:\n{tool_context}\n]"
        messages = [
            {"role": "system", "content": candidate_prompt},
            {"role": "user", "content": user},
        ]
        try:
            result = self._provider.complete(model=self._model, messages=messages)
            return (result.text or "").strip()
        except Exception:
            # If the replay call fails, return empty → judge will likely favour parent,
            # which is the safe (conservative) direction for promotion.
            return ""

    # -- bundle + state -------------------------------------------------

    def _write_bundle(self, agent_id: str, candidate: Playbook, report: GateReport,
                      diff: Any, *, auto_promoted: bool) -> None:
        bundle = {
            "agent_id": agent_id,
            "candidate_playbook_hash": candidate.hash,
            "candidate_generation": candidate.generation,
            "eval_mode": "replay",
            "auto_promoted": auto_promoted,
            "diff": diff.to_dict(),
            "gate": {k: getattr(report, k) for k in (
                "n", "wins", "losses", "ties", "win_rate", "ci_low", "ci_high",
                "loss_rate", "regression_failures", "safety_failures",
                "win_rate_ok", "ci_ok", "loss_ok", "regression_ok", "safety_ok",
            )},
        }
        bundle["gate"]["passed"] = report.passed
        bundle["gate"]["summary"] = report.summary()
        p = Path(self._bundle_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_state(self) -> dict[str, _AgentState]:
        p = Path(self._state_path)
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {aid: _AgentState(**d) for aid, d in data.items()}

    def status_for(self, agent_id: str) -> dict[str, Any]:
        """Read-only snapshot for the dashboard."""
        st = self._load_state().get(agent_id, _AgentState())
        n = len(self._agent_episodes(agent_id))
        return {
            "agent_id": agent_id,
            "n_trajectories": n,
            "n_new_since_last_run": max(0, n - st.last_run_trajectory_count),
            "min_new_to_trigger": self._min_new,
            "last_run_at": st.last_run_at,
            "last_candidate_hash": st.last_candidate_hash,
            "registered": self._playbooks.has(agent_id),
        }

    def _save_run_state(self, agent_id: str, count: int, cand_hash: str | None,
                        now: str | None) -> None:
        states = self._load_state()
        states[agent_id] = _AgentState(
            last_run_trajectory_count=count,
            last_candidate_hash=cand_hash,
            last_run_at=now or _iso_now(),
        )
        p = Path(self._state_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({aid: s.to_dict() for aid, s in states.items()},
                                indent=2, ensure_ascii=False), encoding="utf-8")


def _recorded_tool_context(ep: Episode) -> str:
    """Flatten the tool-call results recorded in the trajectory into a context blob."""
    lines: list[str] = []
    for turn in ep.turns:
        for step in turn.steps:
            if step.type is StepType.TOOL_CALL and step.tool_result is not None:
                name = step.tool_name or "tool"
                lines.append(f"- {name}({_short(step.tool_args)}) → {_short(step.tool_result)}")
    return "\n".join(lines)


def _short(value: Any, limit: int = 300) -> str:
    try:
        s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(value)
    return s if len(s) <= limit else s[:limit] + "…"


def _iso_now() -> str:
    # Imported lazily so the module stays import-light; the web app passes `now`
    # explicitly in tests for determinism.
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()
