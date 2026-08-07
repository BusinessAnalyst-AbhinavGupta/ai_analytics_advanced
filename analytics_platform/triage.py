"""Triage — a senior-review inbox over the governed Company Brain.

Lets an authorized reviewer work through CANDIDATE/UNDER_REVIEW nodes (e.g. the
1229 imported by the Brain v2 migration): list the queue, see title-conflict
candidates, and approve / reject individually or in bulk by kind.

Approval stays the hard gate: the only path to APPROVED is submit-then-approve,
and only actionable statuses (CANDIDATE / UNDER_REVIEW / REVISION_REQUIRED) are
ever touched — everything else is skipped, never mutated.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .brain.store import CompanyBrain
from .database import Store
from .domain import KnowledgeNode, NodeKind, ReviewStatus
from .observability import Observability

ACTIONABLE = (ReviewStatus.CANDIDATE, ReviewStatus.UNDER_REVIEW,
              ReviewStatus.REVISION_REQUIRED)


class TriageService:
    def __init__(self, store: Store, observability: Optional[Observability] = None):
        self.store = store
        self.obs = observability or Observability(store)

    def brain(self, tenant_id: str) -> CompanyBrain:
        return CompanyBrain(self.store, tenant_id)

    # -- reads ---------------------------------------------------------------
    def queue(self, tenant_id: str, *, kind: Optional[NodeKind] = None,
              search: str = "", limit: int = 200) -> List[KnowledgeNode]:
        """Pending nodes (any invoice status) filtered by kind/text."""
        q: List[KnowledgeNode] = []
        for n in self.brain(tenant_id).all(kind=kind, limit=limit):
            if search:
                s = search.lower()
                if s not in n.title.lower() and s not in n.summary.lower():
                    continue
            q.append(n)
        return q

    def summary(self, tenant_id: str) -> Dict[str, Any]:
        brain = self.brain(tenant_id)
        nodes = brain.all(limit=100000)
        by_status: Dict[str, int] = {}
        by_kind: Dict[str, int] = {}
        actionable = 0
        for n in nodes:
            by_status[n.status.value] = by_status.get(n.status.value, 0) + 1
            by_kind[n.kind.value] = by_kind.get(n.kind.value, 0) + 1
            if n.status in ACTIONABLE:
                actionable += 1
        return {
            "tenant_id": tenant_id,
            "total": len(nodes),
            "actionable": actionable,
            "by_status": by_status,
            "by_kind": by_kind,
            "approved": by_status.get(ReviewStatus.APPROVED.value, 0)
                        + by_status.get(ReviewStatus.APPROVED_WITH_CAVEATS.value, 0),
            "conflicts": len(brain.conflicts()),
        }

    def conflicts(self, tenant_id: str) -> List[Dict[str, Any]]:
        return self.brain(tenant_id).conflicts()

    # -- writes (gate kept) ---------------------------------------------------
    def _pending(self, brain: CompanyBrain, node_ids: List[str]) -> List[str]:
        """Only ids that are actionable; the rest are ignored (never mutated)."""
        return [nid for nid in node_ids
                if (n := brain.get(nid)) is not None and n.status in ACTIONABLE]

    def approve(self, tenant_id: str, node_ids: List[str], *,
                by: str = "senior", notes: str = "") -> Dict[str, Any]:
        brain = self.brain(tenant_id)
        ok, skipped = [], []
        for nid in node_ids:
            node = brain.get(nid)
            if node is None:
                skipped.append((nid, "unknown"))
            elif node.status in ACTIONABLE:
                if node.status == ReviewStatus.CANDIDATE:
                    brain.submit(nid, by=by)
                brain.approve(nid, by=by, notes=notes or "approved in triage")
                ok.append(nid)
            else:
                skipped.append((nid, node.status.value))
        self.obs.event(tenant_id=tenant_id, stage="triage.approve", actor=by,
                       meta={"approved": ok, "skipped": skipped})
        return {"approved": ok, "skipped": skipped}

    def reject(self, tenant_id: str, node_ids: List[str], *,
               by: str = "senior", notes: str = "") -> Dict[str, Any]:
        brain = self.brain(tenant_id)
        ok, skipped = [], []
        for nid in node_ids:
            node = brain.get(nid)
            if node is None:
                skipped.append((nid, "unknown"))
            elif node.status in ACTIONABLE:
                brain.reject(nid, by=by, notes=notes or "rejected in triage")
                ok.append(nid)
            else:
                skipped.append((nid, node.status.value))
        self.obs.event(tenant_id=tenant_id, stage="triage.reject", actor=by,
                       meta={"rejected": ok, "skipped": skipped})
        return {"rejected": ok, "skipped": skipped}

    def bulk(self, tenant_id: str, *, kind: Optional[NodeKind] = None,
             action: str = "approve", by: str = "senior",
             notes: str = "", limit: int = 500) -> Dict[str, Any]:
        target = [n.id for n in self.queue(tenant_id, kind=kind, limit=limit)
                  if n.status in ACTIONABLE]
        if action == "reject":
            return self.reject(tenant_id, target, by=by, notes=notes)
        return self.approve(tenant_id, target, by=by, notes=notes)


__all__ = ["TriageService", "ACTIONABLE"]