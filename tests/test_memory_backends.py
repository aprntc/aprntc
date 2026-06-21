"""Tests for the pluggable vector-DB backends (LocalMemoryStore default + factory
dispatch + Pinecone/AWS adapters via injected fakes).

LocalMemoryStore exercises the actual code paths against SQLite + the hash
embedder — no external services needed.

The Pinecone + AWS adapters are exercised against in-process fakes that mimic
the live client APIs (upsert/query). The live integrations are gated on real
credentials (separate env-gated test files, not run by default).
"""

from __future__ import annotations

import pytest

from aprntc.memory import (
    HashEmbedder,
    Lesson,
    LessonType,
    LocalMemoryStore,
    MemoryStore,
    make_memory_store,
)


# ─── Embedder ────────────────────────────────────────────────────────────────


def test_hash_embedder_deterministic():
    e1, e2 = HashEmbedder(), HashEmbedder()
    v1 = e1.embed_one("hello world")
    v2 = e2.embed_one("hello world")
    assert v1 == v2
    assert len(v1) == 256


def test_hash_embedder_different_inputs_diverge():
    e = HashEmbedder()
    a = e.embed_one("refund policy")
    b = e.embed_one("video generation")
    # Cosine should be well below 1.0 for different texts.
    from aprntc.memory import cosine
    assert cosine(a, b) < 0.7


def test_hash_embedder_dim_normalized():
    """L2-normalised — magnitude near 1.0."""
    import math
    e = HashEmbedder()
    v = e.embed_one("the quick brown fox jumps over the lazy dog")
    norm = math.sqrt(sum(x * x for x in v))
    assert abs(norm - 1.0) < 1e-6


# ─── LocalMemoryStore ───────────────────────────────────────────────────────


@pytest.fixture()
def store():
    s = LocalMemoryStore(":memory:", embedder=HashEmbedder())
    yield s
    s.close()


def _les(content: str, situation: str, **kw) -> Lesson:
    kw.setdefault("lesson_type", LessonType.DIRECTIVE)
    kw.setdefault("reward", 0.8)
    return Lesson(content=content, situation=situation, **kw)


def test_local_is_a_memorystore(store):
    assert isinstance(store, MemoryStore)


def test_local_upsert_and_retrieve(store):
    lessons = [
        _les("Always cite the doc section", "refund question"),
        _les("Pass an image as image_url part", "image generation"),
        _les("Set thinking type to enabled", "deep reasoning"),
    ]
    n = store.upsert_lessons(lessons)
    assert n == 3
    assert store.count() == 3
    hits = store.retrieve(query="refund", k=2)
    assert len(hits) <= 2 and any("refund" in h.lesson.situation for h in hits)


def test_local_reupsert_updates_in_place(store):
    """Same lesson_id → UPDATE (not duplicate). Stable id is content-derived."""
    l = _les("v1 content", "same situation")
    store.upsert_lessons([l])
    l2 = Lesson(content=l.content, situation=l.situation,
                lesson_type=l.lesson_type, reward=0.95, generation=2,
                lesson_id=l.lesson_id)
    store.upsert_lessons([l2])
    assert store.count() == 1  # not duplicated
    # The updated reward survives.
    hits = store.retrieve(query="same situation", k=1)
    assert hits[0].lesson.reward == pytest.approx(0.95)


def test_local_filter_pushdown(store):
    store.upsert_lessons([
        _les("low quality", "x", reward=0.2),
        _les("high quality", "x", reward=0.9),
        _les("g2 lesson", "x", reward=0.8, generation=2),
    ])
    # min_reward filters out the 0.2 row.
    hits = store.retrieve(query="x", k=10, min_reward=0.5)
    assert all(h.lesson.reward >= 0.5 for h in hits)
    assert len(hits) == 2
    # generation filter narrows to the one G2 row.
    hits_g2 = store.retrieve(query="x", k=10, generation=2)
    assert len(hits_g2) == 1 and hits_g2[0].lesson.generation == 2


def test_local_excludes_unscrubbed_rows(store):
    """Privacy invariant — pii_status != 'scrubbed' must NEVER appear in retrievals."""
    store.upsert_lessons([
        _les("clean lesson", "topic"),
        Lesson(content="dirty", situation="topic", lesson_type=LessonType.DIRECTIVE,
               reward=0.9, pii_status="unscrubbed"),
    ])
    hits = store.retrieve(query="topic", k=10)
    assert all(h.lesson.pii_status == "scrubbed" for h in hits)


