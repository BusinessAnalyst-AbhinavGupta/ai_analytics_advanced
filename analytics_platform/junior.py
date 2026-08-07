"""Junior maturity-stage engine.

A read-only, stage-gated agent over the governed Brain (plan P5). It never writes
or approves anything — it measures maturity and reproduces *already-approved*
metrics to prove understanding before it is let loose on new questions.

Stages (higher needs the previous):
  0 provisioning            tenant + company profile exist
  1 schema/EDA ready        approved term definitions / a mapped data source
  2 metric understanding    approved query nodes exist
  3 process analysis        approved queries actually reproduce (exec) + targets set

The `executor` is injectable (`SamplerExecutor` offline; `BrowserSessionExecutor`
toward live Metabase), so the same engine validates against local warehouses or a
real data source.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .brain.store import CompanyBrain
from .database import Store
from .domain import KnowledgeNode, NodeKind, ReviewStatus
from .execution.base import ExecutionContext, QueryExecutor
from .observability import Observability
from .tenancy import TenantService


class JuniorEngine:
    def __init__(self, store: Store, executor: Optional[QueryExecutor] = None,
                 tenants: Optional[TenantService] = None,
                 observability: Optional[Observability] = None):
        from .execution.sampler import SamplerExecutor
        self.store = store
        self.executor = executor or SamplerExecutor()
        self.tenants = tenants or TenantService(store)
        self.obs = observability or Observability(store)

    def brain(self, tenant_id: str) -> CompanyBrain:
        return CompanyBrain(self.store, tenant_id)

    # -- reads ---------------------------------------------------------------
    def approved_queries(self, tenant_id: str, limit: int = 200) -> List[KnowledgeNode]:
        return [n for n in self.brain(tenant_id).all(limit=limit)
                if n.kind == NodeKind.QUERY and n.status.is_usable()
                and n.payload.get("sql")]

    def _approved_counts(self, tenant_id: str) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for n in self.brain(tenant_id).all(limit=100000):
            if n.status.is_usable():
                out[n.kind.value] = out.get(n.kind.value, 0) + 1
        return out

    def reproduce_metrics(self, tenant_id: str, limit: int = 200) -> Dict[str, Any]:
        """Run every approved query through the executor (read-only reproduction)."""
        results = []
        for n in self.approved_queries(tenant_id, limit=limit):
            r = self.executor.execute(
                n.payload["sql"],
                ExecutionContext(tenant_id=tenant_id,
                                 dialect=n.payload.get("dialect", "athena")))
            results.append({"node_id": n.id, "title": n.title, "ok": r.ok,
                            "row_count": r.row_count if r.ok else 0,
                            "error": r.error if not r.ok else ""})
        ok = sum(1 for x in results if x["ok"])
        return {"attempted": len(results), "reproduced": ok,
                "failed": [x for x in results if not x["ok"]][:20]}

    def stage(self, tenant_id: str, *, limit: int = 200) -> Dict[str, Any]:
        """Measure maturity. Read-only; never mutates or approves."""
        self.tenants.require_tenant(tenant_id)
        counts = self._approved_counts(tenant_id)
        defs = counts.get(NodeKind.DEFINITION.value, 0) \
            + counts.get(NodeKind.METRIC.value, 0)
        queries_approved = counts.get(NodeKind.QUERY.value, 0)
        repro = self.reproduce_metrics(tenant_id, limit=limit)
        profile = self.tenants.get_company_profile(tenant_id)
        targets = len(profile.targets) if profile else 0

        if repro["reproduced"] > 0 and targets > 0:
            stage = 3
        elif queries_approved > 0:
            stage = 2
        elif defs > 0 or self._has_tables(tenant_id):
            stage = 1
        else:
            stage = 0
        return {
            "tenant_id": tenant_id,
            "stage": stage,
            "maturity": ["provisioning", "data_discovery", "metric_understanding",
                         "process_analysis"][min(stage, 3)],
            "approved_by_kind": counts,
            "defined_terms": defs,
            "approved_queries": queries_approved,
            "reproduction": repro,
            "targets": targets,
        }

    def _has_tables(self, tenant_id: str) -> bool:
        for ds in self.tenants.list_datasources(tenant_id):
            if ds.get("tables"):
                return True
        return False


__all__ = ["JuniorEngine"]