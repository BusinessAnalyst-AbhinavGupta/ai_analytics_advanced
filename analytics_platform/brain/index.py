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
from typing import Dict, List, Optional, Sequence, Tuple

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
        """Node ids ranked by bm25, best first. [] when nothing is searchable.

        `ORDER BY ... LIMIT` runs inside SQL, before any Python code sees a row —
        unlike the vector leg (Task 5), which loads every row and can safely
        re-filter in Python afterwards. That means a candidate set larger than
        SQLite's ~900-parameter limit cannot simply drop the restriction: doing so
        would rank the whole tenant and return the global top-`limit`, which may
        share nothing with the candidate set the caller actually asked about. So
        this leg chunks instead — one MATCH query per <=900-id slice, merging by
        each node's best (most negative) bm25 score across chunks, then re-sorting
        and truncating once at the end.
        """
        if candidate_ids is not None and len(candidate_ids) == 0:
            return []
        match = to_fts_query(query)
        if not match:
            return []

        chunks: List[Optional[Sequence[str]]]
        if candidate_ids is None:
            chunks = [None]
        else:
            ids = list(candidate_ids)
            chunks = [ids[i:i + _MAX_SQL_PARAMS] for i in range(0, len(ids), _MAX_SQL_PARAMS)]

        best: Dict[str, float] = {}
        for chunk in chunks:
            for node_id, score in self._lexical_search_chunk(match, tenant_id, chunk, limit):
                if node_id not in best or score < best[node_id]:
                    best[node_id] = score

        # bm25() is more negative for better matches, so ascending is best-first.
        ordered = sorted(best, key=lambda n: best[n])
        return ordered[:limit]

    def _lexical_search_chunk(self, match: str, tenant_id: str,
                              candidate_ids: Optional[Sequence[str]],
                              limit: int) -> List[Tuple[str, float]]:
        """One MATCH query, restricted to at most _MAX_SQL_PARAMS candidate ids."""
        sql = ("SELECT node_id, bm25(knowledge_fts) AS score FROM knowledge_fts "
               "WHERE knowledge_fts MATCH ? AND tenant_id = ?")
        params: List[object] = [match, tenant_id]
        if candidate_ids is not None:
            sql += f" AND node_id IN ({','.join('?' for _ in candidate_ids)})"
            params.extend(candidate_ids)
        sql += " ORDER BY score ASC LIMIT ?"
        params.append(limit)

        try:
            rows = self.store.query_all(sql, tuple(params))
        except Exception as exc:  # noqa: BLE001
            logger.warning("lexical search failed for tenant %s: %s", tenant_id, exc,
                           exc_info=True)
            return []
        return [(r["node_id"], r["score"]) for r in rows]

    def vector_search(self, query: str, tenant_id: str,
                      candidate_ids: Optional[Sequence[str]] = None,
                      limit: int = 40) -> List[str]:
        """Node ids by descending cosine similarity. [] when embeddings are off.

        Brute force is deliberate: a curated Brain is thousands of nodes, and a
        dot product over a normalised float32 matrix of that size is sub-millisecond.
        An ANN index would add a second stateful store for no measurable gain.
        """
        if candidate_ids is not None and len(candidate_ids) == 0:
            return []
        if not self.embedding_available:
            return []
        qvec = self.embedder.encode_query(query)
        if qvec is None:
            return []

        rows = self._load_vectors(tenant_id, candidate_ids)
        if not rows:
            return []

        ids, matrix = rows
        sims = matrix @ np.asarray(qvec, dtype=np.float32)
        order = np.argsort(-sims)[:limit]
        return [ids[i] for i in order]

    def _load_vectors(self, tenant_id: str,
                      candidate_ids: Optional[Sequence[str]]
                      ) -> Optional[Tuple[List[str], np.ndarray]]:
        sql = ("SELECT node_id, dim, vector FROM knowledge_vectors "
               "WHERE tenant_id = ? AND model = ?")
        params: List[object] = [tenant_id, self.embedder.model_name]
        sql += self._restrict_clause(candidate_ids, params)

        try:
            rows = self.store.query_all(sql, tuple(params))
        except Exception as exc:  # noqa: BLE001
            logger.warning("vector load failed for tenant %s: %s", tenant_id, exc,
                           exc_info=True)
            return None
        if not rows:
            return None

        wanted = set(candidate_ids) if candidate_ids else None
        ids: List[str] = []
        vectors: List[np.ndarray] = []
        expected = self.embedder.dim
        for r in rows:
            if wanted is not None and r["node_id"] not in wanted:
                continue  # candidate set was too large to bind; filter here instead
            if int(r["dim"]) != expected:
                logger.warning("skipping %s: vector dim %s != model dim %s "
                               "(reindex required)", r["node_id"], r["dim"], expected)
                continue
            ids.append(r["node_id"])
            vectors.append(np.frombuffer(r["vector"], dtype=np.float32))

        if not ids:
            return None
        return ids, np.vstack(vectors)

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
