"""ImprovementOrchestrator — the autonomous per-agent loop.

Uses a fake LLM provider + fake judge so the pipeline runs fully offline:
distill (fake mined lessons) → memory upsert → candidate build → replay gate
(fake judge verdicts) → review bundle write + state persistence. Verifies the
trigger threshold, auto-promote gating, and that the bundle the dashboard reads
is produced.
"""

from __future__ import annotations

import json

import pytest

from aprntc.distill.distiller import Distiller
from aprntc.distill.playbook import Playbook
from aprntc.eval.judge import PairwiseJudge, JudgeVerdict
from aprntc.memory import LocalMemoryStore, HashEmbedder
from aprntc.serving import PlaybookRegistry
from aprntc.serving.orchestrator import ImprovementOrchestrator
from aprntc.trajectory import (
    Collector,
    Episode,
    Label,
    LabelSource,
    Step,
    StepType,
    TrajectoryStore,
    Turn,
)


# ── fakes ────────────────────────────────────────────────────────────────────

class _FakeProvider:
    """Returns canned completions. The distiller mines lessons from its JSON; the
    replay re-answer just needs *some* text."""

    def __init__(self, *, distill_json: str, replay_text: str = "a better answer") -> None:
        self._distill = distill_json
        self._replay = replay_text

    def complete(self, *, model, messages, **kw):
        from aprntc.providers.base import CompletionResult
        sys = messages[0]["content"] if messages else ""
        # The distiller's system prompt asks for JSON lessons; the replay uses the
        # candidate rendered prompt. Distinguish by whether the system prompt looks
        # like the distiller's mining instruction.
        if "lesson" in sys.lower() and "json" in sys.lower():
            return CompletionResult(text=self._distill)
        return CompletionResult(text=self._replay)

    def close(self):
        pass


class _ChildWinsJudge(PairwiseJudge):
    """Judge that always favours the child (so the gate passes)."""
    def __init__(self):
        pass
    def compare(self, *, task, child_answer, parent_answer, child_is_a=True):
        return JudgeVerdict(winner="child", child_score=1.0, rationale="", raw={})


class _ParentWinsJudge(PairwiseJudge):
    def __init__(self):
        pass
    def compare(self, *, task, child_answer, parent_answer, child_is_a=True):
        return JudgeVerdict(winner="parent", child_score=0.0, rationale="", raw={})


_DISTILL_JSON = json.dumps({
    "lessons": [
        {"situation": "refund request", "lesson": "Always state the refund reason and the return-window rule."},
        {"situation": "order status", "lesson": "Include the carrier and tracking number when known."},
    ]
})


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def store():
    s = TrajectoryStore(":memory:")
    yield s
    s.close()


@pytest.fixture()
def memory(tmp_path):
    m = LocalMemoryStore(str(tmp_path / "mem.db"), embedder=HashEmbedder())
    yield m
    m.close()


def _seed(store, agent_id="shop", n=6, reward=0.9):
    """Seed n labelled episodes for an agent so distillation has material."""
    for i in range(n):
        turn = Turn(turn_index=0)
        turn.steps.append(Step(step_index=0, type=StepType.TOOL_CALL,
                               tool_name="get_order", tool_args={"order_id": f"A100{i}"},
                               tool_result={"status": "delivered"}))
        ep = Episode(task_input=f"status of order A100{i}?", collector=Collector.EGRESS_PROXY,
                     final_output=f"Order A100{i} is delivered.", agent_id=agent_id,
                     turns=[turn])
        eid = store.put_episode(ep, scrub=False)
        store.attach_label(eid, Label(source=LabelSource.USER_EXPLICIT, score=reward, confidence=0.9))


def _orch(store, memory, tmp_path, *, judge=None, auto_policy=None, trust=None, min_new=5):
    playbooks = PlaybookRegistry(str(tmp_path / "pb.json"))
    playbooks.register("shop", Playbook(system_prompt="You are ShopMate. Be concise."))
    provider = _FakeProvider(distill_json=_DISTILL_JSON)
    distiller = Distiller(provider, model="m", make_exemplars=False)
    return ImprovementOrchestrator(
        store=store, memory=memory, playbooks=playbooks,
        provider=provider, distiller=distiller,
        judge=judge or _ChildWinsJudge(), model="m",
        bundle_path=str(tmp_path / "bundle.json"),
        state_path=str(tmp_path / "orch_state.json"),
        min_new_trajectories=min_new, gate_sample=4,
        auto_policy=auto_policy, trust=trust,
    ), playbooks


