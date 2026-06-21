"""Postgres-backed TrajectoryStore — mirrors :class:`TrajectoryStore`'s public API.

For multi-worker uvicorn deployments (SQLite serializes file writes; Postgres
handles concurrent writers cleanly). Schema is identical in shape to the SQLite
one; only the dialect differs (``BIGSERIAL`` autoincrement, ``%s`` placeholders,
``ON CONFLICT`` upserts). Same Episode→body-JSON contract — the row is a thin
wrapper around the canonical Episode shape.

Activated by ``APRNTC_DB_URL=postgresql://…`` (or by the URL-based factory
:func:`make_trajectory_store`). The ``[postgres]`` extra installs psycopg3.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable

from aprntc.trajectory.pii import scrub_episode, scrub_text
from aprntc.trajectory.schema import Episode, Label, LabelSource, Outcome, PiiStatus


# Same reliability priority as the SQLite store — kept in sync via the import
# in store.py. Imported lazily to avoid a circular import.
def _source_weights() -> dict[LabelSource, float]:
    from aprntc.trajectory.store import _SOURCE_WEIGHT
    return _SOURCE_WEIGHT


_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS episodes (
        episode_id      TEXT PRIMARY KEY,
        trace_id        TEXT,
        generation_id   TEXT,
        collector       TEXT NOT NULL,
        pii_status      TEXT NOT NULL,
        ts_start        TEXT NOT NULL,
        retention_until TEXT,
        partial         BOOLEAN NOT NULL DEFAULT FALSE,
        body            TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_episodes_trace      ON episodes(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_episodes_generation ON episodes(generation_id)",
    "CREATE INDEX IF NOT EXISTS idx_episodes_ts         ON episodes(ts_start)",
    """CREATE TABLE IF NOT EXISTS labels (
        id          BIGSERIAL PRIMARY KEY,
        episode_id  TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
        source      TEXT NOT NULL,
        score       DOUBLE PRECISION NOT NULL,
        rubric_dim  TEXT,
        confidence  DOUBLE PRECISION,
        judge_model TEXT,
        created_at  TEXT NOT NULL,
        body        TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_labels_episode ON labels(episode_id)",
    """CREATE TABLE IF NOT EXISTS outcomes (
        id                BIGSERIAL PRIMARY KEY,
        trace_id          TEXT NOT NULL,
        resolved          INTEGER,
        correct           INTEGER,
        downstream_metric DOUBLE PRECISION,
        arrived_at        TEXT NOT NULL,
        body              TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_trace ON outcomes(trace_id)",
    """CREATE TABLE IF NOT EXISTS subject_episodes (
        subject_id TEXT NOT NULL,
        episode_id TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
        PRIMARY KEY (subject_id, episode_id)
    )""",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PostgresTrajectoryStore:
    """Postgres-backed system of record. Mirrors :class:`TrajectoryStore`'s API.

    One ``psycopg.Connection`` per process. Cursors are short-lived (``with
    conn.cursor()``) so the connection is safe to share across FastAPI's thread
    pool — same idea as the SQLite ``check_same_thread=False`` trick.
    """

    def __init__(self, url: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ModuleNotFoundError as e:  # pragma: no cover - guidance path
            raise ModuleNotFoundError(
                "Postgres store needs psycopg — install: pip install 'aprntc[postgres]'"
            ) from e
        self._url = url
        self._conn = psycopg.connect(url, autocommit=False, row_factory=dict_row)
        with self._conn.cursor() as cur:
            for stmt in _SCHEMA_STATEMENTS:
                cur.execute(stmt)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PostgresTrajectoryStore":
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
        if scrub and episode.pii_status is not PiiStatus.SCRUBBED:
            episode = scrub_episode(episode)
        body = json.dumps(episode.to_dict(), ensure_ascii=False)
        with self._conn.cursor() as cur:
            # Postgres equivalent of "INSERT OR REPLACE": ON CONFLICT … DO UPDATE.
            cur.execute(
                """INSERT INTO episodes
                   (episode_id, trace_id, generation_id, collector, pii_status,
                    ts_start, retention_until, partial, body)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (episode_id) DO UPDATE SET
                     trace_id        = EXCLUDED.trace_id,
                     generation_id   = EXCLUDED.generation_id,
                     collector       = EXCLUDED.collector,
                     pii_status      = EXCLUDED.pii_status,
                     ts_start        = EXCLUDED.ts_start,
                     retention_until = EXCLUDED.retention_until,
                     partial         = EXCLUDED.partial,
                     body            = EXCLUDED.body""",
                (
                    episode.episode_id, episode.trace_id, episode.generation_id,
                    episode.collector.value, episode.pii_status.value,
                    episode.ts_start, episode.retention_until,
                    episode.partial, body,
                ),
            )
            for label in episode.labels:
                self._insert_label(cur, episode.episode_id, label)
            if episode.outcome is not None:
                self._insert_outcome(cur, episode.outcome)
            for sid in subject_ids or ():
                cur.execute(
                    "INSERT INTO subject_episodes (subject_id, episode_id) "
                    "VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (sid, episode.episode_id),
                )
        self._conn.commit()
        return episode.episode_id

    def attach_label(self, episode_id: str, label: Label) -> None:
        if not self._exists(episode_id):
            raise KeyError(f"no episode {episode_id!r}")
        if label.rationale:
            from dataclasses import replace
            label = replace(label, rationale=scrub_text(label.rationale))
        with self._conn.cursor() as cur:
            self._insert_label(cur, episode_id, label)
        self._conn.commit()

    def attach_outcome(self, outcome: Outcome) -> None:
        with self._conn.cursor() as cur:
            self._insert_outcome(cur, outcome)
        self._conn.commit()

    def _insert_label(self, cur: Any, episode_id: str, label: Label) -> None:
        cur.execute(
            """INSERT INTO labels
               (episode_id, source, score, rubric_dim, confidence, judge_model, created_at, body)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                episode_id, label.source.value, label.score, label.rubric_dim,
                label.confidence, label.judge_model, label.created_at,
                json.dumps(label.to_dict(), ensure_ascii=False),
            ),
        )

    def _insert_outcome(self, cur: Any, outcome: Outcome) -> None:
        cur.execute(
            """INSERT INTO outcomes
               (trace_id, resolved, correct, downstream_metric, arrived_at, body)
               VALUES (%s,%s,%s,%s,%s,%s)""",
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
        with self._conn.cursor() as cur:
            cur.execute("SELECT body FROM episodes WHERE episode_id = %s", (episode_id,))
            row = cur.fetchone()
        if row is None:
            raise KeyError(f"no episode {episode_id!r}")
        return Episode.from_dict(json.loads(row["body"]))

    def labels_for(self, episode_id: str) -> list[Label]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT body FROM labels WHERE episode_id = %s ORDER BY id",
                (episode_id,),
            )
            rows = cur.fetchall()
        return [Label.from_dict(json.loads(r["body"])) for r in rows]

    def outcomes_for(self, trace_id: str) -> list[Outcome]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT body FROM outcomes WHERE trace_id = %s ORDER BY id",
                (trace_id,),
            )
            rows = cur.fetchall()
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
            clauses.append("generation_id = %s")
            params.append(generation_id)
        if collector is not None:
            clauses.append("collector = %s")
            params.append(collector)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts_start"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [Episode.from_dict(json.loads(r["body"])) for r in rows]

    def count(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM episodes")
            return cur.fetchone()["n"]

    def counts_by_collector(self) -> dict[str, int]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT collector, COUNT(*) AS n FROM episodes GROUP BY collector")
            rows = cur.fetchall()
        return {r["collector"]: r["n"] for r in rows}

    # -- fused reward ---------------------------------------------------

    def fused_reward(
        self,
        episode_id: str,
        *,
        weights: "dict[LabelSource, float] | None" = None,
    ) -> tuple[float, float] | None:
        labels = self.labels_for(episode_id)
        if not labels:
            return None
        w_map = weights or _source_weights()
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
        return (num / den, max_w)

    def learn_fusion_weights(self, *, min_n: int = 5, blend: float = 0.5):
        from aprntc.eval.fusion import learn_weights
        all_labels = [self.labels_for(e.episode_id) for e in self.query()]
        return learn_weights(all_labels, min_n=min_n, blend=blend)

    # -- privacy ops ----------------------------------------------------

    def delete_by_subject(self, subject_id: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT episode_id FROM subject_episodes WHERE subject_id = %s",
                (subject_id,),
            )
            ids = [r["episode_id"] for r in cur.fetchall()]
            for eid in ids:
                cur.execute("DELETE FROM episodes WHERE episode_id = %s", (eid,))
            cur.execute(
                "DELETE FROM subject_episodes WHERE subject_id = %s",
                (subject_id,),
            )
        self._conn.commit()
        return len(ids)

    def purge_expired(self, *, now: datetime | None = None) -> int:
        cutoff = (now or _utc_now()).isoformat()
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM episodes WHERE retention_until IS NOT NULL "
                "AND retention_until < %s",
                (cutoff,),
            )
            n = cur.rowcount
        self._conn.commit()
        return n

    # -- internals ------------------------------------------------------

    def _exists(self, episode_id: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute("SELECT 1 FROM episodes WHERE episode_id = %s", (episode_id,))
            return cur.fetchone() is not None
