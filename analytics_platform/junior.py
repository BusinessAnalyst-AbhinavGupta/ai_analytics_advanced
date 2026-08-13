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

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from .brain.store import CompanyBrain
from .config import Settings
from .database import Store
from .domain import KnowledgeNode, NodeKind, ReviewStatus, clamp_junior_depth
from .execution.base import ExecutionContext, QueryExecutor
from .llm.client import make_role_client
from .observability import Observability
from .stores import TenantStoreProvider
from .tenancy import TenantService

logger = logging.getLogger(__name__)

# Deterministic hypothesis templates (scaled by junior depth; always additive).
_HYP_EXPLAIN = "'{0}' moved this period — what business driver explains it?"
_HYP_SEGMENT = "A specific segment (channel / cohort / region) drove '{0}' — which one?"
_HYP_BASELINE = "'{0}' changed because the underlying behaviour shifted, not the definition — verify against the baseline."

# Process-wide LLM enrichment cache (CP-13 bill guard). The API builds a fresh
# JuniorEngine per request, so the cache must live at module scope to actually
# throttle cross-request calls (UI reruns, schedulers, other clients).
_LLM_ENRICH_CACHE: Dict[tuple, tuple] = {}   # key -> (ts, lines_text)
_LLM_ENRICH_LOCK = threading.Lock()


