"""ChromaMemoryStore — optional local backend using ChromaDB.

Why this alongside :class:`LocalMemoryStore`? Chroma ships its own embedding
function (sentence-transformers / ONNX), so you get real semantic retrieval
without paying for an API call per upsert/query. Heavier dep (~80MB on first
install) but better quality than the hash-based fallback.

Needs the ``[chroma]`` extra: ``pip install 'aprntc[chroma]'``.
"""

from __future__ import annotations

import os
from typing import Any

from aprntc.memory.base import Lesson, LessonType, RetrievedLesson
from aprntc.memory.embedder import Embedder
from aprntc.memory.mmr import mmr_select


class ChromaMemoryStore:
    """Chroma collection storing distilled lessons. Implements :class:`MemoryStore`.

    Embedding strategy: by default uses Chroma's built-in embedding function (a
    small ONNX sentence-transformer). Pass ``embedder=…`` to override with our
    Embedder protocol — vectors are then computed client-side and sent to Chroma.
    """

    def __init__(
        self,
        persist_dir: str = "./aprntc_chroma",
        *,
        collection: str = "aprntc_lessons",
        embedder: Embedder | None = None,
        # Test/injection: a pre-built Chroma collection object.
        coll: Any | None = None,
    ) -> None:
        self._embedder = embedder  # None → Chroma uses its built-in embedding
        if coll is not None:
            self._coll = coll
            return
        try:
            import chromadb
        except ModuleNotFoundError as e:  # pragma: no cover - guidance path
            raise ModuleNotFoundError(
                "ChromaMemoryStore needs the [chroma] extra — `pip install 'aprntc[chroma]'`."
            ) from e
        os.makedirs(persist_dir, exist_ok=True)
        client = chromadb.PersistentClient(path=persist_dir)
        get_kwargs: dict[str, Any] = {"name": collection}
        # If user is using their own embedder we MUST NOT register Chroma's default
        # — set ef=None so Chroma never tries to embed; we'll always pass vectors.
        if embedder is not None:
            get_kwargs["embedding_function"] = None
        self._coll = client.get_or_create_collection(**get_kwargs)

    @property
    def embedder(self) -> Embedder | None:
        return self._embedder

    # -- writes ---------------------------------------------------------

    def upsert_lessons(self, lessons: list[Lesson]) -> int:
        lessons = list(lessons)
        if not lessons:
            return 0
        ids = [l.lesson_id for l in lessons]
        docs = [l.situation or l.content for l in lessons]
        metas = [
            {
                "content": l.content,
                "situation": l.situation,
                "lesson_type": l.lesson_type.value,
                "reward": float(l.reward),
                "generation": int(l.generation),
                "pii_status": l.pii_status,
            }
            for l in lessons
        ]
        kwargs: dict[str, Any] = {"ids": ids, "documents": docs, "metadatas": metas}
        if self._embedder is not None:
            # BYO vectors — make sure they're computed.
            to_embed = [i for i, l in enumerate(lessons)
                        if not l.embedding or len(l.embedding) != self._embedder.dim]
            if to_embed:
                vecs = self._embedder.embed([lessons[i].situation for i in to_embed])
                for i, v in zip(to_embed, vecs):
                    lessons[i].embedding = list(v)
            kwargs["embeddings"] = [list(l.embedding) for l in lessons]
        self._coll.upsert(**kwargs)
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
        fetch = max(k * 4, k) if diversify else k

        where: dict[str, Any] = {"pii_status": {"$eq": "scrubbed"}}
        if min_reward > 0.0:
            where["reward"] = {"$gte": float(min_reward)}
        if generation is not None:
            where["generation"] = {"$eq": int(generation)}
        if lesson_type is not None:
            where["lesson_type"] = {"$eq": str(lesson_type)}

        kwargs: dict[str, Any] = {"n_results": fetch, "where": where, "include": ["metadatas", "distances", "documents", "embeddings"]}
        if self._embedder is not None:
            q_vec = self._embedder.embed_one(query or "")
            kwargs["query_embeddings"] = [q_vec]
        else:
            kwargs["query_texts"] = [query or ""]
            q_vec = None

        resp = self._coll.query(**kwargs)
        # Chroma returns parallel lists wrapped in a single-query outer list.
        ids = (resp.get("ids") or [[]])[0]
        metas = (resp.get("metadatas") or [[]])[0]
        dists = (resp.get("distances") or [[]])[0]
        embs = (resp.get("embeddings") or [[]])[0] if "embeddings" in resp else []

        candidates: list[RetrievedLesson] = []
        for i, mid in enumerate(ids):
            md = metas[i] if i < len(metas) else {}
            dist = float(dists[i]) if i < len(dists) else 1.0
            # Chroma returns cosine DISTANCE — convert to similarity (1 - d).
            score = 1.0 - dist
            emb = list(embs[i]) if i < len(embs) else []
            lesson = Lesson(
                lesson_id=mid,
                content=md.get("content", "(unknown)"),
                situation=md.get("situation", ""),
                lesson_type=LessonType(md.get("lesson_type", LessonType.DIRECTIVE.value)),
                embedding=emb,
                reward=float(md.get("reward", 0.0)),
                generation=int(md.get("generation", 0)),
                pii_status=md.get("pii_status", "scrubbed"),
            )
            candidates.append(RetrievedLesson(lesson=lesson, score=score))

        if not diversify or len(candidates) <= k or q_vec is None:
            # Without our own embedding we can't MMR-diversify here.
            return candidates[:k]
        mmr_input = [(i, c.lesson.embedding, c.score) for i, c in enumerate(candidates)]
        chosen = mmr_select(q_vec, mmr_input, k=k)
        return [candidates[i] for i in chosen]
