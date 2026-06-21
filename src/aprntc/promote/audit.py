"""Append-only audit log for auto-promotion decisions.

Every time the auto-policy decides on a candidate (AUTO_PROMOTE / HUMAN_REVIEW /
REJECT), the decision is recorded — with the blocked guardrails, the trust signal,
and the candidate's playbook hash — so an operator can later answer "what did we
decide on candidate X, and why?". Written as JSONL (one record per line) for
trivial grep + zero schema migrations.

**Idempotent**: identical consecutive decisions on the same candidate aren't
duplicated (the dashboard's polling would otherwise spam the log). A real change
— policy toggle, trust threshold crossed, new candidate — produces a new line.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AuditRecord:
    ts: str                    # ISO-8601 UTC
    candidate_playbook_hash: str | None
    action: str                # "auto_promote" | "human_review" | "reject"
    policy_enabled: bool
    reasons: tuple[str, ...]
    trust_value: float | None
    trust_n: int
    gate_summary: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "candidate_playbook_hash": self.candidate_playbook_hash,
            "action": self.action,
            "policy_enabled": self.policy_enabled,
            "reasons": list(self.reasons),
            "trust_value": self.trust_value,
            "trust_n": self.trust_n,
            "gate_summary": self.gate_summary,
        }

    def signature(self) -> tuple:
        """The tuple we dedup on — change ANY field below ⇒ a new audit line."""
        return (self.candidate_playbook_hash, self.action,
                self.policy_enabled, self.reasons,
                self.trust_value, self.trust_n)


def append_audit(
    path: str | os.PathLike[str],
    *,
    candidate_playbook_hash: str | None,
    action: str,
    policy_enabled: bool,
    reasons: list[str] | tuple[str, ...],
    trust_value: float | None,
    trust_n: int = 0,
    gate_summary: str | None = None,
    now: _dt.datetime | None = None,
) -> AuditRecord | None:
    """Append a record to the JSONL log if it differs from the previous one.

    Returns the appended :class:`AuditRecord`, or ``None`` if the decision was a
    no-op duplicate of the last line. The caller can ignore the return value —
    side-effecting append is the point.
    """
    ts = (now or _dt.datetime.now(_dt.timezone.utc)).isoformat()
    rec = AuditRecord(
        ts=ts,
        candidate_playbook_hash=candidate_playbook_hash,
        action=action,
        policy_enabled=policy_enabled,
        reasons=tuple(reasons),
        trust_value=trust_value,
        trust_n=trust_n,
        gate_summary=gate_summary,
    )
    p = Path(path)
    last = _last_record(p)
    if last is not None and last.signature() == rec.signature():
        return None  # idempotent skip
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
    return rec


def read_audit(path: str | os.PathLike[str], *, limit: int | None = None) -> list[AuditRecord]:
    """Read records back, newest-first. ``limit`` caps the count."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[AuditRecord] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue  # robust to a partial/torn last line
        out.append(AuditRecord(
            ts=d.get("ts", ""),
            candidate_playbook_hash=d.get("candidate_playbook_hash"),
            action=d.get("action", "human_review"),
            policy_enabled=bool(d.get("policy_enabled", False)),
            reasons=tuple(d.get("reasons", [])),
            trust_value=d.get("trust_value"),
            trust_n=int(d.get("trust_n") or 0),
            gate_summary=d.get("gate_summary"),
        ))
    out.reverse()  # newest first
    if limit is not None:
        out = out[:limit]
    return out


def _last_record(path: Path) -> AuditRecord | None:
    """Read just the final line (cheap on tail-grow-only files)."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    last_line = next(
        (ln for ln in reversed(text.splitlines()) if ln.strip()),
        None,
    )
    if last_line is None:
        return None
    try:
        d = json.loads(last_line)
    except json.JSONDecodeError:
        return None
    return AuditRecord(
        ts=d.get("ts", ""),
        candidate_playbook_hash=d.get("candidate_playbook_hash"),
        action=d.get("action", "human_review"),
        policy_enabled=bool(d.get("policy_enabled", False)),
        reasons=tuple(d.get("reasons", [])),
        trust_value=d.get("trust_value"),
        trust_n=int(d.get("trust_n") or 0),
        gate_summary=d.get("gate_summary"),
    )