# ─── Factory dispatch ───────────────────────────────────────────────────────


def test_factory_default_is_local(tmp_path, monkeypatch):
    monkeypatch.delenv("APRNTC_VECTOR_DB_URL", raising=False)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    s = make_memory_store(f"local:///{tmp_path}/m.db")
    try:
        assert isinstance(s, LocalMemoryStore)
        # Round-trip a lesson through the factory-built store.
        s.upsert_lessons([_les("hello", "world")])
        assert s.count() == 1
    finally:
        s.close()


def test_factory_plain_path_is_local(tmp_path):
    s = make_memory_store(str(tmp_path / "scratch.db"))
    try:
        assert isinstance(s, LocalMemoryStore)
    finally:
        s.close()


def test_factory_unknown_scheme_raises():
    with pytest.raises(ValueError, match="Unknown vector-DB scheme"):
        make_memory_store("mongodb://nope")


def test_factory_chroma_without_extra_raises():
    """chroma:// without [chroma] installed raises with a clear message."""
    import sys
    # Simulate not-installed by removing the module if it happens to be present.
    if "chromadb" in sys.modules:
        del sys.modules["chromadb"]
    # If the module was never installed, the inner import raises ModuleNotFoundError.
    try:
        import chromadb  # noqa: F401 - probing
        installed = True
    except ModuleNotFoundError:
        installed = False
    if installed:
        pytest.skip("chromadb is installed; can't test the missing-extra branch here.")
    with pytest.raises(ModuleNotFoundError, match=r"\[chroma\] extra"):
        make_memory_store("chroma:///./scratch")


# ─── Pinecone adapter (offline via injected fake index) ─────────────────────


class _FakePineconeIndex:
    """In-process fake mimicking the upsert/query subset our adapter uses."""

    def __init__(self) -> None:
        self.vectors: dict[str, dict] = {}
        self.last_query: dict = {}

    def upsert(self, *, vectors):
        for v in vectors:
            self.vectors[v["id"]] = v

    def query(self, *, vector, top_k, include_metadata=True, include_values=False, filter=None):
        # Tiny cosine over our in-process state, honouring the metadata filter.
        from aprntc.memory import cosine
        self.last_query = {"vector": vector, "top_k": top_k, "filter": filter}
        matches = []
        for vid, v in self.vectors.items():
            md = v.get("metadata") or {}
            if not _match_filter(md, filter):
                continue
            score = cosine(vector, v.get("values") or [])
            matches.append({
                "id": vid, "score": score,
                "metadata": md, "values": v.get("values") if include_values else None,
            })
        matches.sort(key=lambda m: m["score"], reverse=True)
        return {"matches": matches[:top_k]}


def _match_filter(md: dict, flt: dict | None) -> bool:
    """Tiny subset of Pinecone's metadata-filter DSL: $eq + $gte + implicit eq."""
    if not flt:
        return True
    for k, cond in flt.items():
        val = md.get(k)
        if isinstance(cond, dict):
            if "$eq" in cond and val != cond["$eq"]:
                return False
            if "$gte" in cond and (val is None or val < cond["$gte"]):
                return False
        else:
            if val != cond:
                return False
    return True


def test_pinecone_adapter_upserts_and_queries():
    from aprntc.memory.pinecone_store import PineconeMemoryStore
    fake = _FakePineconeIndex()
    s = PineconeMemoryStore(index_name="aprntc-test", embedder=HashEmbedder(), index=fake)
    s.upsert_lessons([
        _les("Cite the refund policy", "refund question"),
        _les("Pass image_url", "image generation"),
    ])
    # Vectors went up to the index — exactly two.
    assert len(fake.vectors) == 2
    # Each has the metadata fields the adapter promises.
    one = next(iter(fake.vectors.values()))
    assert {"content", "situation", "lesson_type", "reward", "generation", "pii_status"} <= set(one["metadata"])

    hits = s.retrieve(query="refund", k=2)
    assert any("refund" in h.lesson.situation for h in hits)
    # The adapter constructed the right filter shape.
    assert fake.last_query["filter"]["pii_status"] == {"$eq": "scrubbed"}


