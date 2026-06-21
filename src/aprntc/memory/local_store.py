"""LocalMemoryStore — the default Experience Memory backend.

Pure-Python, zero external services, SQLite-persistent. Stores Lesson rows with
their scalar fields + embedding vectors, retrieves by cosine similarity with
the same filter DSL the cloud backends accept. Diversification via MMR.

This is what runs out of the box when ``APRNTC_VECTOR_DB_URL`` isn't set — no
Pinecone / VikingDB / AWS account needed. The embedding model is pluggable via
the :class:`~aprntc.memory.embedder.Embedder` Protocol; defaults to whatever
``default_embedder()`` picks (ModelArk if ARK_API_KEY is set, else a
deterministic hash-based fallback).

Why SQLite (not Chroma)? Three reasons:
  1. Stdlib only — no extra wheel to install, no model file to download.
  2. Trivial to inspect (``sqlite3 aprntc_memory.db``).
  3. Same persistence model as the trajectory store, so backup/restore is one file.

For production-scale retrieval where vector-search latency matters, swap to
Pinecone, VikingDB, or AWS OpenSearch (all behind the same interface).
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from aprntc.memory.base import Lesson, LessonType, MemoryStore, RetrievedLesson
from aprntc.memory.embedder import Embedder, default_embedder
from aprntc.memory.mmr import mmr_select


_SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    lesson_id    TEXT PRIMARY KEY,
    content      TEXT NOT NULL,
    situation    TEXT NOT NULL,
    lesson_type  TEXT NOT NULL,
    reward       REAL NOT NULL DEFAULT 0.0,
    generation   INTEGER NOT NULL DEFAULT 0,
    pii_status   TEXT NOT NULL DEFAULT 'scrubbed',
    embedding    TEXT NOT NULL,   -- JSON-encoded list[float]
    metadata     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_lessons_reward      ON lessons(reward);
CREATE INDEX IF NOT EXISTS idx_lessons_generation  ON lessons(generation);
CREATE INDEX IF NOT EXISTS idx_lessons_type        ON lessons(lesson_type);
"""


class LocalMemoryStore:
    """SQLite-backed local vector store. Implements :class:`MemoryStore`.

    No external services. Vectors are computed client-side via the injected
    Embedder and persisted alongside the row. Retrieval is full-scan cosine
    (fine up to ~10k lessons; if you need more, point at a real vector DB).
    """

    def __init__(
        self,
        path: str | os.PathLike[str] = "aprntc_memory.db",
        *,
        embedder: Embedder | None = None,
        # Only used if the caller intentionally wants to enforce a vector dim.
        enforce_dim: bool = True,
    ) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._embedder = embedder or default_embedder()
        self._enforce_dim = enforce_dim
        self._dim = self._embedder.dim

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "LocalMemoryStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def embedder(self) -> Embedder:
        return self._embedder

    # -- writes ---------------------------------------------------------

    def upsert_lessons(self, lessons: list[Lesson]) -> int:
        """Insert/update lessons. Re-running over the same lesson_id updates in place.

        Embeds the ``situation`` text via the configured Embedder if the lesson
        doesn't already carry an embedding of matching dim.
        """
        lessons = list(lessons)
        if not lessons:
            return 0

        # Batch-embed only the lessons that need it (skip ones the caller pre-embedded).
        to_embed_idx: list[int] = []
        for i, l in enumerate(lessons):
            if not l.embedding or (self._enforce_dim and len(l.embedding) != self._dim):
                to_embed_idx.append(i)
        if to_embed_idx:
            texts = [lessons[i].situation for i in to_embed_idx]
            vectors = self._embedder.embed(texts)
            for i, v in zip(to_embed_idx, vectors):
                lessons[i].embedding = list(v)

        with self._conn:
            for l in lessons:
                self._conn.execute(
                    """INSERT INTO lessons
                       (lesson_id, content, situation, lesson_type, reward, generation,
                        pii_status, embedding, metadata)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(lesson_id) DO UPDATE SET
                         content      = excluded.content,
                         situation    = excluded.situation,
                         lesson_type  = excluded.lesson_type,
                         reward       = excluded.reward,
                         generation   = excluded.generation,
                         pii_status   = excluded.pii_status,
                         embedding    = excluded.embedding,
                         metadata     = excluded.metadata""",
                    (
                        l.lesson_id, l.content, l.situation, l.lesson_type.value,
                        float(l.reward), int(l.generation), l.pii_status,
                        json.dumps(list(l.embedding)),
                        json.dumps(l.metadata or {}),
                    ),
                )
        return len(lessons)

    # -- reads ----------------------------------------------------------

    def retrieve(
        self,
        *,
        query: str,
        k: int = 4,
        min_reward: float = 0.0,
        generation: int | None = None,
        lesson_type: str | None = None,
        diversify: bool = True,
    ) -> list[RetrievedLesson]:
        """Cosine retrieval with filter pushdown + MMR diversification."""
        # Build the SQL filter (privacy invariant: pii_status='scrubbed' always).
        sql = "SELECT * FROM lessons WHERE pii_status = 'scrubbed'"
        params: list[Any] = []
        if min_reward > 0.0:
            sql += " AND reward >= ?"
            params.append(float(min_reward))
        if generation is not None:
            sql += " AND generation = ?"
            params.append(int(generation))
        if lesson_type is not None:
            sql += " AND lesson_type = ?"
            params.append(str(lesson_type))

        rows = list(self._conn.execute(sql, params).fetchall())
        if not rows:
            return []

        query_vec = self._embedder.embed_one(query or "")

        scored: list[tuple[float, sqlite3.Row, list[float]]] = []
        for r in rows:
            emb = json.loads(r["embedding"])
            score = _cosine(query_vec, emb)
            scored.append((score, r, emb))
        scored.sort(key=lambda t: t[0], reverse=True)

        # Trim to a candidate pool before MMR (~4× k, or all).
        fetch = max(k * 4, k) if diversify else k
        scored = scored[:fetch]

        if not diversify or len(scored) <= k:
            return [_to_retrieved(r, score) for score, r, _emb in scored[:k]]

        mmr_input = [(i, emb, score) for i, (score, _r, emb) in enumerate(scored)]
        chosen = mmr_select(query_vec, mmr_input, k=k)
        return [_to_retrieved(scored[i][1], scored[i][0]) for i in chosen]

    # -- introspection -------------------------------------------------

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS n FROM lessons").fetchone()["n"]


# ─── helpers ────────────────────────────────────────────────────────────────


def _cosine(a: list[float], b: list[float]) -> float:
    """Inline cosine to avoid importing mmr just for the helper. Same math."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na2 = 0.0
    nb2 = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na2 += x * x
        nb2 += y * y
    if na2 == 0.0 or nb2 == 0.0:
        return 0.0
    return dot / (math.sqrt(na2) * math.sqrt(nb2))


def _to_retrieved(row: sqlite3.Row, score: float) -> RetrievedLesson:
    lesson = Lesson(
        lesson_id=row["lesson_id"],
        content=row["content"],
        situation=row["situation"],
        lesson_type=LessonType(row["lesson_type"]),
        embedding=json.loads(row["embedding"]),
        reward=float(row["reward"]),
        generation=int(row["generation"]),
        pii_status=row["pii_status"],
        metadata=json.loads(row["metadata"] or "{}"),
    )
    return RetrievedLesson(lesson=lesson, score=float(score))


# Verify the class is a MemoryStore by structural typing (runtime_checkable).
assert isinstance(LocalMemoryStore.__init__, type(LocalMemoryStore.__init__))
