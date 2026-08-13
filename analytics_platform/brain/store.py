"""Company Brain — a governed, versioned store of approved knowledge.

Every node carries tenant, status, version, provenance (created_by, reviewed_by,
evidence_ref, source_ref) and multi-dimension confidence. Approval status is a
hard filter for retrieval; confidence only ranks.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..database import Store, dump_json, load_json
from ..domain import (KnowledgeNode, NodeKind, ReviewStatus, new_id, now_iso)
try:
    from .vector_store import BrainVectorStore
except ImportError:
    BrainVectorStore = None


VALID_TRANSITIONS: Dict[ReviewStatus, List[ReviewStatus]] = {
    ReviewStatus.CANDIDATE: [ReviewStatus.UNDER_REVIEW, ReviewStatus.REJECTED],
    ReviewStatus.UNDER_REVIEW: [ReviewStatus.APPROVED, ReviewStatus.APPROVED_WITH_CAVEATS,
                                 ReviewStatus.REVISION_REQUIRED, ReviewStatus.REJECTED],
    ReviewStatus.REVISION_REQUIRED: [ReviewStatus.UNDER_REVIEW, ReviewStatus.REJECTED],
    ReviewStatus.APPROVED: [ReviewStatus.STALE, ReviewStatus.SUPERSEDED],
    ReviewStatus.APPROVED_WITH_CAVEATS: [ReviewStatus.STALE, ReviewStatus.SUPERSEDED],
    ReviewStatus.STALE: [ReviewStatus.UNDER_REVIEW, ReviewStatus.ARCHIVED],
    ReviewStatus.SUPERSEDED: [ReviewStatus.ARCHIVED],
    ReviewStatus.REJECTED: [ReviewStatus.ARCHIVED],
}


class BrainConflict(Exception):
    pass


class CompanyBrain:
    def __init__(self, store: Store, tenant_id: str, vector_store: Optional['BrainVectorStore'] = None):
        self.store = store
        self.tenant_id = tenant_id
        self.vector_store = vector_store

    def _sync_vector(self, node: KnowledgeNode) -> None:
        if not self.vector_store:
            return
        text = f"{node.title}\n{node.summary}"
        if node.payload and node.payload.get("sql"):
            text += f"\nSQL: {node.payload['sql']}"
            
        metadata = {
            "tenant_id": node.tenant_id,
            "kind": node.kind.value,
            "status": node.status.value,
        }
        try:
            self.vector_store.upsert_node(node.id, text, metadata)
        except Exception:
            pass

    def reindex_vectors(self) -> int:
        """Rebuild the vector index from all approved/usable nodes in SQLite."""
        if not self.vector_store:
            return 0
        nodes = self.usable_queries(limit=10000)
        # Also include approved definitions
        statuses = [ReviewStatus.APPROVED.value, ReviewStatus.APPROVED_WITH_CAVEATS.value]
        placeholders = ",".join("?" for _ in statuses)
        rows = self.store.query_all(
            f"SELECT * FROM knowledge_nodes WHERE tenant_id=? AND kind=? "
            f"AND status IN ({placeholders})",
            (self.tenant_id, NodeKind.DEFINITION.value, *statuses))
        nodes.extend([self._row_to_node(r) for r in rows])
        
        count = 0
        for n in nodes:
            self._sync_vector(n)
            count += 1
        return count

    # -- write ---------------------------------------------------------------
    def add_node(self, node: KnowledgeNode) -> KnowledgeNode:
        assert node.tenant_id == self.tenant_id
        self.store.execute(
            "INSERT INTO knowledge_nodes (id,tenant_id,kind,status,version,title,summary,payload,"
            "confidence,evidence_ref,source_ref,created_at,updated_at,created_by,reviewed_by,review_notes,supersedes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (node.id, node.tenant_id, node.kind.value, node.status.value, node.version,
             node.title, node.summary, dump_json(node.payload), dump_json(node.confidence),
             node.evidence_ref, node.source_ref, node.created_at, node.updated_at,
             node.created_by, node.reviewed_by, node.review_notes, node.supersedes))
        self._sync_vector(node)
        return node

    def create(self, kind: NodeKind, title: str, payload: Optional[Dict[str, Any]] = None,
               summary: str = "", confidence: Optional[Dict[str, float]] = None,
               evidence_ref: str = "", source_ref: str = "", created_by: str = "system",
               status: ReviewStatus = ReviewStatus.CANDIDATE) -> KnowledgeNode:
        base_conf = {"evidence": 0.0, "review": 0.0, "definition": 0.0,
                     "freshness": 1.0, "reproducibility": 0.0, "source": 0.5}
        if confidence:
            base_conf.update(confidence)
        node = KnowledgeNode(id=new_id("kn"), tenant_id=self.tenant_id, kind=kind,
                             status=status, title=title, summary=summary,
                             payload=payload or {}, confidence=base_conf,
                             evidence_ref=evidence_ref, source_ref=source_ref,
                             created_by=created_by)
        return self.add_node(node)

    def get(self, node_id: str) -> Optional[KnowledgeNode]:
        row = self.store.query_one(
            "SELECT * FROM knowledge_nodes WHERE id=? AND tenant_id=?", (node_id, self.tenant_id))
        return self._row_to_node(row) if row else None

    def _row_to_node(self, row) -> KnowledgeNode:
        r = dict(row)
        r["kind"] = NodeKind(r["kind"])
        r["status"] = ReviewStatus(r["status"])
        r["payload"] = load_json(r["payload"], {})
        r["confidence"] = load_json(r["confidence"], {})
        return KnowledgeNode(**r)

    def transition(self, node_id: str, to: ReviewStatus, by: str = "senior",
                   notes: str = "") -> KnowledgeNode:
        node = self.get(node_id)
        if node is None:
            raise KeyError(f"Unknown node {node_id}")
        if to not in VALID_TRANSITIONS.get(node.status, []):
            raise BrainConflict(
                f"Illegal transition {node.status.value} -> {to.value} for {node_id}")
        self.store.execute(
            "UPDATE knowledge_nodes SET status=?, updated_at=?, reviewed_by=?, review_notes=? "
            "WHERE id=? AND tenant_id=?",
            (to.value, now_iso(), by, notes, node_id, self.tenant_id))
        node = self.get(node_id)
        if node:
            self._sync_vector(node)
        return node

    # convenience transitions
    def submit(self, node_id: str, by: str = "junior", notes: str = "") -> KnowledgeNode:
        return self.transition(node_id, ReviewStatus.UNDER_REVIEW, by=by,
                               notes=notes or "submitted for review")

    def approve(self, node_id: str, by: str = "senior", notes: str = "") -> KnowledgeNode:
        node = self.transition(node_id, ReviewStatus.APPROVED, by=by, notes=notes or "approved")
        conf = node.confidence.copy()
        conf["review"] = 1.0
        conf["reproducibility"] = max(conf.get("reproducibility", 0.0), 0.7)
        self._set_confidence(node_id, conf)
        return self.get(node_id)

    def approve_with_caveats(self, node_id: str, by: str = "senior", notes: str = "") -> KnowledgeNode:
        return self.transition(node_id, ReviewStatus.APPROVED_WITH_CAVEATS, by=by, notes=notes)

    def reject(self, node_id: str, by: str = "senior", notes: str = "") -> KnowledgeNode:
        return self.transition(node_id, ReviewStatus.REJECTED, by=by, notes=notes or "rejected")

    def revise(self, node_id: str, by: str = "senior", notes: str = "") -> KnowledgeNode:
        return self.transition(node_id, ReviewStatus.REVISION_REQUIRED, by=by, notes=notes or "revision required")

    def mark_stale(self, node_id: str, by: str = "system") -> KnowledgeNode:
        return self.transition(node_id, ReviewStatus.STALE, by=by, notes="marked stale")

    def supersede(self, node_id: str, new_node_id: str, by: str = "system") -> KnowledgeNode:
        self.transition(node_id, ReviewStatus.SUPERSEDED, by=by, notes=f"superseded by {new_node_id}")
        return self.update_field(node_id, "supersedes", new_node_id)

    def update_field(self, node_id: str, field: str, value: Any) -> KnowledgeNode:
        self.store.execute(
            f"UPDATE knowledge_nodes SET {field}=?, updated_at=? WHERE id=? AND tenant_id=?",
            (value, now_iso(), node_id, self.tenant_id))
        node = self.get(node_id)
        if node:
            self._sync_vector(node)
        return node

    def _set_confidence(self, node_id: str, conf: Dict[str, float]) -> None:
        self.store.execute(
            "UPDATE knowledge_nodes SET confidence=?, updated_at=? WHERE id=? AND tenant_id=?",
            (dump_json(conf), now_iso(), node_id, self.tenant_id))

    # -- read ---------------------------------------------------------------
    def search(self, query: str = "", kind: Optional[NodeKind] = None,
               usable_only: bool = True, limit: int = 20) -> List[KnowledgeNode]:
        
        vector_ids = []
        if query and self.vector_store:
            filters = {"tenant_id": self.tenant_id}
            if kind is not None:
                filters["kind"] = kind.value if hasattr(kind, 'value') else kind
            if usable_only:
                filters["status"] = {"$in": ["APPROVED", "APPROVED_WITH_CAVEATS"]}
            try:
                vector_ids = self.vector_store.search_similar(query, limit=limit * 2, metadata_filters=filters)
            except Exception:
                pass

        sql = "SELECT * FROM knowledge_nodes WHERE tenant_id=?"
        params: List[Any] = [self.tenant_id]
        if usable_only:
            sql += " AND status IN ('APPROVED','APPROVED_WITH_CAVEATS')"
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind.value if hasattr(kind, 'value') else kind)
            
        if vector_ids:
            placeholders = ",".join("?" for _ in vector_ids)
            sql += f" AND (id IN ({placeholders}) OR title LIKE ? OR summary LIKE ?)"
            params.extend(vector_ids)
            like = f"%{query}%"
            params += [like, like]
        elif query:
            sql += " AND (title LIKE ? OR summary LIKE ?)"
            like = f"%{query}%"
            params += [like, like]
            
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        
        nodes = [self._row_to_node(r) for r in self.store.query_all(sql, tuple(params))]
        if vector_ids:
            nodes.sort(key=lambda n: vector_ids.index(n.id) if n.id in vector_ids else 9999)
        return nodes

    def usable_queries(self, limit: int = 200) -> List[KnowledgeNode]:
        """All QUERY nodes whose status is usable (approved), newest first.

        Filters in SQL so a large Brain (e.g. after a migration) is scanned fully
        rather than whoever happens to fall in the first `limit` rows of `all()`.
        """
        statuses = [ReviewStatus.APPROVED.value, ReviewStatus.APPROVED_WITH_CAVEATS.value]
        placeholders = ",".join("?" for _ in statuses)
        rows = self.store.query_all(
            f"SELECT * FROM knowledge_nodes WHERE tenant_id=? AND kind=? "
            f"AND status IN ({placeholders}) ORDER BY updated_at DESC LIMIT ?",
            (self.tenant_id, NodeKind.QUERY.value, *statuses, limit))
        return [self._row_to_node(r) for r in rows]

    def all(self, kind: Optional[NodeKind] = None, status: Optional[ReviewStatus] = None,
            limit: int = 200) -> List[KnowledgeNode]:
        sql = "SELECT * FROM knowledge_nodes WHERE tenant_id=?"
        params: List[Any] = [self.tenant_id]
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind.value)
        if status is not None:
            sql += " AND status=?"
            params.append(status.value)
        sql += " LIMIT ?"
        params.append(limit)
        return [self._row_to_node(r) for r in self.store.query_all(sql, tuple(params))]

    # statuses that no longer participate in title-conflict detection (discarded).
    _DISCARDED_CONFLICT_STATUSES = ("REJECTED", "STALE", "SUPERSEDED")

    def conflicts(self) -> List[Dict[str, Any]]:
        """Nodes sharing a title (>1 kept) are conflicting/duplicate candidates.

        Discarded statuses (REJECTED/STALE/SUPERSEDED) are excluded so that
        resolving a group (reject/revise away the duplicates) actually clears it
        instead of leaving ghosts behind.
        """
        placeholders = ",".join("?" * len(self._DISCARDED_CONFLICT_STATUSES))
        rows = self.store.query_all(
            "SELECT title, COUNT(*) c, GROUP_CONCAT(id) ids FROM knowledge_nodes "
            f"WHERE tenant_id=? AND status NOT IN ({placeholders}) "
            "GROUP BY title HAVING c > 1",
            (self.tenant_id, *self._DISCARDED_CONFLICT_STATUSES))
        return [{"title": r["title"], "count": r["c"], "ids": (r["ids"] or "").split(",")}
                for r in rows]

    def stats(self) -> Dict[str, Any]:
        total = self.store.query_one(
            "SELECT COUNT(*) c FROM knowledge_nodes WHERE tenant_id=?", (self.tenant_id,))["c"]
        approved = self.store.query_one(
            "SELECT COUNT(*) c FROM knowledge_nodes WHERE tenant_id=? AND status='APPROVED'",
            (self.tenant_id,))["c"]
        stale = self.store.query_one(
            "SELECT COUNT(*) c FROM knowledge_nodes WHERE tenant_id=? AND status='STALE'",
            (self.tenant_id,))["c"]
        by_kind = self.store.query_all(
            "SELECT kind, COUNT(*) c FROM knowledge_nodes WHERE tenant_id=? GROUP BY kind",
            (self.tenant_id,))
        return {"total_nodes": total, "approved": approved, "stale": stale,
                "by_kind": [{"kind": r["kind"], "count": r["c"]} for r in by_kind],
                "conflicts": len(self.conflicts())}