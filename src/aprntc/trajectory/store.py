"""Trajectory Store — the system of record for episodes (Stage 1 part 2).

SQLite to bootstrap (stdlib only; Postgres-ready later behind the same API).
Design (ADR 0007 + privacy invariants):

* **Scrub at ingest** — :func:`scrub_episode` runs before persistence unless the
  episode is already marked scrubbed (or the caller opts out explicitly).
* **Episode body is immutable**; **labels and outcomes ATTACH over time**
  (append-only) via :meth:`attach_label` / :meth:`attach_outcome`.
* **Fused reward** — :meth:`fused_reward` computes a confidence-weighted reward
  with reliability priority ``outcome > explicit > implicit > judge`` (ADR 0006).
  MVP uses fixed weights (learned weights deferred to v1+).
* **Privacy ops** — :meth:`delete_by_subject` (GDPR) + :meth:`purge_expired`
  (retention TTL).

The full Episode is stored as JSON (the canonical shape), with key columns
promoted for querying. This keeps the schema authoritative and the store thin.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from aprntc.trajectory.pii import scrub_episode, scrub_text
from aprntc.trajectory.schema import Episode, Label, LabelSource, Outcome, PiiStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    episode_id      TEXT PRIMARY KEY,
    trace_id        TEXT,
    generation_id   TEXT,
    collector       TEXT NOT NULL,
    pii_status      TEXT NOT NULL,
    ts_start        TEXT NOT NULL,
    retention_until TEXT,
    partial         INTEGER NOT NULL DEFAULT 0,
    body            TEXT NOT NULL          -- full Episode JSON (canonical)
);
CREATE INDEX IF NOT EXISTS idx_episodes_trace      ON episodes(trace_id);
CREATE INDEX IF NOT EXISTS idx_episodes_generation ON episodes(generation_id);
CREATE INDEX IF NOT EXISTS idx_episodes_ts         ON episodes(ts_start);

-- Labels attach over time (append-only); episode body is not rewritten for them.
CREATE TABLE IF NOT EXISTS labels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id  TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    score       REAL NOT NULL,
    rubric_dim  TEXT,
    confidence  REAL,
    judge_model TEXT,
    created_at  TEXT NOT NULL,
    body        TEXT NOT NULL          -- full Label JSON
);
CREATE INDEX IF NOT EXISTS idx_labels_episode ON labels(episode_id);

-- Delayed outcomes, joined by trace_id.
CREATE TABLE IF NOT EXISTS outcomes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id          TEXT NOT NULL,
    resolved          INTEGER,
    correct           INTEGER,
    downstream_metric REAL,
    arrived_at        TEXT NOT NULL,
    body              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_trace ON outcomes(trace_id);

-- Subject map for delete_by_subject (GDPR): subject_id -> episode_id.
CREATE TABLE IF NOT EXISTS subject_episodes (
    subject_id TEXT NOT NULL,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
    PRIMARY KEY (subject_id, episode_id)
);
"""

