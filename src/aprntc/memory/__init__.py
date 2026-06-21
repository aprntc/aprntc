"""Experience Memory — curated, generation-versioned lessons (ADR 0003).

`MemoryStore` is the interface; multiple backends implement it. The default is
the dependency-light :class:`LocalMemoryStore` (SQLite + cosine; works offline).
Cloud / scale-out options: :class:`VikingDBMemoryStore` (BytePlus),
:class:`PineconeMemoryStore`, :class:`AwsOpenSearchMemoryStore`, and optionally
:class:`ChromaMemoryStore` for a richer local choice with ONNX embeddings.

Pick one via :func:`make_memory_store` (URL-based) or instantiate directly.
Lessons are upserted with scalar fields (generation, reward, lesson_type,
pii_status) and retrieved via filtered search + MMR diversification, then
injected as a few sharp exemplars under a token budget.
"""

from aprntc.memory.base import Lesson, LessonType, MemoryStore, RetrievedLesson
from aprntc.memory.embedder import (
    Embedder,
    HashEmbedder,
    ModelArkEmbedder,
    OpenAIEmbedder,
    default_embedder,
)
from aprntc.memory.factory import make_memory_store
from aprntc.memory.local_store import LocalMemoryStore
from aprntc.memory.mmr import cosine, mmr_select
from aprntc.memory.vikingdb import VikingDBError, VikingDBMemoryStore

__all__ = [
    # Core
    "Lesson",
    "LessonType",
    "MemoryStore",
    "RetrievedLesson",
    # Embedders
    "Embedder",
    "HashEmbedder",
    "ModelArkEmbedder",
    "OpenAIEmbedder",
    "default_embedder",
    # Backends
    "LocalMemoryStore",
    "VikingDBError",
    "VikingDBMemoryStore",
    # Factory + helpers
    "make_memory_store",
    "cosine",
    "mmr_select",
]
