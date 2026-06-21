"""PlaybookRegistry — per-agent active/historical playbooks an external agent fetches.

Each external agent has a chain of Playbook versions (G0, G1, …) and a pointer to
the **active** one. The agent calls ``get_active`` each request (or caches it); a
promotion calls ``set_active`` (or ``promote``) to flip the pointer; ``rollback``
flips it back. JSON-file backed, keyed by ``agent_id`` (multi-tenant-ready: B2 adds
auth/isolation on top).

G0 acquisition (decision 2026-06-14): **register, fallback to infer.** The customer
registers their real system prompt as G0; if they don't, :func:`infer_g0_from_messages`
reconstructs an approximate G0 from the system prompt the tap observed in traffic.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aprntc.distill.playbook import Playbook


@dataclass
class ActivePlaybook:
    agent_id: str
    generation: int
    playbook: Playbook
    hash: str
    source: str  # "registered" | "inferred" | "promoted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "generation": self.generation,
            "hash": self.hash,
            "source": self.source,
            "playbook": self.playbook.to_dict(),
            # the rendered system prompt the agent actually uses (convenience)
            "rendered_prompt": self.playbook.render(),
        }


def infer_g0_from_messages(messages: list[dict[str, Any]]) -> Playbook | None:
    """Reconstruct an approximate G0 from an observed request's system message.

    Fallback when the customer didn't register G0 — the egress-proxy tap captures the
    full ``messages`` array, so the system prompt is visible.
    """
    for m in messages:
        if m.get("role") == "system":
            content = m.get("content")
            text = content if isinstance(content, str) else None
            if text and text.strip():
                return Playbook(system_prompt=text.strip(), generation=0)
    return None


class PlaybookRegistry:
    """Per-agent playbook versions + active pointer, fetched by external agents."""

    def __init__(self, path: str | os.PathLike[str] = "playbooks.json") -> None:
        self._path = Path(path)
        # {agent_id: {"active": gen, "versions": {gen: {playbook, source}}}}
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    # -- registration / G0 ----------------------------------------------
    def register(self, agent_id: str, playbook: Playbook, *, source: str = "registered") -> ActivePlaybook:
        """Register/replace an agent's G0 and make it active. Idempotent per agent."""
        pb = Playbook.from_dict({**playbook.to_dict(), "generation": 0})
        self._data[agent_id] = {
            "active": 0,
            "versions": {"0": {"playbook": pb.to_dict(), "source": source}},
        }
        self._save()
        return self.get_active(agent_id)

    def ensure_g0_from_traffic(self, agent_id: str, messages: list[dict[str, Any]]) -> ActivePlaybook | None:
        """If the agent has no playbook yet, infer G0 from observed traffic. No-op if
        already registered. Returns the active playbook, or None if inference failed."""
        if agent_id in self._data:
            return self.get_active(agent_id)
        pb = infer_g0_from_messages(messages)
        if pb is None:
            return None
        return self.register(agent_id, pb, source="inferred")

    # -- fetch (what the external agent calls) --------------------------
    def get_active(self, agent_id: str) -> ActivePlaybook:
        rec = self._data.get(agent_id)
        if not rec:
            raise KeyError(f"no playbook registered for agent {agent_id!r}")
        gen = rec["active"]
        v = rec["versions"][str(gen)]
        pb = Playbook.from_dict(v["playbook"])
        return ActivePlaybook(agent_id=agent_id, generation=gen, playbook=pb,
                              hash=pb.hash, source=v["source"])

    def has(self, agent_id: str) -> bool:
        return agent_id in self._data

    def agent_ids(self) -> list[str]:
        """All registered agent ids (for the autonomous improvement loop)."""
        return list(self._data.keys())

    # -- promotion / rollback (flip the active pointer) -----------------
    def promote(self, agent_id: str, playbook: Playbook) -> ActivePlaybook:
        """Add a new generation (active+1) and make it active."""
        rec = self._data.get(agent_id)
        if not rec:
            raise KeyError(f"no playbook for agent {agent_id!r}; register G0 first")
        new_gen = max(int(g) for g in rec["versions"]) + 1
        pb = Playbook.from_dict({**playbook.to_dict(), "generation": new_gen})
        rec["versions"][str(new_gen)] = {"playbook": pb.to_dict(), "source": "promoted"}
        rec["active"] = new_gen
        self._save()
        return self.get_active(agent_id)

    def set_active(self, agent_id: str, generation: int) -> ActivePlaybook:
        """Point active at an existing generation (rollback / re-promote)."""
        rec = self._data[agent_id]
        if str(generation) not in rec["versions"]:
            raise KeyError(f"agent {agent_id!r} has no generation {generation}")
        rec["active"] = generation
        self._save()
        return self.get_active(agent_id)

    def rollback(self, agent_id: str) -> ActivePlaybook:
        """Revert to the previous generation (active-1 if it exists)."""
        rec = self._data[agent_id]
        cur = rec["active"]
        prev = max((int(g) for g in rec["versions"] if int(g) < cur), default=None)
        if prev is None:
            raise RuntimeError(f"agent {agent_id!r} has no earlier generation to roll back to")
        return self.set_active(agent_id, prev)

    def history(self, agent_id: str) -> list[int]:
        return sorted(int(g) for g in self._data[agent_id]["versions"])

    # -- persistence -----------------------------------------------------
    def _load(self) -> None:
        if self._path.exists():
            self._data = json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
