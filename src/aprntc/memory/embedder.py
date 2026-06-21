"""Embedder protocol — turns text into a fixed-dim vector.

Cloud backends (Pinecone, AWS) and the LocalMemoryStore all need to embed
``situation`` text into vectors. The choice of embedder is independent of the
choice of store, so we keep it behind a small Protocol with multiple
implementations:

* :class:`HashEmbedder` — deterministic, zero-dep, low-quality. Used for tests
  and as the default when no other embedder is wired (good enough for the
  pipeline to work end-to-end out of the box; not good enough for production
  retrieval quality).
* :class:`ModelArkEmbedder` — calls BytePlus ModelArk's embedding endpoint. The
  default upgrade path when ``ARK_API_KEY`` is set (the user already has these
  keys for the rest of the project).
* :class:`OpenAIEmbedder` — calls any OpenAI-compatible /v1/embeddings endpoint.

The store stays embedder-agnostic; just pass one in at construction time.

(BytePlus VikingDB uses server-side embedding and bypasses this layer entirely.)
"""

from __future__ import annotations

import hashlib
import math
import os
from typing import Any, Callable, Protocol, Sequence


class Embedder(Protocol):
    """Anything that turns texts into fixed-dim vectors. Stateless / batch-safe."""

    @property
    def dim(self) -> int:
        """Fixed dimensionality this embedder produces."""
        ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per input text. Same length as the input."""
        ...

    def embed_one(self, text: str) -> list[float]:
        """Convenience wrapper around :meth:`embed`."""
        ...


class HashEmbedder:
    """Deterministic hash-based embedder — works offline, no model file needed.

    Uses SHA-256 over character n-grams scattered into a fixed-dim sparse vector,
    L2-normalized. The semantic quality is LOW (no real concept of similarity beyond
    string overlap), but it's:
      * deterministic — same input always produces the same vector,
      * fast — pure stdlib + a tiny bit of math,
      * dependency-free — no torch, no sentence-transformers, no API key.

    Good enough for the pipeline to function out of the box; **upgrade to
    :class:`ModelArkEmbedder` or :class:`OpenAIEmbedder` in production**.
    """

    def __init__(self, dim: int = 256, ngram: int = 3) -> None:
        if dim < 16 or dim > 4096:
            raise ValueError("HashEmbedder dim should be in [16, 4096]")
        self._dim = dim
        self._ngram = max(2, ngram)

    @property
    def dim(self) -> int:
        return self._dim

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(t or "") for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        # Lowercase + collapse whitespace; iterate character n-grams.
        norm = " ".join(text.lower().split())
        vec = [0.0] * self._dim
        if not norm:
            return vec
        n = max(self._ngram, 1)
        for i in range(max(1, len(norm) - n + 1)):
            gram = norm[i : i + n]
            h = hashlib.sha256(gram.encode("utf-8")).digest()
            # Two distinct hash positions per gram for some spread.
            for off in (0, 4):
                idx = int.from_bytes(h[off : off + 4], "big") % self._dim
                sign = 1.0 if (h[off + 8 if off + 8 < len(h) else 0] & 1) == 0 else -1.0
                vec[idx] += sign
        # L2 normalize (so cosine == dot).
        norm_l2 = math.sqrt(sum(v * v for v in vec))
        if norm_l2 == 0.0:
            return vec
        return [v / norm_l2 for v in vec]


class ModelArkEmbedder:
    """Calls the BytePlus ModelArk embedding endpoint.

    Requires ``ARK_API_KEY``, ``ARK_BASE_URL`` (defaults to the SE-Asia endpoint),
    and a model id (defaults to ``doubao-embedding`` which produces 2048-dim
    vectors per BytePlus docs). Uses the existing OpenAI-compatible interface.
    """

    DEFAULT_MODEL = "doubao-embedding"
    DEFAULT_DIM = 2048

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        dim: int | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("ARK_API_KEY") or ""
        self._base_url = base_url or os.environ.get(
            "ARK_BASE_URL", "https://ark.ap-southeast.bytepluses.com/api/v3",
        )
        self._model = model or os.environ.get("APRNTC_EMBED_MODEL", self.DEFAULT_MODEL)
        self._dim = int(dim or os.environ.get("APRNTC_EMBED_DIM", self.DEFAULT_DIM))
        if not self._api_key:
            raise RuntimeError(
                "ModelArkEmbedder needs ARK_API_KEY (set in .env, or pass api_key=…)."
            )

    @property
    def dim(self) -> int:
        return self._dim

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        try:
            from openai import OpenAI
        except ModuleNotFoundError as e:  # pragma: no cover - guidance path
            raise ModuleNotFoundError(
                "ModelArkEmbedder needs the openai SDK — install: pip install 'aprntc[proxy]' "
                "(or pip install openai)."
            ) from e
        client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        resp = client.embeddings.create(model=self._model, input=list(texts))
        return [list(d.embedding) for d in resp.data]


class OpenAIEmbedder:
    """Calls a generic OpenAI-compatible ``/v1/embeddings`` endpoint.

    Use when the user wants to back the embedder with OpenAI's own embeddings
    (e.g. ``text-embedding-3-small``) instead of ModelArk's. Same shape as
    :class:`ModelArkEmbedder` — only the default base_url + model differ.
    """

    DEFAULT_MODEL = "text-embedding-3-small"
    DEFAULT_DIM = 1536

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        model: str | None = None,
        dim: int | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY") or ""
        self._base_url = base_url
        self._model = model or self.DEFAULT_MODEL
        self._dim = int(dim or self.DEFAULT_DIM)
        if not self._api_key:
            raise RuntimeError("OpenAIEmbedder needs OPENAI_API_KEY.")

    @property
    def dim(self) -> int:
        return self._dim

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        try:
            from openai import OpenAI
        except ModuleNotFoundError as e:  # pragma: no cover - guidance path
            raise ModuleNotFoundError("OpenAIEmbedder needs the openai SDK.") from e
        client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        resp = client.embeddings.create(model=self._model, input=list(texts))
        return [list(d.embedding) for d in resp.data]


def default_embedder() -> Embedder:
    """Pick a sensible embedder based on the environment.

    Tries in order:
      1. ModelArk if BOTH ``ARK_API_KEY`` and ``APRNTC_EMBED_MODEL`` are set
         (the model id varies per ModelArk account — we don't guess a default
         to avoid sending real API calls that may fail).
      2. OpenAI if ``OPENAI_API_KEY`` is set (uses ``text-embedding-3-small``).
      3. :class:`HashEmbedder` (deterministic, offline, low semantic quality).

    The hash fallback always works — the system is functional out of the box.
    For better retrieval quality, point ``APRNTC_EMBED_MODEL`` at a ModelArk
    embedding endpoint you have access to, or set ``OPENAI_API_KEY``.
    """
    if os.environ.get("ARK_API_KEY") and os.environ.get("APRNTC_EMBED_MODEL"):
        try:
            return ModelArkEmbedder()
        except Exception:
            pass
    if os.environ.get("OPENAI_API_KEY"):
        try:
            return OpenAIEmbedder()
        except Exception:
            pass
    return HashEmbedder()
