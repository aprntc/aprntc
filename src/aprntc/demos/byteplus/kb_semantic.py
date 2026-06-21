"""Semantic retriever for the BytePlus KB — VikingDB server-side-vectorize backed.

A drop-in alternative to the keyword :class:`~aprntc.demos.byteplus.kb.KnowledgeBase`:
same ``search(query, k) -> list[Chunk]`` interface, but retrieval uses dense
embeddings instead of TF-IDF. The collection is set up with **server-side
vectorize** (skylark embeddings), so callers send text in, never raw vectors.

Why this exists: the keyword KB tops out around 1040 chunks — it wins on product-
name queries (e.g. "Seedance" → SeedanceCreateAPI) but loses on paraphrased
questions ("deep reasoning" vs "thinking parameter"). The semantic retriever
trades nothing on the former (titles still embed strongly) and pulls ahead on
the latter. See ``scripts/eval_byteplus_retrieval.py`` for the A/B against GOLD.

Wire-compatible with the keyword KB: the agent code that calls ``kb.search(...)``
needs zero changes to swap.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from aprntc.byteplus.signing import Credentials, sign
from aprntc.config import VikingDBConfig
from aprntc.demos.byteplus.kb import Chunk

Transport = Callable[[str, dict[str, str], bytes], tuple[int, dict[str, Any]]]


CONTROL_VERSION = "2020-12-25"


class SemanticKBError(RuntimeError):
    """VikingDB returned a non-200 (signed, but the server rejected the call)."""


def _httpx_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
    """Default transport — requires httpx (the standard memory adapter pulls it in)."""
    try:
        import httpx
    except ModuleNotFoundError as e:  # pragma: no cover - degradation path
        raise ModuleNotFoundError(
            "semantic KB needs httpx — install the [byteplus] extra: pip install 'aprntc[byteplus]'"
        ) from e
    r = httpx.post(url, content=body, headers=headers, timeout=30.0)
    try:
        return r.status_code, r.json()
    except json.JSONDecodeError:
        return r.status_code, {"raw": r.text[:300]}


@dataclass
class SemanticHit:
    chunk: Chunk
    score: float


class SemanticKnowledgeBase:
    """VikingDB-backed semantic retriever for BytePlus KB chunks.

    Schema (server-side vectorize):
      - chunk_id    (string, primary key)        — stable per chunk
      - text        (text)                        — the field embedded server-side
      - doc         (string)                      — source doc / product area
      - section     (string)                      — heading (for citation)
      - chunk_ord   (int64)                       — within-doc order (for filtering)
    """

    def __init__(
        self,
        config: VikingDBConfig,
        *,
        collection: str = "ankur_aprntc_byteplus_kb_collection",
        index: str = "ankur_aprntc_byteplus_kb_index",
        transport: Transport | None = None,
    ) -> None:
        config.validate()
        self._cfg = config
        self._collection = collection
        self._index = index
        self._transport = transport or _httpx_transport
        self._creds = Credentials(ak=config.ak, sk=config.sk,
                                  service=config.service, region=config.region)

    @property
    def collection(self) -> str:
        return self._collection

    # -- low-level signed calls -----------------------------------------

    def _call_data(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        signed = sign(method="POST", host=self._cfg.data_host, path=path,
                      creds=self._creds, body=raw,
                      headers={"Content-Type": "application/json"})
        return self._invoke(signed.url, signed.headers, raw)

    def _call_control(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        query = {"Action": action, "Version": CONTROL_VERSION}
        signed = sign(method="POST", host=self._cfg.control_host, path="/",
                      creds=self._creds, body=raw, query=query,
                      headers={"Content-Type": "application/json"})
        return self._invoke(signed.url, signed.headers, raw)

    def _invoke(self, url: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
        status, data = self._transport(url, headers, body)
        if status != 200:
            raise SemanticKBError(f"VikingDB HTTP {status}: {json.dumps(data)[:300]}")
        return data

    # -- control plane: one-time setup ----------------------------------

    def create_collection(self) -> dict[str, Any]:
        body = {
            "CollectionName": self._collection,
            "Description": "aprntc BytePlus KB chunks (semantic retrieval)",
            # Server-side vectorize: `text` is the field that gets embedded.
            "Fields": [
                {"FieldName": "chunk_id", "FieldType": "string", "IsPrimaryKey": True},
                {"FieldName": "text", "FieldType": "text"},
                {"FieldName": "doc", "FieldType": "string", "DefaultValue": "unknown"},
                {"FieldName": "section", "FieldType": "string", "DefaultValue": "Overview"},
                {"FieldName": "chunk_ord", "FieldType": "int64", "DefaultValue": 0},
            ],
        }
        return self._call_control("CreateVikingdbCollection", body)

    def create_index(self) -> dict[str, Any]:
        body = {
            "CollectionName": self._collection,
            "IndexName": self._index,
            "VectorIndex": {"IndexType": "hnsw", "Distance": "cosine", "Quant": "float"},
            # Scalar fields to filter on later (e.g. restrict to a single doc).
            "ScalarIndex": ["doc", "section"],
        }
        return self._call_control("CreateVikingdbIndex", body)

    # -- data plane: write ----------------------------------------------

    def upsert_chunks(self, chunks: Iterable[Chunk]) -> int:
        """Upsert chunks into VikingDB; server embeds the ``text`` field.

        Server-side vectorize caps each request at 1 row, so we send one at a
        time. Idempotent — re-running over the same chunk_ids updates in place.
        """
        chunks_list = list(chunks)
        if not chunks_list:
            return 0
        written = 0
        for i, c in enumerate(chunks_list):
            row = {
                "chunk_id": c.chunk_id,
                "text": c.text,                # vector (text) field — embedded server-side
                "doc": c.doc,
                "section": c.section,
                "chunk_ord": i,
            }
            self._call_data(
                "/api/vikingdb/data/upsert",
                {"collection_name": self._collection, "data": [row]},
            )
            written += 1
        return written

    # -- data plane: read -----------------------------------------------

    def search(self, query: str, *, k: int = 4) -> list[Chunk]:
        """Return top-k chunks by semantic similarity. Mirrors KnowledgeBase.search."""
        body = {
            "collection_name": self._collection,
            "index_name": self._index,
            "text": query,
            "need_instruction": False,
            "limit": k,
            "output_fields": ["chunk_id", "text", "doc", "section", "chunk_ord"],
        }
        data = self._call_data("/api/vikingdb/data/search/multi_modal", body)
        items = _extract_items(data)
        out: list[Chunk] = []
        for it in items:
            fields = it.get("fields", it)
            chunk = _chunk_from_fields(fields)
            if chunk is not None:
                out.append(chunk)
        return out[:k]

    def search_with_scores(self, query: str, *, k: int = 4) -> list[SemanticHit]:
        """Same as ``search`` but returns the similarity score alongside each chunk."""
        body = {
            "collection_name": self._collection,
            "index_name": self._index,
            "text": query,
            "need_instruction": False,
            "limit": k,
            "output_fields": ["chunk_id", "text", "doc", "section", "chunk_ord"],
        }
        data = self._call_data("/api/vikingdb/data/search/multi_modal", body)
        items = _extract_items(data)
        out: list[SemanticHit] = []
        for it in items:
            fields = it.get("fields", it)
            chunk = _chunk_from_fields(fields)
            if chunk is None:
                continue
            out.append(SemanticHit(chunk=chunk, score=float(it.get("score", 0.0))))
        return out[:k]


def _chunk_from_fields(fields: dict[str, Any]) -> Chunk | None:
    """Reconstruct a Chunk from a VikingDB row (no _tokens — search doesn't need them)."""
    if not fields:
        return None
    text = fields.get("text") or ""
    if not text:
        return None
    return Chunk(
        chunk_id=str(fields.get("chunk_id") or "unknown"),
        doc=str(fields.get("doc") or "unknown"),
        section=str(fields.get("section") or "Overview"),
        text=text,
        _tokens=set(),  # not needed for retrieved chunks; the agent only reads .text / .cite()
    )


def _extract_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the result list out of VikingDB's response envelope (tolerant of shape)."""
    if "data" in data and isinstance(data["data"], list):
        return data["data"]
    result = data.get("result") or data.get("Result") or {}
    if isinstance(result, dict):
        for key in ("data", "items", "Data"):
            if isinstance(result.get(key), list):
                return result[key]
    return []
