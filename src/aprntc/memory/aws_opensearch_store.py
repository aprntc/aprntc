"""AwsOpenSearchMemoryStore — AWS OpenSearch (k-NN) backed Experience Memory.

AWS-native vector DB story: OpenSearch with the k-NN plugin. Works against
managed OpenSearch Service or a self-hosted cluster. BYO embeddings via the
injected :class:`Embedder` (same pattern as Pinecone).

Needs the ``[aws]`` extra: ``pip install 'aprntc[aws]'``.

Configuration (via the URL ``aws://host:port/index`` + env):
  * ``AWS_OPENSEARCH_USER`` + ``AWS_OPENSEARCH_PASSWORD`` — basic-auth (or
    use the standard AWS SDK signing via ``opensearch-py`` extras).
  * ``AWS_OPENSEARCH_USE_SSL`` — ``true`` (default) or ``false`` for local.

The index is auto-created if missing with the configured ``dim``.
"""

from __future__ import annotations

import os
from typing import Any

from aprntc.memory.base import Lesson, LessonType, RetrievedLesson
from aprntc.memory.embedder import Embedder, default_embedder
from aprntc.memory.mmr import mmr_select


class AwsOpenSearchMemoryStore:
    """OpenSearch k-NN index storing distilled lessons. Implements :class:`MemoryStore`.

    Vector field: ``embedding`` (knn_vector). Filter pushdown via bool/term
    queries. Same scalar fields the Pinecone + Local backends use.
    """

    def __init__(
        self,
        *,
        host: str,
        index: str,
        embedder: Embedder | None = None,
        user: str | None = None,
        password: str | None = None,
        use_ssl: bool | None = None,
        # Tests can inject a fake client implementing index / search / indices.create / etc.
        client: Any | None = None,
    ) -> None:
        self._embedder = embedder or default_embedder()
        self._index = index

        if client is not None:
            self._client = client
            self._ensure_index()
            return

        try:
            from opensearchpy import OpenSearch, RequestsHttpConnection
        except ModuleNotFoundError as e:  # pragma: no cover - guidance path
            raise ModuleNotFoundError(
                "AwsOpenSearchMemoryStore needs the [aws] extra — "
                "`pip install 'aprntc[aws]'`."
            ) from e

        if ":" in host:
            host_part, port_part = host.rsplit(":", 1)
            port = int(port_part)
        else:
            host_part, port = host, 9200
        use_ssl_flag = (
            use_ssl if use_ssl is not None
            else os.environ.get("AWS_OPENSEARCH_USE_SSL", "true").lower() == "true"
        )
        http_auth = None
        u = user or os.environ.get("AWS_OPENSEARCH_USER")
        p = password or os.environ.get("AWS_OPENSEARCH_PASSWORD")
        if u and p:
            http_auth = (u, p)
        self._client = OpenSearch(
            hosts=[{"host": host_part, "port": port}],
            http_auth=http_auth,
            use_ssl=use_ssl_flag,
            verify_certs=use_ssl_flag,
            connection_class=RequestsHttpConnection,
            timeout=20,
        )
        self._ensure_index()

    @property
    def embedder(self) -> Embedder:
        return self._embedder

    def _ensure_index(self) -> None:
        # Create the index lazily if missing, with k-NN enabled and our schema.
        try:
            exists = self._client.indices.exists(index=self._index)
        except Exception:  # pragma: no cover - defensive
            return
        if exists:
            return
        body = {
            "settings": {"index": {"knn": True, "knn.algo_param.ef_search": 100}},
            "mappings": {
                "properties": {
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": self._embedder.dim,
                        "method": {
                            "name": "hnsw",
                            "space_type": "cosinesimil",
                            "engine": "nmslib",
                        },
                    },
                    "content": {"type": "text"},
                    "situation": {"type": "text"},
                    "lesson_type": {"type": "keyword"},
                    "reward": {"type": "float"},
                    "generation": {"type": "integer"},
                    "pii_status": {"type": "keyword"},
                },
            },
        }
        self._client.indices.create(index=self._index, body=body)

    # -- writes ---------------------------------------------------------

    def upsert_lessons(self, lessons: list[Lesson]) -> int:
        lessons = list(lessons)
        if not lessons:
            return 0
        to_embed = [i for i, l in enumerate(lessons)
                    if not l.embedding or len(l.embedding) != self._embedder.dim]
        if to_embed:
            texts = [lessons[i].situation for i in to_embed]
            vectors = self._embedder.embed(texts)
            for i, v in zip(to_embed, vectors):
                lessons[i].embedding = list(v)

        for l in lessons:
            doc = {
                "content": l.content,
                "situation": l.situation,
                "lesson_type": l.lesson_type.value,
                "reward": float(l.reward),
                "generation": int(l.generation),
                "pii_status": l.pii_status,
                "embedding": list(l.embedding),
            }
            # ``id`` makes this an upsert via _index API.
            self._client.index(index=self._index, id=l.lesson_id, body=doc, refresh=False)
        # Single refresh after the batch.
        try:
            self._client.indices.refresh(index=self._index)
        except Exception:  # pragma: no cover - cluster may auto-refresh
            pass
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
        q_vec = self._embedder.embed_one(query or "")
        fetch = max(k * 4, k) if diversify else k

        filters: list[dict[str, Any]] = [{"term": {"pii_status": "scrubbed"}}]
        if min_reward > 0.0:
            filters.append({"range": {"reward": {"gte": float(min_reward)}}})
        if generation is not None:
            filters.append({"term": {"generation": int(generation)}})
        if lesson_type is not None:
            filters.append({"term": {"lesson_type": str(lesson_type)}})

        body = {
            "size": fetch,
            "query": {
                "bool": {
                    "filter": filters,
                    "must": {
                        "knn": {
                            "embedding": {"vector": q_vec, "k": fetch},
                        },
                    },
                },
            },
            "_source": True,
        }
        resp = self._client.search(index=self._index, body=body)
        hits = ((resp or {}).get("hits") or {}).get("hits") or []
        candidates: list[RetrievedLesson] = []
        for h in hits:
            src = h.get("_source") or {}
            score = float(h.get("_score") or 0.0)
            lesson = Lesson(
                lesson_id=h.get("_id", ""),
                content=src.get("content", "(unknown)"),
                situation=src.get("situation", ""),
                lesson_type=LessonType(src.get("lesson_type", LessonType.DIRECTIVE.value)),
                embedding=list(src.get("embedding") or []),
                reward=float(src.get("reward", 0.0)),
                generation=int(src.get("generation", 0)),
                pii_status=src.get("pii_status", "scrubbed"),
            )
            candidates.append(RetrievedLesson(lesson=lesson, score=score))

        if not diversify or len(candidates) <= k:
            return candidates[:k]
        mmr_input = [(i, c.lesson.embedding, c.score) for i, c in enumerate(candidates)]
        chosen = mmr_select(q_vec, mmr_input, k=k)
        return [candidates[i] for i in chosen]
