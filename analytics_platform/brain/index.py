"""Hybrid retrieval index for the Company Brain.

Two recall legs over the same SQLite database that holds `knowledge_nodes`:

* lexical — FTS5 + bm25(), which carries internal vocabulary (metric names,
  product codes, acronyms) that dense vectors handle poorly;
* dense — normalised embeddings in a BLOB column, brute-forced with numpy,
  which carries paraphrase ("drop off" ~ "abandonment").

Both are always restricted by `tenant_id` in SQL. Isolation is a property of the
source of truth, never of an index's metadata filter.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple

import numpy as np

from ..database import Store
from ..domain import now_iso
from .embedding import Embedder, NullEmbedder
from .text import to_fts_query

logger = logging.getLogger(__name__)

# SQLite's default limit is 999 host parameters per statement.
_MAX_SQL_PARAMS = 900


class BrainIndex:
    def __init__(self, store: Store, embedder: Optional[Embedder] = None):
        self.store = store
        self.embedder = embedder or NullEmbedder("no embedder supplied")

    @property
    def embedding_available(self) -> bool:
        return bool(getattr(self.embedder, "available", False))

    # -- write ---------------------------------------------------------------
    def upsert(self, node_id: str, tenant_id: str, title: str, summary: str) -> None:
        """Index one node into both legs. Replaces any previous entry."""
        self._upsert_lexical(node_id, tenant_id, title, summary)
        self._upsert_vector(node_id, tenant_id, title, summary)

    def _upsert_lexical(self, node_id: str, tenant_id: str, title: str,
                        summary: str) -> None:
        try:
            self.store.execute_many([
                ("DELETE FROM knowledge_fts WHERE node_id = ?", (node_id,)),
                ("INSERT INTO knowledge_fts (node_id, tenant_id, title, summary) "
                 "VALUES (?,?,?,?)", (node_id, tenant_id, title or "", summary or "")),
            ])
        except Exception as exc:  # noqa: BLE001 - indexing must not fail a write
            logger.warning("lexical index upsert failed for %s: %s", node_id, exc,
                           exc_info=True)

    def _upsert_vector(self, node_id: str, tenant_id: str, title: str,
                       summary: str) -> None:
        if not self.embedding_available:
            return
        # Only prose is embedded. SQL is structure, not language: embedding it
        # dilutes the vector and cannot answer table-level questions anyway.
        text = f"{title}\n{summary}".strip()
        if not text:
            return
        vecs = self.embedder.encode_documents([text])
        if vecs is None:
            return
        vec = np.asarray(vecs[0], dtype=np.float32)
        try:
            self.store.execute(
                "INSERT INTO knowledge_vectors (node_id, tenant_id, model, dim, vector, updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET "
                "tenant_id=excluded.tenant_id, model=excluded.model, dim=excluded.dim, "
                "vector=excluded.vector, updated_at=excluded.updated_at",
                (node_id, tenant_id, self.embedder.model_name, int(vec.shape[0]),
                 vec.tobytes(), now_iso()))
        except Exception as exc:  # noqa: BLE001
            logger.warning("vector index upsert failed for %s: %s", node_id, exc,
                           exc_info=True)

    def delete(self, node_id: str) -> None:
        try:
            self.store.execute_many([
                ("DELETE FROM knowledge_fts WHERE node_id = ?", (node_id,)),
                ("DELETE FROM knowledge_vectors WHERE node_id = ?", (node_id,)),
            ])
        except Exception as exc:  # noqa: BLE001
            logger.warning("index delete failed for %s: %s", node_id, exc, exc_info=True)

    # -- read ----------------------------------------------------------------
    def lexical_search(self, query: str, tenant_id: str,
                       candidate_ids: Optional[Sequence[str]] = None,
                       limit: int = 40) -> List[str]:
        """Node ids ranked by bm25, best first. [] when nothing is searchable."""
        if candidate_ids is not None and len(candidate_ids) == 0:
            return []
        match = to_fts_query(query)
        if not match:
            return []

        sql = ("SELECT node_id, bm25(knowledge_fts) AS score FROM knowledge_fts "
               "WHERE knowledge_fts MATCH ? AND tenant_id = ?")
        params: List[object] = [match, tenant_id]
        restrict = self._restrict_clause(candidate_ids, params)
        sql += restrict
        # bm25() is more negative for better matches, so ascending is best-first.
        sql += " ORDER BY score ASC LIMIT ?"
        params.append(limit)

        try:
            rows = self.store.query_all(sql, tuple(params))
        except Exception as exc:  # noqa: BLE001
            logger.warning("lexical search failed for tenant %s: %s", tenant_id, exc,
                           exc_info=True)
            return []
        return [r["node_id"] for r in rows]

    @staticmethod
    def _restrict_clause(candidate_ids: Optional[Sequence[str]],
                         params: List[object]) -> str:
        """AND node_id IN (...) when a candidate set was supplied and fits."""
        if not candidate_ids:
            return ""
        ids = list(candidate_ids)
        if len(ids) > _MAX_SQL_PARAMS:
            # Too many to bind; the caller's set is broad enough that filtering
            # afterwards is equivalent and cheaper than chunking.
            return ""
        params.extend(ids)
        return f" AND node_id IN ({','.join('?' for _ in ids)})"
