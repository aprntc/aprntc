"""Learned fusion weights (A3) — trust each signal by its agreement with the anchor.

The store's default fusion uses FIXED reliability weights (ADR 0006). A3 *learns*
them from history: for each label source, measure how well it agrees with the
**anchor** (the most reliable signal present on the same episode — outcome >
explicit > human), and weight a source by that agreement. A judge that routinely
disagrees with real outcomes/user feedback gets auto-down-weighted; one that tracks
them keeps its trust.

Pure + stdlib-only: :func:`learn_weights` reads episodes' labels and returns a
``{source: weight}`` map you feed back into ``store.fused_reward(weights=...)``.
Cold-start safe (falls back to priors when there's not enough agreement data).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aprntc.trajectory.schema import Label, LabelSource

# Anchor reliability order — the "ground truth" we calibrate other sources against.
_ANCHOR_ORDER = (LabelSource.OUTCOME, LabelSource.HUMAN, LabelSource.USER_EXPLICIT)

# Prior weights (cold start / fallback) — mirror the store's fixed defaults.
_PRIOR = {
    LabelSource.OUTCOME: 1.0,
    LabelSource.HUMAN: 0.9,
    LabelSource.USER_EXPLICIT: 0.7,
    LabelSource.USER_IMPLICIT: 0.5,
    LabelSource.JUDGE: 0.4,
}


@dataclass
class SourceAgreement:
    source: LabelSource
    n: int = 0                 # episodes where this source AND an anchor were present
    agree: float = 0.0         # sum of agreement (1 - |source_score - anchor_score|)
    learned_weight: float = 0.0
    prior_weight: float = 0.0

    @property
    def agreement(self) -> float:
        return self.agree / self.n if self.n else 0.0


@dataclass
class FusionWeights:
    weights: dict[LabelSource, float] = field(default_factory=dict)
    report: dict[LabelSource, SourceAgreement] = field(default_factory=dict)

    def as_map(self) -> dict[LabelSource, float]:
        return dict(self.weights)

    def summary(self) -> str:
        rows = []
        for src, sa in sorted(self.report.items(), key=lambda kv: kv[0].value):
            rows.append(f"{src.value}: w={sa.learned_weight:.2f} "
                        f"(prior {sa.prior_weight:.2f}, agree {sa.agreement:.2f} n={sa.n})")
        return " | ".join(rows)


def _anchor_score(labels: list[Label]) -> float | None:
    """The score of the most reliable anchor present on this episode, if any."""
    by_source = {l.source: l for l in labels}
    for src in _ANCHOR_ORDER:
        if src in by_source:
            return by_source[src].score
    return None


def learn_weights(
    episodes_labels: list[list[Label]],
    *,
    min_n: int = 5,
    blend: float = 0.5,
) -> FusionWeights:
    """Learn per-source weights from history.

    ``episodes_labels``: one list of Labels per episode. For each non-anchor source,
    agreement = mean(1 - |source_score - anchor_score|) over episodes where both the
    source and an anchor appear. Learned weight = prior scaled by agreement, then
    blended with the prior by ``blend`` (0 = ignore data, 1 = trust data fully).
    Anchor sources keep their priors (they ARE the reference). ``min_n`` guards
    against over-reacting to tiny samples (falls back to prior).
    """
    agg: dict[LabelSource, SourceAgreement] = {}

    for labels in episodes_labels:
        anchor = _anchor_score(labels)
        if anchor is None:
            continue
        anchor_sources = set(_ANCHOR_ORDER)
        for lbl in labels:
            if lbl.source in anchor_sources:
                continue  # don't calibrate an anchor against itself
            sa = agg.setdefault(lbl.source, SourceAgreement(source=lbl.source))
            sa.n += 1
            sa.agree += 1.0 - abs(lbl.score - anchor)

    weights: dict[LabelSource, float] = dict(_PRIOR)
    report: dict[LabelSource, SourceAgreement] = {}

    for src, prior in _PRIOR.items():
        sa = agg.get(src) or SourceAgreement(source=src)
        sa.prior_weight = prior
        if src in _ANCHOR_ORDER or sa.n < min_n:
            sa.learned_weight = prior          # anchors + cold-start keep the prior
        else:
            # scale prior by agreement (0..1), then blend with prior for stability
            data_w = prior * sa.agreement
            sa.learned_weight = (1 - blend) * prior + blend * data_w
        weights[src] = sa.learned_weight
        report[src] = sa

    return FusionWeights(weights=weights, report=report)


@dataclass
class TrustReport:
    """Trust signal for the A4 auto-promotion gate, derived from A3 fusion data.

    The single scalar ``value`` (0..1) is how well the JUDGE has historically agreed
    with anchors (outcome > human > explicit) on the same episodes — i.e. has the
    judge proven itself reliable enough to auto-promote on. ``None`` when there
    isn't enough joint data yet (cold-start), in which case the auto-policy stays
    conservatively in HUMAN_REVIEW.
    """

    value: float | None
    n: int                     # number of episodes with both a judge label AND an anchor
    min_n: int                 # threshold used (n < min_n → value is None)


def compute_trust(
    episodes_labels: list[list[Label]],
    *,
    min_n: int = 10,
) -> TrustReport:
    """Derive the auto-promote trust scalar from the judge ↔ anchor agreement.

    Same pairing logic as :func:`learn_weights` (an anchor must be present on the
    same episode as the judge label), but reports just the judge agreement and the
    sample size so the auto-policy can decide "trustworthy enough to auto-fire".

    The 10-sample default mirrors the gate's notion of "powered N" — fewer than
    that and a single bad agreement could swing the score wildly. Adjust higher
    in production once enough joint data accumulates.
    """
    n = 0
    agree = 0.0
    for labels in episodes_labels:
        anchor = _anchor_score(labels)
        if anchor is None:
            continue
        judge = next((l for l in labels if l.source is LabelSource.JUDGE), None)
        if judge is None:
            continue
        n += 1
        agree += 1.0 - abs(judge.score - anchor)
    if n < min_n:
        return TrustReport(value=None, n=n, min_n=min_n)
    return TrustReport(value=agree / n, n=n, min_n=min_n)