class JuniorEngine:
    def __init__(self, stores: TenantStoreProvider, executor: Optional[QueryExecutor] = None,
                 tenants: Optional[TenantService] = None,
                 observability: Optional[Observability] = None,
                 settings: Optional[Settings] = None):
        from .execution.sampler import SamplerExecutor
        from .config import Settings
        self.stores = stores
        self.executor = executor or SamplerExecutor()
        self.tenants = tenants or TenantService(stores)
        self.obs = observability or Observability(stores)
        self.settings = settings or Settings()

    @property
    def llm_cache_ttl_seconds(self) -> int:
        return int(getattr(self.settings, "junior_llm_cache_ttl_minutes", 60)) * 60

    @property
    def llm_daily_cap(self) -> int:
        return int(getattr(self.settings, "llm_daily_cap", 20))

    # -- LLM enrichment cache + daily budget (bill guard) --------------------
    def _llm_cache_key(self, tenant_id: str, kind: str, depth: int) -> tuple:
        return (tenant_id, kind, int(depth))

    def _llm_cache_get(self, key: tuple) -> Optional[str]:
        with _LLM_ENRICH_LOCK:
            entry = _LLM_ENRICH_CACHE.get(key)
            if not entry:
                return None
            ts, text = entry
            if time.time() - ts < self.llm_cache_ttl_seconds:
                return text
        return None

    def _llm_cache_put(self, key: tuple, text: str) -> None:
        with _LLM_ENRICH_LOCK:
            _LLM_ENRICH_CACHE[key] = (time.time(), text)

    def _llm_budget_key(self, tenant_id: str) -> str:
        return f"llm_daily:{tenant_id}:{time.strftime('%Y-%m-%d', time.gmtime())}"

    def _llm_budget_ok(self, tenant_id: str) -> bool:
        # scheduler_state is a control-plane table (there is one, not one per
        # tenant); the tenant_id lives inside the key, not in which file it's in.
        try:
            row = self.stores.control.query_one(
                "SELECT value FROM scheduler_state WHERE key=?", (self._llm_budget_key(tenant_id),))
            used = int(float(row["value"])) if row and row["value"] else 0
            return used < self.llm_daily_cap
        except Exception:  # noqa: BLE001 - best-effort
            # Fails OPEN: a control-store read failure here silently disables the
            # daily LLM spending cap for this tenant, so it must never pass
            # unnoticed — the next thing that happens is real money being spent.
            logger.warning(
                "could not read the LLM daily budget for tenant %r from the "
                "control store; allowing the call and leaving the %d/day cap "
                "unenforced for it", tenant_id, self.llm_daily_cap, exc_info=True)
            return True

    def _llm_spend(self, tenant_id: str) -> None:
        try:
            key = self._llm_budget_key(tenant_id)
            row = self.stores.control.query_one(
                "SELECT value FROM scheduler_state WHERE key=?", (key,))
            used = int(float(row["value"])) if row and row["value"] else 0
            self.stores.control.execute(
                "INSERT INTO scheduler_state (key,value,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, str(used + 1),
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        except Exception:  # noqa: BLE001 - best-effort
            # An unrecorded spend is an under-counted budget: the cap reads low
            # for the rest of the day and lets extra LLM calls through.
            logger.warning(
                "could not record an LLM spend for tenant %r in the control "
                "store; today's %d/day cap is now under-counted",
                tenant_id, self.llm_daily_cap, exc_info=True)

    def brain(self, tenant_id: str) -> CompanyBrain:
        return CompanyBrain(self.stores.for_tenant(tenant_id), tenant_id)

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
                    
        # CP-16: Also discover root tables embedded in the knowledge queue
        brain = self.brain(tenant_id)
        from .brain.ingest import extract
        
        for node in brain.all(kind=NodeKind.QUERY, limit=1000):
            payload_tables = node.payload.get("structure", {}).get("tables")
            if payload_tables is None:
                if node.payload.get("table_name"):
                    payload_tables = [node.payload["table_name"]]
                else:
                    sql = node.payload.get("sql")
                    if sql:
                        payload_tables = extract(sql).get("tables", [])
            for t in payload_tables or []:
                if t and t not in seen:
                    seen.append(t)
                    
        for node in brain.all(kind=NodeKind.DEFINITION, limit=1000):
            if node.title.startswith("Table: "):
                t = node.title[7:].strip()
                if t and t not in seen:
                    seen.append(t)
        return seen
                    
    def get_catalog(self, tenant_id: str) -> Dict[str, Any]:
        """Fetch the cached schema catalog from the Company Brain (instant)."""
        nodes = self.brain(tenant_id).all(kind=NodeKind.DEFINITION)
        cat_node = next((n for n in nodes if n.title == "Database Catalog"), None)
        if cat_node and "tables" in cat_node.payload:
            return cat_node.payload

        # Return skeleton if no catalog exists yet
        tables = self._tables(tenant_id)
        return {"tenant_id": tenant_id, "tables_known": len(tables),
                "tables_described": 0, "tables": []}

    def refresh_catalog(self, tenant_id: str) -> Dict[str, Any]:
        """Probe the database schema (LIMIT 1) with priority to new tables, and cache to Brain."""
        physical_tables = set(self._tables(tenant_id))
        nodes = self.brain(tenant_id).all(kind=NodeKind.DEFINITION)
        cat_node = next((n for n in nodes if n.title == "Database Catalog"), None)
        
        cached_tables = {}
        if cat_node and "tables" in cat_node.payload:
            for t in cat_node.payload["tables"]:
                if t["table"] in physical_tables:
                    cached_tables[t["table"]] = t
                    
        new_tables = [t for t in physical_tables if t not in cached_tables]
        existing_tables = [t for t in physical_tables if t in cached_tables]
        
        # Priority 1: Probe new tables
        for t in new_tables:
            r = self.executor.execute(
                f"SELECT * FROM {t} LIMIT 1",
                ExecutionContext(tenant_id=tenant_id, dialect="athena"))
            if r.ok and r.data is not None:
                cached_tables[t] = {"table": t, "columns": list(r.data.columns),
                                    "types": [str(d) for d in r.data.dtypes], "error": ""}
            else:
                cached_tables[t] = {"table": t, "columns": [], "types": [],
                                    "error": r.error or "unable to describe"}
                                    
        # Priority 2: Refresh existing tables
        for t in existing_tables:
            r = self.executor.execute(
                f"SELECT * FROM {t} LIMIT 1",
                ExecutionContext(tenant_id=tenant_id, dialect="athena"))
            if r.ok and r.data is not None:
                cached_tables[t] = {"table": t, "columns": list(r.data.columns),
                                    "types": [str(d) for d in r.data.dtypes], "error": ""}
            else:
                cached_tables[t] = {"table": t, "columns": [], "types": [],
                                    "error": r.error or "unable to describe"}
                                    
        ok_n = sum(1 for t in cached_tables.values() if not t.get("error"))
        entries = [cached_tables[t] for t in physical_tables]
        
        payload = {
            "tenant_id": tenant_id,
            "tables_known": len(physical_tables),
            "tables_described": ok_n,
            "tables": entries
        }
        
        # Persist to Company Brain
        if cat_node:
            cat_node.payload = payload
            cat_node.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            # We don't have update_node exposed simply in store, but we can recreate it or find the right API.
            # Wait, how do we update? The BrainStore doesn't expose a raw update payload method.
            # Let's check BrainStore methods in a moment. We'll use a hack if needed.
            # Let's just create a new one and supersede the old one.
            self.brain(tenant_id).create(
                kind=NodeKind.DEFINITION, title="Database Catalog",
                summary="Full database schema catalog, automatically maintained.",
                payload=payload, status=ReviewStatus.APPROVED
            )
        else:
            self.brain(tenant_id).create(
                kind=NodeKind.DEFINITION, title="Database Catalog",
                summary="Full database schema catalog, automatically maintained.",
                payload=payload, status=ReviewStatus.APPROVED
            )
            
        return payload

    def datasets(self, tenant_id: str) -> List[str]:
        """Distinct column/schema names known across described tables (for EDA)."""
        return [t["table"] for t in self.get_catalog(tenant_id)["tables"] if t["columns"]]

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

    def _lo(self, category: str, question: str, columns: List[str], sql: str,
            depth: int) -> Dict[str, Any]:
        """One low-level taxonomy entry (CP-15). Level is always 'low' so these
        auto-fold to FINDINGs (under cap) and never need a human."""
        return {"target": "", "category": category, "priority": 0, "question": question,
                "columns": columns, "source": "low_level_taxonomy", "depth": depth,
                "level": "low", "sql": sql}

    def _low_level_pool(self, depth: int, catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Low-level exploratory taxonomy: schema, fill rate, success trends,
        breakdown/funnel by dimension, univariate contribution — single-table, safe
        SQL built from the catalog. Promotion/demotion controls breadth (depth 0 =
        narrower set; depth 1 adds contribution)."""
        out: List[Dict[str, Any]] = []
        for t in catalog.get("tables") or []:
            cols = t.get("columns") or []
            if not cols:
                continue
            name = t["table"]
            first = cols[0]
            dim = next((c for c in cols
                        if not any(k in str(c).lower() for k in
                                   ("id", "date", "time", "_at", "created", "updated",
                                    "timestamp"))), first)
            date_col = next((c for c in cols
                             if any(k in str(c).lower() for k in
                                    ("date", "time", "created", "updated", "timestamp"))), None)
            # schema (a probe: no data -> never promoted, by design)
            out.append(self._lo("schema",
                                f"Describe the schema of `{name}` (columns + types).",
                                cols, f"SELECT {', '.join(cols[:8])} FROM {name} LIMIT 1",
                                depth))
            # fill rate / coverage of a meaningful column
            out.append(self._lo("fill_rate",
                                f"What is the fill rate (non-null coverage) of `{first}` in `{name}`?",
                                [first],
                                f"SELECT COUNT(*) AS total, COUNT({first}) AS non_null FROM {name}",
                                depth))
            # success trend over time (when a date column exists)
            if date_col:
                out.append(self._lo("success_trend",
                                    f"How has activity in `{name}` trended over time ({date_col})?",
                                    [date_col],
                                    f"SELECT {date_col} AS day, COUNT(*) AS volume FROM {name} "
                                    f"GROUP BY {date_col} ORDER BY 1", depth))
            # breakdown / funnel by a dimension
            out.append(self._lo(
                "breakdown" if len(catalog.get("tables") or []) == 1 else "funnel_by_dim",
                f"How is `{name}` distributed across `{dim}`?", [dim],
                f"SELECT {dim} AS dim, COUNT(*) AS n FROM {name} "
                f"GROUP BY {dim} ORDER BY n DESC LIMIT 50", depth))
            # univariate contribution / share-of (depth 1+ adds breadth)
            if depth >= 1:
                out.append(self._lo(
                    "contribution_uni",
                    f"What share (contribution) does each `{dim}` in `{name}` make?", [dim],
                    f"SELECT {dim} AS dim, COUNT(*) AS n, "
                    f"ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct "
                    f"FROM {name} GROUP BY {dim} ORDER BY pct DESC LIMIT 50", depth))
        return out

    def suggest_questions(self, tenant_id: str, *, limit_per_target: int = 2) -> Dict[str, Any]:
        """Turn CompanyProfile.targets + the catalog into the full junior question set.

        CP-15 two-tier: the low-level exploratory taxonomy (schema, fill rate,
        success trends, breakdown/funnel by dimension, univariate contribution) is
        always offered and auto-folds to FINDINGs (never human-gated); high-level
        (hypothesis formation / RCA) is offered ONLY when the junior is promoted
        (`junior_depth>=2`) and is human-governed. Promotion/demotion on the senior
        tab is therefore the control for the level of work.
        """
        self.tenants.require_tenant(tenant_id)
        profile = self.tenants.get_company_profile(tenant_id)
        defs = self._usable_definitions(tenant_id)
        query_titles = [n.title for n in self.approved_queries(tenant_id)]
        catalog = self.refresh_catalog(tenant_id)
        catalog_columns = {c for t in catalog["tables"] for c in t["columns"]}
        targets = list(profile.targets) if profile else []
        suggestions = []
        depth = self.junior_depth(tenant_id)

        level_by_depth = {0: "low", 1: "low", 2: "high"}
        cat_by_depth = {0: "overview", 1: "success_trend", 2: "rca"}
        my_level = level_by_depth.get(depth, "low")


        for t in targets:
            col = next((c for c in (t.metric_refs or []) if c in catalog_columns), None)
            if col is None:
                col = next((c for c in (t.metric_refs or []) if c in defs), None)
            source = ("approved_definition" if col in defs
                      else "metric" if col in catalog_columns else "adapted")
            question = self._depth_question(t, col, depth)
            suggestions.append({"target": t.name, "category": cat_by_depth.get(depth, "low"),
                                "priority": t.priority, "question": question,
                                "columns": [col] if col else [], "source": source,
                                "depth": depth, "level": my_level})
            if depth >= 2:
                # promoted junior: high-level hypothesis / RCA per target
                suggestions.append({"target": t.name, "category": "hypothesis",
                                    "priority": t.priority,
                                    "question": self._hypothesis_question(t, col),
                                    "columns": [col] if col else [], "source": "deep",
                                    "depth": depth, "level": "high"})
        # cap the target-derived questions
        per = max(len(targets), 1)
        limit = per * limit_per_target if depth < 2 else per * max(limit_per_target, 2)
        suggestions = suggestions[:limit]

        # low-level exploratory taxonomy (always available; auto-folds to FINDINGs)
        suggestions.extend(self._low_level_pool(depth, catalog))

        # fallback when no targets: approved queries + definitions (low-level)
        if not targets:
            for title in query_titles[:limit_per_target]:
                suggestions.append({"target": "", "category": "reproduce", "priority": 0,
                                    "question": f"{title} — refresh/reproduce?",
                                    "columns": [], "source": "approved_query",
                                    "depth": depth, "level": "low"})
            for col, vals in list(defs.items())[:limit_per_target]:
                suggestions.append({"target": "", "category": "breakdown", "priority": 0,
                                    "question": f"Analyze {col} distribution of {vals[:3]}?",
                                    "columns": [col], "source": "approved_definition",
                                    "depth": depth, "level": "low"})
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

    def _llm_live(self, llm: Any) -> bool:
        """True when an actual LLM client (not the offline NullClient) is configured."""
        return getattr(llm, "name", "null") != "null"

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
        """Optional LLM-authored questions, throttled (CP-13).

        Additive only; deterministic suggestions are never removed and raw
        rows/sql/cookies are never sent. The OpenRouter call fires at most once
        per `llm_cache_ttl_minutes` per (tenant, depth), and at most
        `llm_daily_cap` times per UTC day (persisted) — so UI reruns and
        background calls share the budget and cannot multiply the bill.
        """
        cfg = self.tenants.get_analyst_config(tenant_id)
        if not cfg.junior.enabled:
            return
        llm = make_role_client(self.settings, cfg.junior)
        if not self._llm_live(llm):
            return
        depth = self.junior_depth(tenant_id)
        key = self._llm_cache_key(tenant_id, "questions", depth)
        cached = self._llm_cache_get(key)
        if cached is not None:
            added = 0
            for line in self._llm_lines(cached, 2):
                suggestions.append({"target": "", "category": "llm", "priority": 0,
                                    "question": line, "columns": [], "source": "llm"})
                added += 1
            self.obs.event(tenant_id=tenant_id, stage="junior.suggest.llm", actor="junior",
                           status="CACHED", meta={"added": added})
            return
        if not self._llm_budget_ok(tenant_id):
            self.obs.event(tenant_id=tenant_id, stage="junior.suggest.llm", actor="junior",
                           status="BUDGET_EXCEEDED",
                           meta={"daily_cap": self.llm_daily_cap})
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
            res = llm.generate(prompt=prompt, system_prompt=(
                "You are a senior product-analytics assistant. Be concrete and measurable."),
                temperature=0.3)
            lines = self._llm_lines(res.text, 2)
            for line in lines:
                suggestions.append({"target": "", "category": "llm", "priority": 0,
                                    "question": line, "columns": [], "source": "llm"})
            if lines:
                self._llm_cache_put(key, "\n".join(lines))
                self._llm_spend(tenant_id)
            self.obs.event(tenant_id=tenant_id, stage="junior.suggest.llm", actor="junior",
                           tokens_out=getattr(res, "tokens_out", 0), meta={"added": len(lines)})
        except Exception as e:  # noqa: BLE001 - LLM is an optional enhancement
            self.obs.event(tenant_id=tenant_id, stage="junior.suggest.llm", actor="junior",
                           status="FAILED", meta={"error": str(e)})

    def _enrich_hypotheses_llm(self, tenant_id: str, hyps: List[Dict[str, Any]],
                                profile: Optional[Any], depth: int) -> None:
        """Optional LLM-authored business hypotheses, additive + throttled (CP-13)."""
        cfg = self.tenants.get_analyst_config(tenant_id)
        if not cfg.junior.enabled:
            return
        llm = make_role_client(self.settings, cfg.junior)
        if not self._llm_live(llm) or depth < 2:
            return
        key = self._llm_cache_key(tenant_id, "hypotheses", depth)
        cached = self._llm_cache_get(key)
        if cached is not None:
            added = 0
            for line in self._llm_lines(cached, 2):
                hyps.append({"target": "", "category": "llm",
                             "hypothesis": line, "testable": True, "source": "llm"})
                added += 1
            self.obs.event(tenant_id=tenant_id, stage="junior.hypotheses.llm", actor="junior",
                           status="CACHED", meta={"depth": depth, "added": added})
            return
        if not self._llm_budget_ok(tenant_id):
            self.obs.event(tenant_id=tenant_id, stage="junior.hypotheses.llm", actor="junior",
                           status="BUDGET_EXCEEDED",
                           meta={"daily_cap": self.llm_daily_cap, "depth": depth})
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
            res = llm.generate(prompt=prompt, system_prompt=(
                "You are a senior product-analytics assistant. Hypotheses must be "
                "specific and falsifiable with the company's own data."),
                temperature=0.4)
            lines = self._llm_lines(res.text, 2)
            for line in lines:
                hyps.append({"target": "", "category": "llm",
                             "hypothesis": line, "testable": True, "source": "llm"})
            if lines:
                self._llm_cache_put(key, "\n".join(lines))
                self._llm_spend(tenant_id)
            self.obs.event(tenant_id=tenant_id, stage="junior.hypotheses.llm", actor="junior",
                           tokens_out=getattr(res, "tokens_out", 0),
                           meta={"depth": depth, "added": len(lines)})
        except Exception as e:  # noqa: BLE001 - LLM is an optional enhancement
            self.obs.event(tenant_id=tenant_id, stage="junior.hypotheses.llm", actor="junior",
                           status="FAILED", meta={"error": str(e)})


__all__ = ["JuniorEngine"]