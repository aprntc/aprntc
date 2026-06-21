"""A2 online eval — shadow runner + canary controller (offline, deterministic)."""

import pytest

from aprntc.eval.judge import PairwiseJudge
from aprntc.online import CanaryController, CanaryStatus, ShadowRunner
from aprntc.providers.base import CompletionResult


# ─── judge fakes ─────────────────────────────────────────────────────────────

def _judge(winner_slot="A", score=0.9):
    payload = f'{{"winner":"{winner_slot}","child_score":{score},"rationale":"x"}}'
    class P:
        def complete(self, *, model, messages, **kwargs):
            return CompletionResult(text=payload)
    return PairwiseJudge(P(), judge_model="ep-judge", policy_model="ep-policy")


class AlwaysChildWinsJudge:
    """A judge whose verdict always favors the CHILD regardless of slot."""
    def __init__(self):
        self.judge_model = "ep-judge"
    def compare(self, *, task, child_answer, parent_answer, child_is_a=True):
        from aprntc.eval.judge import JudgeVerdict
        return JudgeVerdict(winner="child", child_score=0.9, rationale="child better", raw={})


# ─── shadow runner ───────────────────────────────────────────────────────────

def test_shadow_accumulates_wins():
    runner = ShadowRunner(AlwaysChildWinsJudge(), child=lambda t: "child ans")
    for i in range(10):
        runner.observe(f"q{i}", "parent ans")
    assert runner.stats.n == 10 and runner.stats.wins == 10
    assert runner.stats.win_rate == 1.0


def test_shadow_is_fail_open_on_child_error():
    def boom(_t):
        raise RuntimeError("child crashed")
    errors = []
    runner = ShadowRunner(AlwaysChildWinsJudge(), child=boom, on_error=errors.append)
    runner.observe("q", "parent ans")  # must NOT raise
    assert runner.stats.errors == 1 and runner.stats.n == 0
    assert len(errors) == 1


def test_shadow_sampling_skips():
    # rng always returns 0.9; sample_rate 0.5 -> 0.9 >= 0.5 -> skip everything
    runner = ShadowRunner(AlwaysChildWinsJudge(), child=lambda t: "c",
                          sample_rate=0.5, rng=lambda: 0.9)
    for i in range(5):
        runner.observe(f"q{i}", "p")
    assert runner.stats.n == 0


def test_shadow_position_debias_alternates():
    # record which slot the child was placed in across calls
    slots = []
    class SpyJudge:
        judge_model = "j"
        def compare(self, *, task, child_answer, parent_answer, child_is_a=True):
            from aprntc.eval.judge import JudgeVerdict
            slots.append(child_is_a)
            return JudgeVerdict(winner="tie", child_score=0.5, rationale="", raw={})
    runner = ShadowRunner(SpyJudge(), child=lambda t: "c")
    runner.observe("q1", "p"); runner.observe("q2", "p")
    assert slots == [True, False]  # alternates A/B to debias position


def test_shadow_ties_split():
    class TieJudge:
        judge_model = "j"
        def compare(self, **kw):
            from aprntc.eval.judge import JudgeVerdict
            return JudgeVerdict(winner="tie", child_score=0.5, rationale="", raw={})
    runner = ShadowRunner(TieJudge(), child=lambda t: "c")
    runner.observe("q", "p")
    assert runner.stats.wins == 0.5 and runner.stats.losses == 0.5 and runner.stats.ties == 1


def test_shadow_ready_to_promote_gates_on_n_and_ci():
    """Stats-side guardrails — assumes judge trust is already established."""
    runner = ShadowRunner(AlwaysChildWinsJudge(), child=lambda t: "c")
    for i in range(10):
        runner.observe(f"q{i}", "p")
    assert not runner.ready_to_promote(min_n=30, trust=1.0)   # too few samples
    for i in range(30):
        runner.observe(f"r{i}", "p")
    # 40 wins, CI clears, trust is high → ready
    assert runner.ready_to_promote(min_n=30, trust=1.0)


def test_shadow_ready_to_promote_blocks_without_trust_signal():
    """Even with a perfect live record, no judge-trust signal blocks promotion.

    Same guardrail as the A4 auto-policy: a strong win-rate produced by an
    untrusted judge isn't enough to override human review.
    """
    runner = ShadowRunner(AlwaysChildWinsJudge(), child=lambda t: "c")
    for i in range(40):
        runner.observe(f"q{i}", "p")
    assert runner.stats.win_rate == 1.0  # judge always picks child
    # No trust signal yet (cold start) → blocked.
    assert not runner.ready_to_promote(min_n=30, trust=None)
    # Trust below the default min (0.80) → still blocked.
    assert not runner.ready_to_promote(min_n=30, trust=0.6)
    # Trust over min → cleared.
    assert runner.ready_to_promote(min_n=30, trust=0.85)


def test_shadow_ready_to_promote_min_trust_is_tunable():
    """Operators can tighten the trust bar without code change."""
    runner = ShadowRunner(AlwaysChildWinsJudge(), child=lambda t: "c")
    for i in range(40):
        runner.observe(f"q{i}", "p")
    # 0.85 trust would pass the default 0.80, but not a stricter 0.90.
    assert runner.ready_to_promote(min_n=30, trust=0.85, min_trust=0.80)
    assert not runner.ready_to_promote(min_n=30, trust=0.85, min_trust=0.90)


# ─── canary controller ───────────────────────────────────────────────────────

def test_canary_routes_by_fraction_stably():
    c = CanaryController(stages=(0.5,))
    # same key always lands in the same arm
    k = "user-42"
    assert c.route(k) == c.route(k)
    # over many keys, roughly the configured fraction go to child
    child = sum(1 for i in range(200) if c.route(f"u{i}") == "child")
    assert 70 <= child <= 130  # ~50% with hash spread


def test_canary_advances_stages_then_promotes():
    c = CanaryController(stages=(0.1, 1.0), min_per_stage=3, grace_n=100)
    # feed child rewards; parent stays comparable so no rollback
    for _ in range(3):
        c.record("child", 0.9)
    assert c.fraction == 1.0 and c.status is CanaryStatus.RUNNING
    for _ in range(3):
        c.record("child", 0.9)
    assert c.status is CanaryStatus.PROMOTED


def test_canary_auto_rollback_on_degradation():
    c = CanaryController(stages=(0.5, 1.0), min_per_stage=100,
                         degrade_margin=0.05, grace_n=3)
    for _ in range(5):
        c.record("parent", 0.9)   # parent baseline high
    for _ in range(5):
        c.record("child", 0.3)    # child much worse
    assert c.status is CanaryStatus.ROLLED_BACK
    assert c.fraction == 0.0 and c.route("anyone") == "parent"


def test_canary_no_rollback_within_grace():
    c = CanaryController(degrade_margin=0.05, grace_n=10)
    c.record("parent", 0.9)
    c.record("child", 0.1)  # worse, but under grace_n -> no rollback yet
    assert c.status is CanaryStatus.RUNNING


def test_canary_promoted_routes_all_to_child():
    c = CanaryController(stages=(1.0,), min_per_stage=1, grace_n=100)
    c.record("child", 0.9)
    assert c.status is CanaryStatus.PROMOTED and c.route("x") == "child"
