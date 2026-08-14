"""Onboarding wizard service (plan section 6, Stage 0-2 scaffold).

Guides standing up a NEW company: tenant + company profile -> main tables ->
ingest legacy queries (as CANDIDATE) -> senior review -> readiness/stage review.
Reuses the governed Company Brain; nothing imported is auto-approved.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .brain.embedding import Embedder
from .brain.index import BrainIndex
from .brain.ingest import ingest_sql
from .brain.store import CompanyBrain
from .database import Store
from .domain import DataSource, DataSourceKind, KnowledgeNode, NodeKind, ReviewStatus, Tenant
from .observability import Observability
from .pipeline import Pipeline
from .stores import TenantStoreProvider
from .tenancy import TenantService


class OnboardingService:
    def __init__(self, stores: TenantStoreProvider, tenants: Optional[TenantService] = None,
                 pipeline: Optional[Pipeline] = None,
                 observability: Optional[Observability] = None,
                 embedder: Optional[Embedder] = None):
        self.stores = stores
        self.tenants = tenants or TenantService(stores)
        self.pipeline = pipeline
        self.obs = observability or Observability(stores)
        self.embedder = embedder

    def brain(self, tenant_id: str) -> CompanyBrain:
        store = self.stores.for_tenant(tenant_id)
        return CompanyBrain(store, tenant_id,
                            index=BrainIndex(store, embedder=self.embedder))

    # -- step 1 + 2: tenant + company profile --------------------------------
    def provision_company(self, profile: Dict[str, Any],
                          datasource: Optional[Dict[str, Any]] = None,
                          region: str = "global", purpose: str = "onboarding") -> Tenant:
        t = self.tenants.create_tenant(name=profile.get("name", "Unnamed company"),
                                       region=region, purpose=purpose,
                                       llm_provider=profile.get("llm_provider", "null"))
        self.tenants.set_company_profile(t.id, profile)
        self.obs.event(tenant_id=t.id, stage="onboarding.tenant_provisioned", actor="owner")
        if datasource:
            self.add_main_tables(t.id, datasource.get("tables", []),
                                 name=datasource.get("name", "primary"),
                                 kind=datasource.get("kind", "direct_db"),
                                 dialect=datasource.get("dialect", "athena"))
        return t

    # -- step 3+4: data source + main tables ---------------------------------
    def add_main_tables(self, tenant_id: str, tables: List[str], *,
                        name: str = "primary", kind: str = "direct_db",
                        dialect: str = "athena") -> DataSource:
        self.tenants.require_tenant(tenant_id)
        try:
            k = DataSourceKind(kind)
        except ValueError:
            k = DataSourceKind.DIRECT_DB
        ds = self.tenants.add_datasource(tenant_id, name, k, dialect, tables=tables,
                                         connected=True)
        self.obs.event(tenant_id=tenant_id, stage="onboarding.main_tables_mapped",
                       actor="admin", resource=ds.id, meta={"tables": tables})
        return ds

    # -- step 5: ingest legacy queries (as CANDIDATE nodes) -------------------
    def ingest_legacy(self, tenant_id: str, items: List[Dict[str, Any]],
                      by: str = "ingest") -> List[KnowledgeNode]:
        self.tenants.require_tenant(tenant_id)
        brain = self.brain(tenant_id)
        created: List[KnowledgeNode] = []
        for it in items:
            nodes = ingest_sql(brain, it["sql"], source_ref=it.get("source_ref", ""),
                               title=it.get("title"), created_by=by)
            created.extend(nodes)
        self.obs.event(tenant_id=tenant_id, stage="onboarding.legacy_ingested",
                       actor=by, resource=str(len(created)), meta={"count": len(created)})
        return created

    def bulk_ingest_json(self, tenant_id: str, json_content: str, llm: Any, by: str = "ingest") -> Dict[str, Any]:
        """Parses a JSON array of legacy queries, extracts AST, uses LLM for EDA, and saves nodes."""
        import json
        from .domain import KnowledgeNode
        
        self.tenants.require_tenant(tenant_id)
        brain = self.brain(tenant_id)
        
        try:
            items = json.loads(json_content)
            if not isinstance(items, list):
                return {"error": "Expected a JSON array of queries."}
        except Exception as e:
            return {"error": f"Invalid JSON: {e}"}
            
        created_nodes = []
        for it in items:
            sql = it.get("sql", "")
            if not sql:
                continue
                
            dashboard = it.get("dashboard", "")
            purpose = it.get("purpose", "")
            
            prompt = f"Analyze this SQL query from dashboard '{dashboard}' with purpose '{purpose}':\n\n{sql}\n\n"
            prompt += "Please return a JSON object with:\n"
            prompt += "- 'title': A short, clear title for the query.\n"
            prompt += "- 'description': A summary of what this query calculates.\n"
            prompt += "- 'definitions': A list of business definitions (e.g. 'Active User', 'Churn') found in the WHERE/GROUP BY clauses. Each should have a 'name' and 'description'.\n"
            
            try:
                res = llm.generate_json(prompt)
            except Exception:
                res = {}
                
            title = res.get("title", f"Query from {dashboard}" if dashboard else "Imported Query")
            desc = res.get("description", purpose)
            
            # Create QUERY node using AST parser
            nodes = ingest_sql(brain, sql, source_ref=dashboard, title=title, created_by=by)
            for node in nodes:
                node.description = desc
                node.payload["purpose"] = purpose
                brain.save(node)
                created_nodes.append(node)
                
            # Create DEFINITION nodes from LLM
            defs = res.get("definitions", [])
            for d in defs:
                d_name = d.get("name", "Unknown")
                d_desc = d.get("description", "")
                def_node = KnowledgeNode.definition(d_name, d_desc, created_by=by)
                def_node.source_ref = dashboard
                brain.save(def_node)
                created_nodes.append(def_node)
                
        self.obs.event(tenant_id=tenant_id, stage="onboarding.bulk_ingested",
                       actor=by, resource=str(len(created_nodes)), meta={"count": len(created_nodes)})
        return {"created_nodes_count": len(created_nodes)}

    # -- candidates + review --------------------------------------------------
    def candidates(self, tenant_id: str, status: ReviewStatus = ReviewStatus.CANDIDATE,
                   limit: int = 200) -> List[KnowledgeNode]:
        return self.brain(tenant_id).all(status=status, limit=limit)

    def review(self, tenant_id: str, approve_ids: Optional[List[str]] = None,
               reject_ids: Optional[List[str]] = None, by: str = "senior",
               notes: str = "") -> Dict[str, Any]:
        brain = self.brain(tenant_id)
        approved, rejected, skipped = [], [], []
        for nid in approve_ids or []:
            node = brain.get(nid)
            if node is None:
                skipped.append(nid)
            elif node.status in (ReviewStatus.CANDIDATE, ReviewStatus.UNDER_REVIEW,
                                 ReviewStatus.REVISION_REQUIRED):
                brain.submit(nid, by=by)
                brain.approve(nid, by=by, notes=notes or "approved during onboarding")
                approved.append(nid)
            else:
                skipped.append(nid)
        for nid in reject_ids or []:
            node = brain.get(nid)
            if node is None:
                skipped.append(nid)
            else:
                brain.reject(nid, by=by, notes=notes or "rejected during onboarding")
                rejected.append(nid)
        self.obs.event(tenant_id=tenant_id, stage="onboarding.review",
                       actor=by, meta={"approved": len(approved), "rejected": len(rejected)})
        return {"approved": approved, "rejected": rejected, "skipped": skipped}

    # -- step 6-8: readiness / maturity ---------------------------------------
    def readiness(self, tenant_id: str) -> Dict[str, Any]:
        """Score the company brain against onboarding readiness criteria and return
        a coarse stage (0..3) the senior analyst uses to gate the junior analyst."""
        self.tenants.require_tenant(tenant_id)
        brain = self.brain(tenant_id)
        profile = self.tenants.get_company_profile(tenant_id)
        sources = self.tenants.list_datasources(tenant_id)
        tables = sorted({t for s in sources for t in s.get("tables", [])})
        all_nodes = brain.all(limit=1000)
        approved = [n for n in all_nodes if n.status.is_usable()]
        by_kind = _kind_counts(approved)
        criteria = {
            "company_profile": profile is not None,
            "main_tables_mapped": len(tables) > 0,
            "defined_metrics": by_kind.get("DEFINITION", 0) > 0 or by_kind.get("METRIC", 0) > 0,
            "approved_queries": by_kind.get("QUERY", 0) > 0,
            "approved_findings": by_kind.get("FINDING", 0) > 0,
        }
        score = sum(1 for v in criteria.values() if v)
        # stage: 0 provisioned, 1 tables, 2 metrics/queries, 3 findings/research-ready
        if score >= 5:
            stage = 3
        elif score >= 3:
            stage = 2
        elif score >= 2:
            stage = 1
        else:
            stage = 0
        return {
            "tenant_id": tenant_id,
            "stage": stage,
            "criteria": criteria,
            "tables": tables,
            "node_counts": _kind_counts(all_nodes),
            "approved_counts": _kind_counts(approved),
            "conflicts": len(brain.conflicts()),
        }

    def maturity_label(self, stage: int) -> str:
        return ["provisioning", "data_discovery", "metric_understanding",
                "process_analysis"][min(stage, 3)]

    def digest(self, tenant_id: str) -> Dict[str, Any]:
        """Concise company + brain digest for a senior readiness review."""
        self.tenants.require_tenant(tenant_id)
        t = self.tenants.get_tenant(tenant_id)
        p = self.tenants.get_company_profile(tenant_id)
        r = self.readiness(tenant_id)
        return {
            "tenant": t.to_dict() if t else None,
            "profile": p.to_dict() if p else None,
            "readiness": r,
        }


def _kind_counts(nodes: List[KnowledgeNode]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for n in nodes:
        out[n.kind.value] = out.get(n.kind.value, 0) + 1
    return out