# Reliability priority for confidence-weighted fusion (ADR 0006). Higher = more trusted.
_SOURCE_WEIGHT = {
    LabelSource.OUTCOME: 1.0,
    LabelSource.HUMAN: 0.9,
    LabelSource.USER_EXPLICIT: 0.7,
    LabelSource.USER_IMPLICIT: 0.5,
    LabelSource.JUDGE: 0.4,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TrajectoryStore:
    """SQLite-backed system of record for trajectories."""

    def __init__(self, db_path: str | os.PathLike[str] = "aprntc.db") -> None:
        self._path = str(db_path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the web API (FastAPI/Starlette) services requests on
        # a thread pool, so the connection is touched from threads other than the one that
        # created it. SQLite serializes access internally; we don't share cursors across
        # threads, so this is safe here.
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "TrajectoryStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writing ---------------------------------------------------------

    def put_episode(
        self,
        episode: Episode,
        *,
        scrub: bool = True,
        subject_ids: Iterable[str] | None = None,
    ) -> str:
        """Persist an episode (scrubbing PII at ingest by default). Returns its id.

        ``subject_ids`` registers the episode against data subjects so it can be
        deleted later via :meth:`delete_by_subject` (GDPR).
        """
        if scrub and episode.pii_status is not PiiStatus.SCRUBBED:
            episode = scrub_episode(episode)
        body = json.dumps(episode.to_dict(), ensure_ascii=False)
        self._conn.execute(
            """INSERT OR REPLACE INTO episodes
               (episode_id, trace_id, generation_id, collector, pii_status,
                ts_start, retention_until, partial, body)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                episode.episode_id, episode.trace_id, episode.generation_id,
                episode.collector.value, episode.pii_status.value,
                episode.ts_start, episode.retention_until,
                1 if episode.partial else 0, body,
            ),
        )
        # Persist any labels/outcome carried on the episode into their tables too.
        for label in episode.labels:
            self._insert_label(episode.episode_id, label)
        if episode.outcome is not None:
            self._insert_outcome(episode.outcome)
        for sid in subject_ids or ():
            self._conn.execute(
                "INSERT OR IGNORE INTO subject_episodes (subject_id, episode_id) VALUES (?,?)",
                (sid, episode.episode_id),
            )
        self._conn.commit()
        return episode.episode_id

    def attach_label(self, episode_id: str, label: Label) -> None:
        """Append a label to an existing episode (append-only)."""
        if not self._exists(episode_id):
            raise KeyError(f"no episode {episode_id!r}")
        # Scrub any free-text rationale at ingest.
        if label.rationale:
            from dataclasses import replace
            label = replace(label, rationale=scrub_text(label.rationale))
        self._insert_label(episode_id, label)
        self._conn.commit()

    def attach_outcome(self, outcome: Outcome) -> None:
        """Record a delayed outcome (joined to episodes by ``trace_id``)."""
        self._insert_outcome(outcome)
        self._conn.commit()

    def _insert_label(self, episode_id: str, label: Label) -> None:
        self._conn.execute(
            """INSERT INTO labels
               (episode_id, source, score, rubric_dim, confidence, judge_model, created_at, body)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                episode_id, label.source.value, label.score, label.rubric_dim,
                label.confidence, label.judge_model, label.created_at,
                json.dumps(label.to_dict(), ensure_ascii=False),
            ),
        )

    def _insert_outcome(self, outcome: Outcome) -> None:
        self._conn.execute(
            """INSERT INTO outcomes
               (trace_id, resolved, correct, downstream_metric, arrived_at, body)
               VALUES (?,?,?,?,?,?)""",
            (
                outcome.trace_id,
                None if outcome.resolved is None else int(outcome.resolved),
                None if outcome.correct is None else int(outcome.correct),
                outcome.downstream_metric, outcome.arrived_at,
                json.dumps(outcome.to_dict(), ensure_ascii=False),
            ),
        )

    # -- reading ---------------------------------------------------------

    def get_episode(self, episode_id: str) -> Episode:
        row = self._conn.execute(
            "SELECT body FROM episodes WHERE episode_id = ?", (episode_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no episode {episode_id!r}")
        return Episode.from_dict(json.loads(row["body"]))

    def labels_for(self, episode_id: str) -> list[Label]:
        rows = self._conn.execute(
            "SELECT body FROM labels WHERE episode_id = ? ORDER BY id", (episode_id,)
        ).fetchall()
        return [Label.from_dict(json.loads(r["body"])) for r in rows]

    def outcomes_for(self, trace_id: str) -> list[Outcome]:
        rows = self._conn.execute(
            "SELECT body FROM outcomes WHERE trace_id = ? ORDER BY id", (trace_id,)
        ).fetchall()
        return [Outcome.from_dict(json.loads(r["body"])) for r in rows]

    def query(
        self,
        *,
        generation_id: str | None = None,
        collector: str | None = None,
        limit: int | None = None,
    ) -> list[Episode]:
        sql = "SELECT body FROM episodes"
        clauses, params = [], []
        if generation_id is not None:
            clauses.append("generation_id = ?")
            params.append(generation_id)
        if collector is not None:
            clauses.append("collector = ?")
            params.append(collector)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts_start"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        rows = self._conn.execute(sql, params).fetchall()
        return [Episode.from_dict(json.loads(r["body"])) for r in rows]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS n FROM episodes").fetchone()["n"]

    def counts_by_collector(self) -> dict[str, int]:
        """Episode counts grouped by collector — for UI filter chips."""
        rows = self._conn.execute(
            "SELECT collector, COUNT(*) AS n FROM episodes GROUP BY collector"
        ).fetchall()
        return {r["collector"]: r["n"] for r in rows}

    # -- fused reward (ADR 0006: outcome-anchored, confidence-weighted) --

    def fused_reward(
        self,
        episode_id: str,
        *,
        weights: "dict[LabelSource, float] | None" = None,
    ) -> tuple[float, float] | None:
        """Return ``(reward, confidence)`` fused from all labels, or ``None`` if
        there are no labels.

        Confidence-weighted by source reliability. Each label contributes
        ``weight * confidence``; the reward is that-weighted mean of scores; the
        fused confidence is anchored by the most reliable source present.

        ``weights`` overrides the default fixed reliability map with **learned**
        weights (A3, from :func:`aprntc.eval.fusion.learn_weights`). When omitted,
        the fixed ``_SOURCE_WEIGHT`` priors are used (cold-start safe).
        """
        labels = self.labels_for(episode_id)
        if not labels:
            return None
        w_map = weights or _SOURCE_WEIGHT
        num = den = 0.0
        max_w = 0.0
        for lbl in labels:
            w = w_map.get(lbl.source, 0.3)
            c = lbl.confidence if lbl.confidence is not None else 1.0
            weight = w * c
            num += weight * lbl.score
            den += weight
            max_w = max(max_w, w)
        if den == 0.0:
            return None
        reward = num / den
        # Fused confidence: anchored by the most reliable source present.
        return (reward, max_w)

    def learn_fusion_weights(self, *, min_n: int = 5, blend: float = 0.5):
        """Learn per-source fusion weights from this store's label history (A3).

        Returns a :class:`aprntc.eval.fusion.FusionWeights`; pass ``.as_map()`` to
        ``fused_reward(weights=...)``. Imported lazily to keep the store core light.
        """
        from aprntc.eval.fusion import learn_weights

        all_labels = [self.labels_for(e.episode_id) for e in self.query()]
        return learn_weights(all_labels, min_n=min_n, blend=blend)

    # -- privacy ops -----------------------------------------------------

    def delete_by_subject(self, subject_id: str) -> int:
        """GDPR: delete all episodes (and cascading labels) for a data subject.

        Returns the number of episodes deleted.
        """
        rows = self._conn.execute(
            "SELECT episode_id FROM subject_episodes WHERE subject_id = ?", (subject_id,)
        ).fetchall()
        ids = [r["episode_id"] for r in rows]
        for eid in ids:
            self._conn.execute("DELETE FROM episodes WHERE episode_id = ?", (eid,))
        self._conn.execute("DELETE FROM subject_episodes WHERE subject_id = ?", (subject_id,))
        self._conn.commit()
        return len(ids)

    def purge_expired(self, *, now: datetime | None = None) -> int:
        """Delete episodes whose ``retention_until`` is in the past (TTL).

        Returns the number purged. Episodes with no ``retention_until`` are kept.
        """
        cutoff = (now or _utc_now()).isoformat()
        cur = self._conn.execute(
            "DELETE FROM episodes WHERE retention_until IS NOT NULL AND retention_until < ?",
            (cutoff,),
        )
        self._conn.commit()
        return cur.rowcount

    # -- internals -------------------------------------------------------

    def _exists(self, episode_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM episodes WHERE episode_id = ?", (episode_id,)
        ).fetchone() is not None
