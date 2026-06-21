"""Shadow runner — evaluate the child on live traffic without affecting users.

On each live request the parent serves normally; the shadow runner ALSO runs the
child on the same input (output discarded), judges child-vs-parent pairwise, and
accumulates a running win-rate with a Wilson CI. This is the online analogue of the
offline promotion gate — same acceptance math, but on real traffic (ADR 0006).

**Invariants (mirroring the tap):**
- **Fail-open:** a shadow error never propagates to the user's response. The public
  entry point is ``observe()``, which returns immediately; shadow work is isolated.
- **Sampled:** ``sample_rate`` bounds the ~2x inference cost (default: shadow all).
- The child's output is **never returned to the user** — shadow is observation only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from aprntc.eval.judge import PairwiseJudge
from aprntc.promote.stats import wilson_interval

Answerer = Callable[[str], str]


@dataclass
class ShadowStats:
    """Running tally of child-vs-parent on live traffic."""

    n: int = 0
    wins: float = 0.0
    losses: float = 0.0
    ties: int = 0
    errors: int = 0  # child/judge failures (excluded from win-rate)

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def loss_rate(self) -> float:
        return self.losses / self.n if self.n else 0.0

    def ci(self) -> tuple[float, float]:
        return wilson_interval(self.wins, self.n)

    def summary(self) -> str:
        lo, hi = self.ci()
        return (f"shadow n={self.n} win={self.win_rate:.0%} CI=[{lo:.0%},{hi:.0%}] "
                f"loss={self.loss_rate:.0%} errors={self.errors}")


class ShadowRunner:
    """Runs the child in shadow on sampled live requests and tallies the result.

    ``rng`` is an injectable 0..1 sampler (default: shadow everything) so tests are
    deterministic without ``random`` (unavailable in some sandboxes).
    """

    def __init__(
        self,
        judge: PairwiseJudge,
        *,
        child: Answerer,
        sample_rate: float = 1.0,
        rng: Callable[[], float] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        self._judge = judge
        self._child = child
        self._sample_rate = sample_rate
        self._rng = rng or (lambda: 0.0)  # default: always sample
        self._on_error = on_error
        self.stats = ShadowStats()
        self._toggle = True  # alternate A/B slot to debias position

    def observe(self, task: str, parent_answer: str) -> None:
        """Record one live interaction. The parent already answered the user; we
        shadow the child against it. NEVER raises (fail-open)."""
        try:
            if self._rng() >= self._sample_rate:
                return
            self._shadow(task, parent_answer)
        except BaseException as exc:  # noqa: BLE001 - fail-open
            self.stats.errors += 1
            if self._on_error is not None:
                try:
                    self._on_error(exc)
                except BaseException:
                    pass

    def _shadow(self, task: str, parent_answer: str) -> None:
        child_answer = self._child(task)  # the child runs; its output is discarded
        child_is_a = self._toggle
        self._toggle = not self._toggle
        verdict = self._judge.compare(
            task=task, child_answer=child_answer, parent_answer=parent_answer,
            child_is_a=child_is_a,
        )
        self.stats.n += 1
        if verdict.winner == "child":
            self.stats.wins += 1
        elif verdict.winner == "parent":
            self.stats.losses += 1
        else:
            self.stats.wins += 0.5
            self.stats.losses += 0.5
            self.stats.ties += 1

    def ready_to_promote(self, *, min_n: int = 30, win_rate_min: float = 0.55,
                         ci_low_min: float = 0.50, loss_rate_max: float = 0.10,
                         trust: float | None = None, min_trust: float = 0.80) -> bool:
        """True when accumulated live shadow stats clear the acceptance bar.

        Mirrors the A4 auto-promote guardrail: a strong live win-rate alone isn't
        enough if the JUDGE that produced those wins hasn't earned reliability.
        Pass ``trust`` (the A3 judge↔anchor agreement, e.g. from
        :func:`aprntc.eval.fusion.compute_trust`). ``None`` ⇒ no trust signal yet
        ⇒ stay blocked. ``min_trust`` defaults to 0.80, matching ``AutoPromotionPolicy``.
        """
        if self.stats.n < min_n:
            return False
        if trust is None or trust < min_trust:
            return False
        lo, _ = self.stats.ci()
        return (self.stats.win_rate >= win_rate_min and lo > ci_low_min
                and self.stats.loss_rate < loss_rate_max)
