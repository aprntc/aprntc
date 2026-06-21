"""SemanticKnowledgeBase tests — offline via injected transport.

We test the REAL adapter's request construction + response handling (mirrors the
test_memory.py pattern); the live VikingDB is exercised by the indexer/eval scripts.
"""

import json

import pytest

from aprntc.config import VikingDBConfig
from aprntc.demos.byteplus.kb import Chunk
from aprntc.demos.byteplus.kb_semantic import (
    SemanticKBError,
    SemanticKnowledgeBase,
)


def _cfg() -> VikingDBConfig:
    return VikingDBConfig(ak="AK", sk="SK", region="ap-southeast-1",
                          data_host="data.example.com", control_host="control.example.com")


class RecordingTransport:
    """Captures the last signed request and returns a scripted (status, body)."""

    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body if body is not None else {"result": {"data": []}}
        self.calls = []

    def __call__(self, url, headers, body):
        self.calls.append({"url": url, "headers": headers,
                           "body": json.loads(body.decode("utf-8"))})
        return self.status, self.body


def _chunk(cid: str, doc: str, section: str, text: str) -> Chunk:
    return Chunk(chunk_id=cid, doc=doc, section=section, text=text, _tokens=set())


def test_create_collection_uses_control_action_and_is_signed():
    """Control-plane V2 routing: signed POST to control host with Action= query."""
    t = RecordingTransport(body={"Result": {"ResourceId": "rid"}})
    kb = SemanticKnowledgeBase(_cfg(), transport=t)
    kb.create_collection()
    call = t.calls[0]
    assert "control.example.com" in call["url"]
    assert "Action=CreateVikingdbCollection" in call["url"]
    assert "Version=" in call["url"]
    assert call["headers"]["Authorization"].startswith("HMAC-SHA256 ")
    # Server-side vectorize: `text` is the TEXT vector field (no raw embedding field)
    fields = call["body"]["Fields"]
    text_field = next(f for f in fields if f["FieldName"] == "text")
    assert text_field["FieldType"] == "text"
    assert not any(f["FieldName"] == "embedding" for f in fields)
    # chunk_id is the primary key (re-upserts UPDATE, never duplicate)
    pk = next(f for f in fields if f.get("IsPrimaryKey"))
    assert pk["FieldName"] == "chunk_id"


def test_create_index_uses_hnsw_cosine_and_scalar_indexes():
    """Index is hnsw + cosine; doc/section are scalar-indexed so we can filter on them."""
    t = RecordingTransport(body={"Result": {"Message": "success"}})
    kb = SemanticKnowledgeBase(_cfg(), transport=t)
    kb.create_index()
    body = t.calls[0]["body"]
    assert body["VectorIndex"]["IndexType"] == "hnsw"
    assert body["VectorIndex"]["Distance"] == "cosine"
    assert "doc" in body["ScalarIndex"] and "section" in body["ScalarIndex"]


def test_upsert_sends_one_row_per_request_with_text_for_server_vectorize():
    """Server-side vectorize caps at 1 row/request; we send TEXT in `text`, no client vector."""
    t = RecordingTransport(body={"result": {}})
    kb = SemanticKnowledgeBase(_cfg(), transport=t)
    written = kb.upsert_chunks([
        _chunk("c1", "ModelArk", "Deepreasoning", "Set thinking type to enabled."),
        _chunk("c2", "VikingDB", "Create Index", "Distance metrics: ip, L2, cosine."),
    ])
    assert written == 2
    assert len(t.calls) == 2  # one HTTP call per chunk (server-side embed cap)
    # First request: collection name + a single-row data array, no raw vector
    body = t.calls[0]["body"]
    assert body["collection_name"] == "ankur_aprntc_byteplus_kb_collection"
    row = body["data"][0]
    assert row["chunk_id"] == "c1"
    assert row["text"] == "Set thinking type to enabled."  # vector field as TEXT
    assert "embedding" not in row and "vector" not in row


def test_upsert_empty_is_noop():
    t = RecordingTransport()
    kb = SemanticKnowledgeBase(_cfg(), transport=t)
    assert kb.upsert_chunks([]) == 0
    assert t.calls == []


def test_search_uses_multimodal_endpoint_with_text_query():
    """search() hits /data/search/multi_modal with the text query (server embeds it)."""
    body = {"data": [
        {"fields": {"chunk_id": "c1", "text": "thinking enabled",
                    "doc": "Deepreasoning", "section": "Usage", "chunk_ord": 3}, "score": 0.91},
        {"fields": {"chunk_id": "c2", "text": "max_completion_tokens",
                    "doc": "Chat API", "section": "Tokens", "chunk_ord": 7}, "score": 0.74},
    ]}
    t = RecordingTransport(body=body)
    kb = SemanticKnowledgeBase(_cfg(), transport=t)
    hits = kb.search("how do i enable deep reasoning?", k=2)
    # Calls the data plane (signed POST to data host), multimodal path, text query
    call = t.calls[0]
    assert "data.example.com" in call["url"]
    assert "/api/vikingdb/data/search/multi_modal" in call["url"]
    assert call["body"]["text"] == "how do i enable deep reasoning?"
    assert call["body"]["limit"] == 2
    # Returns Chunk objects, in order (top-k by score)
    assert [c.chunk_id for c in hits] == ["c1", "c2"]
    assert hits[0].doc == "Deepreasoning" and hits[0].section == "Usage"
    # And the chunks are wire-compatible with the keyword KB's Chunk class
    assert hits[0].cite() == "Deepreasoning › Usage"


def test_search_with_scores_returns_score_per_hit():
    body = {"data": [
        {"fields": {"chunk_id": "c1", "text": "t1", "doc": "D1", "section": "S1"}, "score": 0.85},
    ]}
    t = RecordingTransport(body=body)
    kb = SemanticKnowledgeBase(_cfg(), transport=t)
    hits = kb.search_with_scores("q", k=4)
    assert hits[0].score == pytest.approx(0.85)
    assert hits[0].chunk.chunk_id == "c1"


def test_search_tolerant_to_envelope_shapes():
    """VikingDB sometimes wraps results in {Result:{Data:[...]}}; we handle either."""
    body = {"Result": {"Data": [
        {"fields": {"chunk_id": "c1", "text": "t", "doc": "D", "section": "S"}, "score": 0.5},
    ]}}
    t = RecordingTransport(body=body)
    kb = SemanticKnowledgeBase(_cfg(), transport=t)
    assert len(kb.search("q", k=4)) == 1


def test_search_skips_rows_with_empty_text():
    """A row with no text isn't useful for grounding; we drop it silently."""
    body = {"data": [{"fields": {"chunk_id": "c1", "text": ""}, "score": 0.0}]}
    t = RecordingTransport(body=body)
    kb = SemanticKnowledgeBase(_cfg(), transport=t)
    assert kb.search("q", k=4) == []


def test_non_200_status_raises_semantickberror_with_context():
    """A signed but rejected call (e.g. collection doesn't exist) must surface clearly."""
    t = RecordingTransport(status=400, body={"Code": 100008, "Message": "no such collection"})
    kb = SemanticKnowledgeBase(_cfg(), transport=t)
    with pytest.raises(SemanticKBError, match="VikingDB HTTP 400"):
        kb.search("q", k=4)
