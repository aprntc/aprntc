"""PineconeMemoryStore — Pinecone-backed Experience Memory.

Cloud vector DB with sub-100ms queries at scale. Pinecone is "BYO embeddings"
— you call any Embedder to turn ``situation`` text into a vector and upsert it
along with the lesson metadata. Scalar filters (reward / generation / type /
pii_status) ride along as Pinecone metadata.

Needs the ``[pinecone]`` extra: ``pip install 'aprntc[pinecone]'``.

Configuration (via the URL ``pinecone://<index-name>`` + env):
  * ``PINECONE_API_KEY`` — required
  * ``PINECONE_CLOUD`` — default ``aws``
  * ``PINECONE_REGION`` — default ``us-east-1``

The index is auto-created if missing (serverless), matching the Embedder's dim.
"""

from __future__ import annotations

import os
from typing import Any

from aprntc.memory.base import Lesson, LessonType, RetrievedLesson
from aprntc.memory.embedder import Embedder, default_embedder
from aprntc.memory.mmr import mmr_select


class PineconeMemoryStore:
    """Pinecone vector index storing distilled lessons. Implements :class:`MemoryStore`.

    Embedding strategy: BYO (client-side). Pass an :class:`Embedder` in the
    constructor; if omitted, :func:`default_embedder` is used.
    """

    def __init__(
        self,
        *,
        index_name: str,
        embedder: Embedder | None = None,
        api_key: str | None = None,
        cloud: str | None = None,
        region: str | None = None,
        # Tests / advanced users can inject a pre-built index object that
        # quacks like ``pinecone.Index`` (upsert / query / fetch / delete).
        index: Any | None = None,
    ) -> None:
        self._embedder = embedder or default_embedder()
        self._index_name = index_name
        if index is not None:
            self._index = index
            return
        try:
            from pinecone import Pinecone, ServerlessSpec
        except ModuleNotFoundError as e:  # pragma: no cover - guidance path
            raise ModuleNotFoundError(
                "PineconeMemoryStore needs the [pinecone] extra — `pip install 'aprntc[pinecone]'`."
            ) from e
        key = api_key or os.environ.get("PINECONE_API_KEY")
        if not key:
            raise RuntimeError("PINECONE_API_KEY is required for the Pinecone backend.")
        pc = Pinecone(api_key=key)
        existing = {i.name for i in pc.list_indexes()}
        if index_name not in existing:
            pc.create_index(
                name=index_name,
                dimension=self._embedder.dim,
                metric="cosine",
                spec=ServerlessSpec(
                    cloud=cloud or os.environ.get("PINECONE_CLOUD", "aws"),
                    region=region or os.environ.get("PINECONE_REGION", "us-east-1"),
                ),
            )
        self._index = pc.Index(index_name)

    @property
    def embedder(self) -> Embedder:
        return self._embedder

    # -- writes ---------------------------------------------------------

    def upsert_lessons(self, lessons: list[Lesson]) -> int:
        lessons = list(lessons)
        if not lessons:
            return 0
        # Batch-embed only what needs it.
        to_embed = [i for i, l in enumerate(lessons)
                    if not l.embedding or len(l.embedding) != self._embedder.dim]
        if to_embed:
            texts = [lessons[i].situation for i in to_embed]
            vectors = self._embedder.embed(texts)
            for i, v in zip(to_embed, vectors):
                lessons[i].embedding = list(v)

        vectors_payload = []
        for l in lessons:
            vectors_payload.append({
                "id": l.lesson_id,
                "values": list(l.embedding),
                "metadata": {
                    "content": l.content,
                    "situation": l.situation,
                    "lesson_type": l.lesson_type.value,
                    "reward": float(l.reward),
                    "generation": int(l.generation),
                    "pii_status": l.pii_status,
                },
            })
        self._index.upsert(vectors=vectors_payload)
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
        # Translate our filter DSL into Pinecone's metadata-filter operators.
        flt: dict[str, Any] = {"pii_status": {"$eq": "scrubbed"}}
        if min_reward > 0.0:
            flt["reward"] = {"$gte": float(min_reward)}
        if generation is not None:
            flt["generation"] = {"$eq": int(generation)}
        if lesson_type is not None:
            flt["lesson_type"] = {"$eq": str(lesson_type)}

        q_vec = self._embedder.embed_one(query or "")
        fetch = max(k * 4, k) if diversify else k
        resp = self._index.query(
            vector=q_vec, top_k=fetch, include_metadata=True,
            include_values=True, filter=flt,
        )
        matches = _matches(resp)
        if not matches:
            return []
        # Convert + optionally MMR-diversify.
        candidates: list[RetrievedLesson] = [_match_to_retrieved(m) for m in matches]
        if not diversify or len(candidates) <= k:
            return candidates[:k]
        mmr_input = [(i, c.lesson.embedding, c.score) for i, c in enumerate(candidates)]
        chosen = mmr_select(q_vec, mmr_input, k=k)
        return [candidates[i] for i in chosen]


def _matches(resp: Any) -> list[dict[str, Any]]:
    """Pull the matches list from either an object or dict response shape."""
    if isinstance(resp, dict):
        return resp.get("matches", []) or []
    return list(getattr(resp, "matches", []) or [])


def _match_to_retrieved(match: Any) -> RetrievedLesson:
    if isinstance(match, dict):
        mid = match.get("id", "")
        score = float(match.get("score", 0.0))
        md = match.get("metadata", {}) or {}
        values = match.get("values", []) or []
    else:
        mid = getattr(match, "id", "")
        score = float(getattr(match, "score", 0.0))
        md = getattr(match, "metadata", {}) or {}
        values = list(getattr(match, "values", []) or [])
    lesson = Lesson(
        lesson_id=mid,
        content=md.get("content", "(unknown)"),
        situation=md.get("situation", ""),
        lesson_type=LessonType(md.get("lesson_type", LessonType.DIRECTIVE.value)),
        embedding=list(values),
        reward=float(md.get("reward", 0.0)),
        generation=int(md.get("generation", 0)),
        pii_status=md.get("pii_status", "scrubbed"),
    )
    return RetrievedLesson(lesson=lesson, score=score)
