"""Embedding providers for the Brain's dense recall leg.

Two rules shape this module:

* The model is configuration, never a literal — every tenant deployment can pick
  its own, and the default is the small BGE variant because a curated Brain is
  thousands of nodes, not millions.
* Unavailability is an explicit, logged state. Retrieval must be able to ask
  `embedder.available` and fall back to lexical-only *knowing* that it did, rather
  than silently returning nothing.

All vectors are L2-normalised float32, so cosine similarity is a dot product.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Protocol, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    available: bool
    model_name: str
    dim: int

    def encode_documents(self, texts: List[str]) -> Optional[np.ndarray]: ...
    def encode_query(self, text: str) -> Optional[np.ndarray]: ...


class NullEmbedder:
    """Stands in when embeddings are off or the model will not load."""

    available = False
    dim = 0

    def __init__(self, reason: str):
        self.reason = reason
        self.model_name = ""

    def encode_documents(self, texts: List[str]) -> Optional[np.ndarray]:
        return None

    def encode_query(self, text: str) -> Optional[np.ndarray]:
        return None


class SentenceTransformerEmbedder:
    """sentence-transformers backend. Never raises on load — sets `available`."""

    def __init__(self, model_name: str, query_prefix: str = ""):
        self.model_name = model_name
        self.query_prefix = query_prefix
        self.available = False
        self.dim = 0
        self._model = None
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(model_name)
            self.dim = int(self._model.get_sentence_embedding_dimension())
            self.available = True
        except Exception as exc:  # noqa: BLE001 - load failure must not crash startup
            logger.warning(
                "embedding model %r unavailable, Brain retrieval will be "
                "lexical-only: %s", model_name, exc)

    def _encode(self, texts: List[str]) -> Optional[np.ndarray]:
        if not self.available:
            return None
        try:
            vecs = self._model.encode(texts, normalize_embeddings=True,
                                      convert_to_numpy=True,
                                      show_progress_bar=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("embedding failed for %d text(s): %s", len(texts), exc)
            return None
        return np.asarray(vecs, dtype=np.float32)

    def encode_documents(self, texts: List[str]) -> Optional[np.ndarray]:
        if not texts:
            return None
        return self._encode(list(texts))

    def encode_query(self, text: str) -> Optional[np.ndarray]:
        vecs = self._encode([f"{self.query_prefix}{text}"])
        return None if vecs is None else vecs[0]


_CACHE: Dict[Tuple[str, bool, str], Embedder] = {}


def get_embedder(settings) -> Embedder:
    """Cached embedder for these settings. Loading a model is seconds, not ms."""
    key = (settings.embedding_model, settings.embedding_enabled,
           settings.embedding_query_prefix)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    if not settings.embedding_enabled:
        logger.info("embeddings disabled by config; Brain retrieval is lexical-only")
        emb: Embedder = NullEmbedder("disabled by config")
    else:
        candidate = SentenceTransformerEmbedder(
            settings.embedding_model, settings.embedding_query_prefix)
        emb = candidate if candidate.available else NullEmbedder(
            f"model {settings.embedding_model!r} failed to load")

    _CACHE[key] = emb
    return emb


def reset_embedder_cache() -> None:
    """Test seam — drops cached models so settings changes take effect."""
    _CACHE.clear()