def test_pinecone_adapter_min_reward_filter():
    from aprntc.memory.pinecone_store import PineconeMemoryStore
    fake = _FakePineconeIndex()
    s = PineconeMemoryStore(index_name="aprntc-test", embedder=HashEmbedder(), index=fake)
    s.upsert_lessons([
        _les("low", "topic", reward=0.2),
        _les("high", "topic", reward=0.9),
    ])
    hits = s.retrieve(query="topic", k=10, min_reward=0.5)
    assert all(h.lesson.reward >= 0.5 for h in hits)


# ─── AWS OpenSearch adapter (offline via injected fake client) ──────────────


class _FakeOpenSearchClient:
    """Mimics the index / search / indices subset our adapter uses."""

    def __init__(self) -> None:
        self.docs: dict[tuple[str, str], dict] = {}  # (index, id) -> _source
        self.indices = self  # so adapter can call client.indices.* on us
        self.last_search: dict = {}

    # indices.* facade
    def exists(self, *, index: str) -> bool:
        return any(k[0] == index for k in self.docs)

    def create(self, *, index: str, body: dict) -> None:
        # Touch a sentinel so exists() returns True next time.
        self.docs.setdefault((index, "__init__"), {})

    def refresh(self, *, index: str) -> None:
        pass

    # client.* facade
    def index(self, *, index: str, id: str, body: dict, refresh: bool = False) -> None:
        self.docs[(index, id)] = body

    def search(self, *, index: str, body: dict):
        from aprntc.memory import cosine
        self.last_search = {"index": index, "body": body}
        q_vec = (((body.get("query") or {}).get("bool") or {}).get("must") or {}).get("knn", {})
        # knn shape: {"embedding": {"vector": [...], "k": N}}
        knn_inner = next(iter(q_vec.values())) if q_vec else {}
        vec = knn_inner.get("vector") or []
        filters = (((body.get("query") or {}).get("bool") or {}).get("filter") or [])

        hits = []
        for (idx, doc_id), src in self.docs.items():
            if idx != index or doc_id == "__init__":
                continue
            if not _aws_match(src, filters):
                continue
            hits.append({"_id": doc_id, "_source": src, "_score": cosine(vec, src.get("embedding") or [])})
        hits.sort(key=lambda h: h["_score"], reverse=True)
        size = body.get("size") or 4
        return {"hits": {"hits": hits[:size]}}


def _aws_match(src: dict, filters: list[dict]) -> bool:
    for f in filters:
        if "term" in f:
            for k, v in f["term"].items():
                if src.get(k) != v:
                    return False
        if "range" in f:
            for k, cond in f["range"].items():
                if "gte" in cond and (src.get(k) is None or src[k] < cond["gte"]):
                    return False
    return True


def test_aws_adapter_upserts_and_queries():
    from aprntc.memory.aws_opensearch_store import AwsOpenSearchMemoryStore
    fake = _FakeOpenSearchClient()
    s = AwsOpenSearchMemoryStore(host="x:0", index="lessons",
                                  embedder=HashEmbedder(), client=fake)
    s.upsert_lessons([
        _les("Cite the doc", "refund question"),
        _les("Pass image_url", "image generation"),
    ])
    # Both docs landed (plus the __init__ sentinel created by create).
    real_docs = {k: v for k, v in fake.docs.items() if k[1] != "__init__"}
    assert len(real_docs) == 2

    hits = s.retrieve(query="refund", k=2)
    assert any("refund" in h.lesson.situation for h in hits)
    # Adapter constructed the bool/filter/knn shape correctly.
    body = fake.last_search["body"]
    assert "knn" in body["query"]["bool"]["must"]
    # pii_status='scrubbed' is always one of the filters.
    assert any("term" in f and f["term"].get("pii_status") == "scrubbed"
               for f in body["query"]["bool"]["filter"])


def test_aws_adapter_filter_pushdown():
    from aprntc.memory.aws_opensearch_store import AwsOpenSearchMemoryStore
    fake = _FakeOpenSearchClient()
    s = AwsOpenSearchMemoryStore(host="x:0", index="lessons",
                                  embedder=HashEmbedder(), client=fake)
    s.upsert_lessons([
        _les("low quality", "topic", reward=0.2),
        _les("high quality", "topic", reward=0.9),
    ])
    hits = s.retrieve(query="topic", k=10, min_reward=0.5)
    assert all(h.lesson.reward >= 0.5 for h in hits)
