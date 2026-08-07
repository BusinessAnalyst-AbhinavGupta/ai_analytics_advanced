"""Persist migration `NodeSpec` drafts into a CompanyBrain as CANDIDATE nodes.

Idempotency: a spec whose `source_ref` already exists in the target tenant's
brain is skipped, so re-running a migration never duplicates. Every written node
keeps `created_by="migration"`, provenance on `source_ref`/`evidence_ref`, and
`confidence` from the spec. Tenant scoping is enforced by `CompanyBrain` (it
asserts `node.tenant_id == tenant_id` on every write).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..brain.store import CompanyBrain
from ..domain import KnowledgeNode, ReviewStatus
from .mapper import NodeSpec, load_snapshot, plan_from_snapshot


def source_exists(brain: CompanyBrain, source_ref: str) -> bool:
    """True if any node in this tenant already carries the given source_ref."""
    row = brain.store.query_one(
        "SELECT id FROM knowledge_nodes WHERE tenant_id=? AND source_ref=?",
        (brain.tenant_id, source_ref))
    return row is not None


def migrate_specs(brain: CompanyBrain, specs: List[NodeSpec],
                  *,
                  force_status: Optional[ReviewStatus] = None,
                  created_by: Optional[str] = None) -> Dict[str, Any]:
    """Write specs into the brain. Returns `{imported, skipped, by_kind, total}`.

    Approvals remain the hard gate: every node is written as `CANDIDATE` unless
    `force_status` is passed by an explicit caller.
    """
    imported = 0
    skipped = 0
    by_kind: Dict[str, int] = {}
    status = force_status if force_status is not None else ReviewStatus.CANDIDATE
    actor = created_by or "migration"
    for spec in specs:
        if spec.source_ref and source_exists(brain, spec.source_ref):
            skipped += 1
            continue
        brain.create(
            kind=spec.kind,
            title=spec.title,
            payload=spec.payload,
            summary=spec.summary,
            source_ref=spec.source_ref,
            evidence_ref=spec.evidence_ref,
            confidence=spec.confidence,
            created_by=actor,
            status=status,
        )
        imported += 1
        by_kind[spec.kind.value] = by_kind.get(spec.kind.value, 0) + 1
    return {"imported": imported, "skipped": skipped,
            "by_kind": by_kind, "total": imported + skipped}


def migrate_from_snapshot(brain: CompanyBrain, snapshot_path: str,
                          *,
                          derive_definitions: bool = True,
                          created_by: str = "migration",
                          force_status: Optional[ReviewStatus] = None) -> Dict[str, Any]:
    """Load a snapshot file, plan it, and persist CANDIDATE nodes (idempotent)."""
    snapshot = load_snapshot(snapshot_path)
    specs = plan_from_snapshot(snapshot, derive_definitions=derive_definitions)
    return migrate_specs(brain, specs, force_status=force_status,
                         created_by=created_by)