# ── tests ────────────────────────────────────────────────────────────────────

def test_skips_when_below_trigger(store, memory, tmp_path):
    _seed(store, n=2)
    orch, _ = _orch(store, memory, tmp_path, min_new=5)
    rep = orch.run_for_agent("shop")  # not forced
    assert rep.ran is False
    assert "new trajectories" in rep.reason


def test_full_run_produces_lessons_candidate_and_bundle(store, memory, tmp_path):
    _seed(store, n=6)
    orch, playbooks = _orch(store, memory, tmp_path)
    rep = orch.run_for_agent("shop", force=True, now="2026-06-21T00:00:00+00:00")
    assert rep.ran is True
    # lessons mined + written to memory
    assert rep.n_lessons >= 1
    assert memory.count() >= 1
    # candidate built = G1
    assert rep.candidate_generation == 1
    # replay gate ran + the child-wins judge passes it
    assert rep.gate_passed is True
    assert rep.win_rate == pytest.approx(1.0)
    # review bundle written (dashboard reads this)
    assert rep.bundle_written is True
    bundle = json.loads((tmp_path / "bundle.json").read_text())
    assert bundle["agent_id"] == "shop"
    assert bundle["eval_mode"] == "replay"
    assert bundle["candidate_generation"] == 1
    assert bundle["gate"]["passed"] is True
    # NOT promoted (no auto-policy) — stays for human review
    assert rep.auto_promoted is False
    assert playbooks.get_active("shop").generation == 0  # still G0 until a human promotes


def test_auto_promote_when_policy_enabled_and_guardrails_clear(store, memory, tmp_path):
    from aprntc.promote.auto import AutoPromotionPolicy
    _seed(store, n=6)
    # Enabled policy + high trust + a winning gate → should auto-promote.
    policy = AutoPromotionPolicy(enabled=True, win_rate_min=0.6, ci_low_min=0.5,
                                 loss_rate_max=0.5, max_diff_items=10, min_trust=0.8)
    orch, playbooks = _orch(store, memory, tmp_path, auto_policy=policy, trust=0.95)
    rep = orch.run_for_agent("shop", force=True)
    assert rep.gate_passed is True
    assert rep.auto_promoted is True
    assert playbooks.get_active("shop").generation == 1  # promoted to G1 automatically


def test_no_auto_promote_without_trust(store, memory, tmp_path):
    from aprntc.promote.auto import AutoPromotionPolicy
    _seed(store, n=6)
    policy = AutoPromotionPolicy(enabled=True, min_trust=0.8)
    orch, playbooks = _orch(store, memory, tmp_path, auto_policy=policy, trust=None)
    rep = orch.run_for_agent("shop", force=True)
    assert rep.auto_promoted is False               # no trust signal → human review
    assert playbooks.get_active("shop").generation == 0


def test_only_this_agents_trajectories_are_used(store, memory, tmp_path):
    _seed(store, agent_id="shop", n=6)
    _seed(store, agent_id="other", n=6)  # a different agent's data must be ignored
    orch, _ = _orch(store, memory, tmp_path)
    # status should count only the 6 'shop' episodes
    st = orch.status_for("shop")
    assert st["n_trajectories"] == 6


def test_unregistered_agent_does_not_run(store, memory, tmp_path):
    _seed(store, agent_id="ghost", n=6)
    orch, _ = _orch(store, memory, tmp_path)  # only 'shop' is registered
    rep = orch.run_for_agent("ghost", force=True)
    assert rep.ran is False
    assert "no registered playbook" in rep.reason


def test_state_persists_trigger_count(store, memory, tmp_path):
    _seed(store, n=6)
    orch, _ = _orch(store, memory, tmp_path)
    orch.run_for_agent("shop", force=True, now="2026-06-21T00:00:00+00:00")
    # After a run the state records 6 → a second non-forced run with no new data skips.
    rep2 = orch.run_for_agent("shop")
    assert rep2.ran is False
    assert rep2.n_new == 0
