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
from .domain import KnowledgeNode, NodeKind, ReviewStatus, clamp_junior_depth
from .execution.base import ExecutionContext, QueryExecutor
from .observability import Observability
from .tenancy import TenantService

# Deterministic hypothesis templates (scaled by junior depth; always additive).
_HYP_EXPLAIN = "'{0}' moved this period — what business driver explains it?"
_HYP_SEGMENT = "A specific segment (channel / cohort / region) drove '{0}' — which one?"
_HYP_BASELINE = "'{0}' changed because the underlying behaviour shifted, not the definition — verify against the baseline."


class JuniorEngine:
    def __init__(self, store: Store, executor: Optional[QueryExecutor] = None,
                 tenants: Optional[TenantService] = None,
                 observability: Optional[Observability] = None,
                 llm: Optional[Any] = None):
        from .execution.sampler import SamplerExecutor
        from .llm.client import NullClient
        self.store = store
        self.executor = executor or SamplerExecutor()
        self.tenants = tenants or TenantService(store)
        self.obs = observability or Observability(store)
        self.llm = llm or NullClient()  # injectable; NullClient (offline) by default

    def brain(self, tenant_id: str) -> CompanyBrain:
        return CompanyBrain(self.store, tenant_id)

    # -- reads ---------------------------------------------------------------
    def approved_queries(self, tenant_id: str, limit: int = 200) -> List[KnowledgeNode]:
        return [n for n in self.brain(tenant_id).usable_queries(limit=limit)
                if n.payload.get("sql")]

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

    # -- question depth (human-controlled on the senior tab) -------------------
    def junior_depth(self, tenant_id: str) -> int:
        """Current junior question depth (0..2), clamped. Defaults to 1."""
        try:
            return clamp_junior_depth(self.tenants.get_analyst_config(tenant_id).junior_depth)
        except Exception:  # noqa: BLE001 - best-effort read
            return 1

    def _depth_question(self, t, col: Optional[str], depth: int) -> str:
        """Depth-aware question wording for a target. Higher depth = deeper asks."""
        if depth <= 0:
            return (f"How does '{t.name}' ({t.category}) look this period"
                    + (f" using {col}?" if col else "?"))
        if depth == 1:
            return (f"How has '{t.name}' ({t.category}) trended over time"
                    + (f" using {col}?" if col else "?"))
        return (f"What drove the change in '{t.name}' ({t.category})"
                + (f", segmented by {col}?" if col else " — which segment drove it?"))

    def _hypothesis_question(self, t, col: Optional[str]) -> str:
        return (f"Which hypothesis best explains '{t.name}' ({t.category}) movement"
                + (f" — explore {col} segments and cohorts?" if col
                   else " — what changed in the funnel, and for whom?"))

    def suggest_questions(self, tenant_id: str, *, limit_per_target: int = 2) -> Dict[str, Any]:
        """Turn CompanyProfile.targets (+ approved definitions/queries) into questions."""
        self.tenants.require_tenant(tenant_id)
        profile = self.tenants.get_company_profile(tenant_id)
        defs = self._usable_definitions(tenant_id)
        query_titles = [n.title for n in self.approved_queries(tenant_id)]
        catalog_columns = {c for t in self.catalog(tenant_id)["tables"] for c in t["columns"]}
        targets = list(profile.targets) if profile else []
        suggestions = []
        depth = self.junior_depth(tenant_id)
        for t in targets:
            col = next((c for c in (t.metric_refs or []) if c in catalog_columns), None)
            if col is None:
                col = next((c for c in (t.metric_refs or []) if c in defs), None)
            source = ("approved_definition" if col in defs
                      else "metric" if col in catalog_columns else "adapted")
            question = self._depth_question(t, col, depth)
            suggestions.append({"target": t.name, "category": t.category,
                                "priority": t.priority, "question": question,
                                "columns": [col] if col else [], "source": source,
                                "depth": depth})
            if depth >= 2:
                # advanced: push one deeper business question per target too
                suggestions.append({"target": t.name, "category": t.category,
                                    "priority": t.priority,
                                    "question": self._hypothesis_question(t, col),
                                    "columns": [col] if col else [],
                                    "source": "deep", "depth": depth})
            if t.priority:
                # prioritize high-priority targets first; cap below
                pass
        # cap
        per = max(len(targets), 1)
        limit = per * limit_per_target if depth < 2 else per * max(limit_per_target, 2)
        suggestions = suggestions[:limit]

        # fallback when no targets / to enrich: approved queries + definitions
        if not targets:
            for title in query_titles[:limit_per_target]:
                suggestions.append({"target": "", "category": "", "priority": 0,
                                    "question": f"{title} — refresh/reproduce?",
                                    "columns": [], "source": "approved_query",
                                    "depth": depth})
            for col, vals in list(defs.items())[:limit_per_target]:
                suggestions.append({"target": "", "category": "", "priority": 0,
                                    "question": f"Analyze {col} distribution of {vals[:3]}?",
                                    "columns": [col], "source": "approved_definition",
                                    "depth": depth})
        # optional LLM enrichment (only when a live client is configured) -----
        self._enrich_with_llm(tenant_id, suggestions, profile)
        return {"tenant_id": tenant_id, "depth": depth, "count": len(suggestions),
                "suggestions": suggestions}

    def suggest_hypotheses(self, tenant_id: str, *, limit: int = 4) -> Dict[str, Any]:
        """Business hypotheses, scaled by junior depth. Deterministic-first
        (templates) so it works offline; LLM enrichment is purely additive."""
        depth = self.junior_depth(tenant_id)
        profile = self.tenants.get_company_profile(tenant_id)
        targets = list(profile.targets) if profile else []
        hyps: List[Dict[str, Any]] = []
        if depth >= 1:
            for t in targets[:limit]:
                hyps.append({"target": t.name, "category": t.category,
                             "hypothesis": _HYP_EXPLAIN.format(t.name),
                             "testable": True, "source": "template"})
                hyps.append({"target": t.name, "category": t.category,
                             "hypothesis": _HYP_SEGMENT.format(t.name),
                             "testable": True, "source": "template"})
        if depth >= 2 and not targets:
            for title in [n.title for n in self.approved_queries(tenant_id)][:limit]:
                hyps.append({"target": "", "category": "",
                             "hypothesis": _HYP_BASELINE.format(title),
                             "testable": True, "source": "template"})
        self._enrich_hypotheses_llm(tenant_id, hyps, profile, depth)
        return {"tenant_id": tenant_id, "depth": depth, "count": len(hyps),
                "hypotheses": hyps}

    def _llm_live(self) -> bool:
        """True when an actual LLM client (not the offline NullClient) is configured."""
        return getattr(self.llm, "name", "null") != "null"

    def _llm_lines(self, text: str, limit: int) -> List[str]:
        """Parse newline-delimited LLM lines, stripping numbering/bullets."""
        out: List[str] = []
        for line in (text or "").splitlines():
            line = line.strip().lstrip("0123456789.-• ").strip()
            if line and len(out) < limit:
                out.append(line)
        return out

    def _enrich_with_llm(self, tenant_id: str, suggestions: List[Dict[str, Any]],
                         profile: Optional[Any]) -> None:
        """Optional LLM-authored questions (only when a live client is wired).

        Purely additive: never removes the deterministic suggestions, never sends
        raw rows/sql/cookies to the LLM (only targets + existing question text),
        and a failure is logged as observability, never raised.
        """
        if not self._llm_live():
            return
        targets = list(profile.targets) if profile else []
        context = "\n".join(f"- {s['question']}" for s in suggestions) or "no approved targets yet"
        prompt = (
            "Suggest 2 concise, data-driven analysis questions for a product analyst.\n"
            f"Company profile targets: {', '.join(t.name for t in targets) or 'none provided'}.\n"
            f"Already proposed:\n{context}\n"
            "Return only the questions, one per line, no numbering."
        )
        try:
            res = self.llm.generate(prompt=prompt, system_prompt=(
                "You are a senior product-analytics assistant. Be concrete and measurable."),
                temperature=0.3)
            added = 0
            for line in self._llm_lines(res.text, 2):
                suggestions.append({"target": "", "category": "llm", "priority": 0,
                                    "question": line, "columns": [], "source": "llm"})
                added += 1
            self.obs.event(tenant_id=tenant_id, stage="junior.suggest.llm", actor="junior",
                           tokens_out=getattr(res, "tokens_out", 0), meta={"added": added})
        except Exception as e:  # noqa: BLE001 - LLM is an optional enhancement
            self.obs.event(tenant_id=tenant_id, stage="junior.suggest.llm", actor="junior",
                           status="FAILED", meta={"error": str(e)})

    def _enrich_hypotheses_llm(self, tenant_id: str, hyps: List[Dict[str, Any]],
                               profile: Optional[Any], depth: int) -> None:
        """Optional LLM-authored business hypotheses, additive, depth-scaled."""
        if not self._llm_live() or depth < 2:
            return
        targets = list(profile.targets) if profile else []
        context = "\n".join(f"- {h['hypothesis']}" for h in hyps) or "no approved targets yet"
        prompt = (
            "Suggest 2 concise, testable business hypotheses for a product analyst.\n"
            f"Company profile targets: {', '.join(t.name for t in targets) or 'none provided'}.\n"
            f"Already proposed:\n{context}\n"
            "Return only the hypotheses, one per line, no numbering."
        )
        try:
            res = self.llm.generate(prompt=prompt, system_prompt=(
                "You are a senior product-analytics assistant. Hypotheses must be "
                "specific and falsifiable with the company's own data."),
                temperature=0.4)
            added = 0
            for line in self._llm_lines(res.text, 2):
                hyps.append({"target": "", "category": "llm",
                             "hypothesis": line, "testable": True, "source": "llm"})
                added += 1
            self.obs.event(tenant_id=tenant_id, stage="junior.hypotheses.llm", actor="junior",
                           tokens_out=getattr(res, "tokens_out", 0),
                           meta={"depth": depth, "added": added})
        except Exception as e:  # noqa: BLE001 - LLM is an optional enhancement
            self.obs.event(tenant_id=tenant_id, stage="junior.hypotheses.llm", actor="junior",
                           status="FAILED", meta={"error": str(e)})


__all__ = ["JuniorEngine"]