"""URL-based factory for selecting a MemoryStore backend.

Selection order:
  * ``local://path/to/file.db``  → :class:`LocalMemoryStore`   (SQLite + cosine; default)
  * ``chroma://path/to/chromadir`` → :class:`ChromaMemoryStore` (needs ``[chroma]``)
  * ``byteplus://…``             → :class:`VikingDBMemoryStore` (needs ``[byteplus]``)
  * ``pinecone://index-name``    → :class:`PineconeMemoryStore` (needs ``[pinecone]``)
  * ``aws://host[:port]/index``  → :class:`AwsOpenSearchMemoryStore` (needs ``[aws]``)
  * Plain path (no scheme)       → treated as ``local://<that path>``

This lets the deployer pick a vector DB without touching code — just set
``APRNTC_VECTOR_DB_URL``. The default (when unset) is ``local:///./aprntc_memory.db``.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from aprntc.memory.base import MemoryStore
from aprntc.memory.embedder import Embedder


def make_memory_store(
    url: str | None = None,
    *,
    embedder: Embedder | None = None,
) -> MemoryStore | None:
    """Open a MemoryStore appropriate for ``url``.

    Returns ``None`` when the chosen backend isn't configured (e.g. byteplus://
    selected but VIKINGDB_AK is empty) — the dashboard then degrades to "memory
    not connected" gracefully. Raises only on hard mis-configuration.
    """
    url = (url or os.environ.get("APRNTC_VECTOR_DB_URL") or "local:///./aprntc_memory.db").strip()
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    # No scheme → treat as a local path.
    if not scheme or scheme == "file":
        from aprntc.memory.local_store import LocalMemoryStore
        return LocalMemoryStore(url, embedder=embedder)

    if scheme in ("local", "sqlite"):
        from aprntc.memory.local_store import LocalMemoryStore
        # url like local:///./path.db → parsed.path = "/./path.db"
        path = parsed.path or parsed.netloc or "aprntc_memory.db"
        if path.startswith("/./"):
            path = path[3:]
        elif path.startswith("/"):
            path = path[1:] or "aprntc_memory.db"
        return LocalMemoryStore(path, embedder=embedder)

    if scheme == "chroma":
        try:
            from aprntc.memory.chroma_store import ChromaMemoryStore
        except ModuleNotFoundError as e:
            raise ModuleNotFoundError(
                "chroma:// requires the [chroma] extra — `pip install 'aprntc[chroma]'`"
            ) from e
        path = parsed.path or parsed.netloc or "./aprntc_chroma"
        if path.startswith("/"):
            path = path.lstrip("/") or "./aprntc_chroma"
        return ChromaMemoryStore(path, embedder=embedder)

    if scheme == "byteplus":
        # Existing VikingDB adapter — config comes from env (VIKINGDB_AK/SK + hosts).
        return _byteplus_store(url)

    if scheme == "pinecone":
        from aprntc.memory.pinecone_store import PineconeMemoryStore
        # url like pinecone://my-index-name → parsed.netloc = "my-index-name"
        index_name = parsed.netloc or parsed.path.lstrip("/") or "aprntc-lessons"
        return PineconeMemoryStore(index_name=index_name, embedder=embedder)

    if scheme in ("aws", "opensearch"):
        from aprntc.memory.aws_opensearch_store import AwsOpenSearchMemoryStore
        # url like aws://host:9200/index — host from netloc, index from path
        host = parsed.netloc or "localhost:9200"
        index = parsed.path.lstrip("/") or "aprntc-lessons"
        return AwsOpenSearchMemoryStore(host=host, index=index, embedder=embedder)

    raise ValueError(f"Unknown vector-DB scheme: {scheme!r}. "
                     f"Expected one of: local, sqlite, chroma, byteplus, pinecone, aws.")


def _byteplus_store(url: str) -> MemoryStore | None:
    """Build the existing VikingDBMemoryStore from env (the original default)."""
    try:
        from aprntc.config import Settings
        from aprntc.memory.vikingdb import VikingDBMemoryStore
    except ImportError:
        return None
    try:
        settings = Settings.from_env(dotenv=".env")
        settings.vikingdb.validate()
    except Exception:
        return None
    parsed = urlparse(url)
    # byteplus://<collection>[/<index>] — allow overrides via URL; else defaults.
    collection = (parsed.netloc or "ankur_aprntc_collection").strip("/")
    index_part = (parsed.path or "").strip("/")
    index = index_part or "ankur_aprntc_index"
    return VikingDBMemoryStore(
        settings.vikingdb,
        collection=collection,
        index=index,
        dim=2048,
    )
