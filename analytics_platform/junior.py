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

    # -- stage 0-1: schema / EDA catalog (read-only) -------------------------
    def _tables(self, tenant_id: str) -> List[str]:
        seen: List[str] = []
        for ds in self.tenants.list_datasources(tenant_id):
            for t in ds.get("tables", []) or []:
                if t and t not in seen:
                    seen.append(t)
        return seen

    def catalog(self, tenant_id: str) -> Dict[str, Any]:
        """Describe registered tables via `SELECT * FROM t LIMIT 0` (dialect-agnostic)."""
        tables = self._tables(tenant_id)
        entries = []
        ok_n = 0
        for t in tables:
            r = self.executor.execute(
                f"SELECT * FROM {t} LIMIT 0",
                ExecutionContext(tenant_id=tenant_id, dialect="athena"))
            if r.ok and r.data is not None:
                entries.append({"table": t, "columns": list(r.data.columns),
                                "types": [str(d) for d in r.data.dtypes],
                                "error": ""})
                ok_n += 1
            else:
                entries.append({"table": t, "columns": [], "types": [],
                                "error": r.error or "unable to describe"})
        return {"tenant_id": tenant_id,
                "tables_known": len(tables),
                "tables_described": ok_n,
                "tables": entries}

    def datasets(self, tenant_id: str) -> List[str]:
        """Distinct column/schema names known across described tables (for EDA)."""
        return [t["table"] for t in self.catalog(tenant_id)["tables"] if t["columns"]]

    # -- stage 3: goal-aligned questions (read-only, deterministic) -----------
    def _usable_definitions(self, tenant_id: str) -> Dict[str, List[Any]]:
        out: Dict[str, List[Any]] = {}
        for n in self.brain(tenant_id).all(limit=100000):
            if n.kind == NodeKind.DEFINITION and n.status.is_usable():
                col = n.payload.get("column")
                if not col:
                    continue
                for v in n.payload.get("values", []):
                    if v not in out.setdefault(col, []):
                        out[col].append(v)
        return out

    def suggest_questions(self, tenant_id: str, *, limit_per_target: int = 2) -> Dict[str, Any]:
        """Turn CompanyProfile.targets (+ approved definitions/queries) into questions."""
        self.tenants.require_tenant(tenant_id)
        profile = self.tenants.get_company_profile(tenant_id)
        defs = self._usable_definitions(tenant_id)
        query_titles = [n.title for n in self.approved_queries(tenant_id)]
        catalog_columns = {c for t in self.catalog(tenant_id)["tables"] for c in t["columns"]}
        targets = list(profile.targets) if profile else []
        suggestions = []
        for t in targets:
            col = next((c for c in (t.metric_refs or []) if c in catalog_columns), None)
            if col is None:
                col = next((c for c in (t.metric_refs or []) if c in defs), None)
            source = ("approved_definition" if col in defs
                      else "metric" if col in catalog_columns else "adapted")
            question = (f"How has '{t.name}' ({t.category}) trended over time"
                        + (f" using {col}?" if col else "?"))
            suggestions.append({"target": t.name, "category": t.category,
                                "priority": t.priority, "question": question,
                                "columns": [col] if col else [], "source": source})
            if t.priority:
                # prioritize high-priority targets first; cap below
                pass
        # cap
        suggestions = suggestions[:max(len(targets), 1) * limit_per_target]

        # fallback when no targets / to enrich: approved queries + definitions
        if not targets:
            for title in query_titles[:limit_per_target]:
                suggestions.append({"target": "", "category": "", "priority": 0,
                                    "question": f"{title} — refresh/reproduce?",
                                    "columns": [], "source": "approved_query"})
            for col, vals in list(defs.items())[:limit_per_target]:
                suggestions.append({"target": "", "category": "", "priority": 0,
                                    "question": f"Analyze {col} distribution of {vals[:3]}?",
                                    "columns": [col], "source": "approved_definition"})
        return {"tenant_id": tenant_id, "count": len(suggestions),
                "suggestions": suggestions}


__all__ = ["JuniorEngine"]