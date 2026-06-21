"""Canonical Trajectory schema — locked by ADR 0007.

Dataclasses with strict, lossless JSON round-tripping (``to_dict`` / ``from_dict``).
Design rules baked in here:

* **True superset** — uncertain fields are Optional so lower-fidelity collectors
  (proxy/OTel) lose nothing.
* **Provenance** — ``Episode.collector`` + per-``Step`` ``source_fidelity``.
* **Best-effort** — ``partial`` flags mark incomplete captures rather than dropping them.
* **Privacy-by-shape** — media is stored **by reference** (``ContentPart.ref``), never
  raw blobs inline; ``pii_status`` supports scrub/TTL/delete-by-subject.
* **Versioned** — every Episode carries ``schema_version``.

Stdlib-only (core stays dependency-light). Enums serialize to their string values.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

SCHEMA_VERSION = 1


# ─── enums ──────────────────────────────────────────────────────────────────

class _StrEnum(str, Enum):
    """str-valued enum that serializes to its value and coerces leniently."""

    @classmethod
    def coerce(cls, value: "Any"):
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError as exc:  # pragma: no cover - exercised via tests
            valid = ", ".join(m.value for m in cls)
            raise ValueError(f"invalid {cls.__name__} {value!r}; expected one of: {valid}") from exc


class Collector(_StrEnum):
    SDK_WRAPPER = "sdk_wrapper"
    EGRESS_PROXY = "egress_proxy"
    OTEL = "otel"
    MCP = "mcp"
    A2A = "a2a"


class ContentType(_StrEnum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    FILE = "file"
    AUDIO = "audio"


class StepType(_StrEnum):
    THOUGHT = "thought"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    MODEL_CALL = "model_call"
    HANDOFF = "handoff"


class SourceFidelity(_StrEnum):
    FULL = "full"          # direct capture (SDK wrapper)
    PARTIAL = "partial"    # some fields missing (e.g. OTel without tool args)
    INFERRED = "inferred"  # reconstructed (e.g. proxy reading the function-call loop)
    COARSE = "coarse"      # task/artifact granularity only (e.g. A2A agent boundary)


class PiiStatus(_StrEnum):
    RAW = "raw"
    SCRUBBED = "scrubbed"
    REDACTED = "redacted"


class LabelSource(_StrEnum):
    JUDGE = "judge"
    USER_EXPLICIT = "user_explicit"
    USER_IMPLICIT = "user_implicit"
    OUTCOME = "outcome"
    HUMAN = "human"


# ─── helpers ────────────────────────────────────────────────────────────────

def new_episode_id() -> str:
    return f"ep_{uuid.uuid4().hex}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    """Omit None values so JSON stays clean and absence is unambiguous."""
    return {k: v for k, v in d.items() if v is not None}


# ─── content ────────────────────────────────────────────────────────────────

@dataclass
class ContentPart:
    """One piece of multimodal content. Media is referenced, never inlined."""

    type: ContentType
    text: str | None = None        # for type == text
    ref: str | None = None         # URL or content-hash for media (NOT raw bytes)
    mime: str | None = None

    def __post_init__(self) -> None:
        self.type = ContentType.coerce(self.type)
        if self.type is ContentType.TEXT:
            if self.text is None:
                raise ValueError("ContentPart(type=text) requires `text`")
        else:
            if not self.ref:
                raise ValueError(f"ContentPart(type={self.type.value}) requires a `ref` (media by reference)")

    @staticmethod
    def text_part(text: str) -> "ContentPart":
        return ContentPart(type=ContentType.TEXT, text=text)

    def to_dict(self) -> dict[str, Any]:
        return _drop_none({"type": self.type.value, "text": self.text, "ref": self.ref, "mime": self.mime})

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ContentPart":
        return cls(type=d["type"], text=d.get("text"), ref=d.get("ref"), mime=d.get("mime"))


# ─── step ───────────────────────────────────────────────────────────────────

@dataclass
class Step:
    """One step within a turn (a tool call, model call, thought, etc.)."""

    step_index: int
    type: StepType
    tool_name: str | None = None
    tool_args: Any | None = None
    tool_result: Any | None = None
    duration_ms: float | None = None
    tokens: int | None = None
    error: str | None = None
    source_fidelity: SourceFidelity = SourceFidelity.FULL

    def __post_init__(self) -> None:
        self.type = StepType.coerce(self.type)
        self.source_fidelity = SourceFidelity.coerce(self.source_fidelity)

    def to_dict(self) -> dict[str, Any]:
        return _drop_none(
            {
                "step_index": self.step_index,
                "type": self.type.value,
                "tool_name": self.tool_name,
                "tool_args": self.tool_args,
                "tool_result": self.tool_result,
                "duration_ms": self.duration_ms,
                "tokens": self.tokens,
                "error": self.error,
                "source_fidelity": self.source_fidelity.value,
            }
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Step":
        return cls(
            step_index=d["step_index"],
            type=d["type"],
            tool_name=d.get("tool_name"),
            tool_args=d.get("tool_args"),
            tool_result=d.get("tool_result"),
            duration_ms=d.get("duration_ms"),
            tokens=d.get("tokens"),
            error=d.get("error"),
            source_fidelity=d.get("source_fidelity", SourceFidelity.FULL),
        )


# ─── turn ───────────────────────────────────────────────────────────────────

@dataclass
class Turn:
    """One conversational turn; sub-record rolled up into the task Episode."""

    turn_index: int
    user_content: list[ContentPart] = field(default_factory=list)
    agent_content: list[ContentPart] = field(default_factory=list)
    reasoning_content: str | None = None  # deep-thinking trace, separate from answer
    steps: list[Step] = field(default_factory=list)
    partial: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _drop_none(
            {
                "turn_index": self.turn_index,
                "user_content": [c.to_dict() for c in self.user_content],
                "agent_content": [c.to_dict() for c in self.agent_content],
                "reasoning_content": self.reasoning_content,
                "steps": [s.to_dict() for s in self.steps],
                "partial": self.partial,
            }
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Turn":
        return cls(
            turn_index=d["turn_index"],
            user_content=[ContentPart.from_dict(c) for c in d.get("user_content", [])],
            agent_content=[ContentPart.from_dict(c) for c in d.get("agent_content", [])],
            reasoning_content=d.get("reasoning_content"),
            steps=[Step.from_dict(s) for s in d.get("steps", [])],
            partial=bool(d.get("partial", False)),
        )


# ─── labels & outcomes ──────────────────────────────────────────────────────

@dataclass
class Label:
    """A quality annotation from one source. Many attach to an episode over time."""

    source: LabelSource
    score: float
    rubric_dim: str | None = None
    rationale: str | None = None
    confidence: float | None = None
    judge_model: str | None = None
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.source = LabelSource.coerce(self.source)
        if not 0.0 <= float(self.score) <= 1.0:
            raise ValueError(f"Label.score must be in [0,1]; got {self.score}")
        if self.confidence is not None and not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError(f"Label.confidence must be in [0,1]; got {self.confidence}")

    def to_dict(self) -> dict[str, Any]:
        return _drop_none(
            {
                "source": self.source.value,
                "score": self.score,
                "rubric_dim": self.rubric_dim,
                "rationale": self.rationale,
                "confidence": self.confidence,
                "judge_model": self.judge_model,
                "created_at": self.created_at,
            }
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Label":
        return cls(
            source=d["source"],
            score=d["score"],
            rubric_dim=d.get("rubric_dim"),
            rationale=d.get("rationale"),
            confidence=d.get("confidence"),
            judge_model=d.get("judge_model"),
            created_at=d.get("created_at", utc_now_iso()),
        )


@dataclass
class Outcome:
    """Delayed ground-truth signal, joined to an episode by ``trace_id``."""

    trace_id: str
    resolved: bool | None = None
    correct: bool | None = None
    downstream_metric: float | None = None
    arrived_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return _drop_none(
            {
                "trace_id": self.trace_id,
                "resolved": self.resolved,
                "correct": self.correct,
                "downstream_metric": self.downstream_metric,
                "arrived_at": self.arrived_at,
            }
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Outcome":
        return cls(
            trace_id=d["trace_id"],
            resolved=d.get("resolved"),
            correct=d.get("correct"),
            downstream_metric=d.get("downstream_metric"),
            arrived_at=d.get("arrived_at", utc_now_iso()),
        )


# ─── episode (the unit of learning) ─────────────────────────────────────────

@dataclass
class Episode:
    """One task execution — the atomic unit of learning. See ADR 0007."""

    task_input: str
    collector: Collector
    episode_id: str = field(default_factory=new_episode_id)
    schema_version: int = SCHEMA_VERSION
    trace_id: str | None = None              # correlation id (best-effort)
    generation_id: str | None = None
    agent_id: str | None = None              # which parent agent produced this (for the improvement loop)
    agent_artifact_hash: str | None = None
    ts_start: str = field(default_factory=utc_now_iso)
    ts_end: str | None = None
    input_context: list[str] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    final_output: str | None = None
    # metrics (all optional — fidelity varies by collector)
    latency_ms: float | None = None
    ttft_ms: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    model_id: str | None = None
    pii_status: PiiStatus = PiiStatus.RAW
    retention_until: str | None = None
    partial: bool = False
    labels: list[Label] = field(default_factory=list)
    outcome: Outcome | None = None

    def __post_init__(self) -> None:
        if not self.task_input or not str(self.task_input).strip():
            raise ValueError("Episode.task_input must be a non-empty string")
        self.collector = Collector.coerce(self.collector)
        self.pii_status = PiiStatus.coerce(self.pii_status)

    # -- convenience -----------------------------------------------------
    def add_turn(self, turn: Turn) -> Turn:
        self.turns.append(turn)
        return turn

    def add_label(self, label: Label) -> Label:
        self.labels.append(label)
        return label

    # -- serialization ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return _drop_none(
            {
                "episode_id": self.episode_id,
                "schema_version": self.schema_version,
                "collector": self.collector.value,
                "trace_id": self.trace_id,
                "generation_id": self.generation_id,
                "agent_id": self.agent_id,
                "agent_artifact_hash": self.agent_artifact_hash,
                "ts_start": self.ts_start,
                "ts_end": self.ts_end,
                "task_input": self.task_input,
                "input_context": list(self.input_context),
                "turns": [t.to_dict() for t in self.turns],
                "final_output": self.final_output,
                "latency_ms": self.latency_ms,
                "ttft_ms": self.ttft_ms,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "cost_usd": self.cost_usd,
                "model_id": self.model_id,
                "pii_status": self.pii_status.value,
                "retention_until": self.retention_until,
                "partial": self.partial,
                "labels": [l.to_dict() for l in self.labels],
                "outcome": self.outcome.to_dict() if self.outcome else None,
            }
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Episode":
        return cls(
            task_input=d["task_input"],
            collector=d["collector"],
            episode_id=d.get("episode_id", new_episode_id()),
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            trace_id=d.get("trace_id"),
            generation_id=d.get("generation_id"),
            agent_id=d.get("agent_id"),
            agent_artifact_hash=d.get("agent_artifact_hash"),
            ts_start=d.get("ts_start", utc_now_iso()),
            ts_end=d.get("ts_end"),
            input_context=list(d.get("input_context", [])),
            turns=[Turn.from_dict(t) for t in d.get("turns", [])],
            final_output=d.get("final_output"),
            latency_ms=d.get("latency_ms"),
            ttft_ms=d.get("ttft_ms"),
            tokens_in=d.get("tokens_in"),
            tokens_out=d.get("tokens_out"),
            cost_usd=d.get("cost_usd"),
            model_id=d.get("model_id"),
            pii_status=d.get("pii_status", PiiStatus.RAW),
            retention_until=d.get("retention_until"),
            partial=bool(d.get("partial", False)),
            labels=[Label.from_dict(l) for l in d.get("labels", [])],
            outcome=Outcome.from_dict(d["outcome"]) if d.get("outcome") else None,
        )
