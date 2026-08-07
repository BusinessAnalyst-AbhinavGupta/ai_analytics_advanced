"""FastAPI surface for the standalone platform.

Endpoints mirror the plan's API groups (/tenants, /company-profile, /datasources,
/questions, /analyses, /knowledge, /reviews, /metrics) as a modular-monolith API.
Components stay in-process but every call emits telemetry, so the /metrics
endpoint shows the pipeline as if each hop were a monitored API.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .analysis import evaluate_rules, profile_df
from .brain.store import CompanyBrain
from .config import Settings
from .database import Store
from .domain import (AnswerMode, DataSourceKind, KnowledgeNode, NodeKind, ReviewStatus, RunStatus)
from .execution.sampler import SamplerExecutor
from .observability import Observability
from .pipeline import Pipeline
from .tenancy import TenantService

# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class NewTenant(BaseModel):
    name: str
    region: str = "global"
    llm_provider: str = "null"
    purpose: str = ""
    retention_days: int = 90


class CompanyProfileIn(BaseModel):
    name: str = ""
    industry: str = ""
    region: str = ""
    description: str = ""
    customers: str = ""
    product: str = ""
    value_creation: str = ""
    revenue_model: str = ""
    targets: List[Dict[str, Any]] = []
    constraints: List[str] = []
    risks: List[str] = []
    competitors: List[str] = []
    preferred_metrics: List[str] = []


class DataSourceIn(BaseModel):
    name: str
    kind: str = "direct_db"
    dialect: str = "ANSI"
    tables: List[str] = []
    connected: bool = True


class QuestionIn(BaseModel):
    question: str
    mode_budget: str = "low_cost"
    sql: Optional[str] = None   # optional persisted/generated SQL


class ReviewIn(BaseModel):
    action: str = "approve"     # approve | approve_with_caveats | reject | revise | submit | stale
    by: str = "senior"
    notes: str = ""


class PromoteIn(BaseModel):
    run_id: str
    by: str = "senior"
    notes: str = ""


class IngestSQL(BaseModel):
    sql: str
    source_ref: str = ""
    title: Optional[str] = None


# --------------------------------------------------------------------------- #
# Application context
# --------------------------------------------------------------------------- #
@dataclass
class AppContext:
    settings: Settings
    store: Store
    tenants: TenantService
    observability: Observability
    pipeline: Pipeline
    executor: SamplerExecutor


def make_context(settings: Optional[Settings] = None,
                 warehouse: Optional[Dict[str, Any]] = None) -> AppContext:
    settings = settings or Settings.from_env()
    store = Store(settings.resolve_db_path())
    tenants = TenantService(store)
    obs = Observability(store)
    executor = SamplerExecutor(warehouse or {})
    pipeline = Pipeline(store, settings=settings, tenant_service=tenants,
                        executor=executor, observability=obs)
    return AppContext(settings=settings, store=store, tenants=tenants,
                      observability=obs, pipeline=pipeline, executor=executor)


def bootstrap_demo(ctx: AppContext) -> str:
    """Create a demo tenant with profile + datasource + golden queries."""
    from .fixtures import GOLDEN_QUERIES, build_retail_warehouse, make_company_doc
    if ctx.executor and not ctx.executor._warehouse:
        ctx.executor.register_warehouse(build_retail_warehouse())
    t = ctx.tenants.create_tenant(name="Acme Retail GmbH", region="DE", purpose="demo",
                                  llm_provider="null")
    ctx.tenants.set_company_profile(t.id, make_company_doc(t.name))
    ctx.tenants.add_datasource(t.id, "Events warehouse", DataSourceKind.DIRECT_DB,
                               dialect="ANSI", tables=["events"], connected=True)
    for g in GOLDEN_QUERIES:
        ctx.pipeline.register_approved_query(t.id, g["sql"], g["title"], g["summary"],
                                             by="admin", source_ref="onboarding")
    return t.id
# <<PAPI2>>
# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
def create_app(ctx: Optional[AppContext] = None) -> FastAPI:
    ctx = ctx or make_context()
    app = FastAPI(title="AI Analytics Platform", version="0.1.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])
    app.state.ctx = ctx
    C = ctx  # closure shorthand

    def tenant_or_404(tenant_id: str) -> str:
        try:
            C.tenants.require_tenant(tenant_id)
        except KeyError:
            raise HTTPException(404, f"Unknown tenant {tenant_id}")
        return tenant_id

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {"status": "ok", "app": C.settings.app_name, "db": C.store.db_path}

    # -- tenants -----------------------------------------------------------
    @app.post("/tenants")
    def create_tenant(body: NewTenant) -> Dict[str, Any]:
        t = C.tenants.create_tenant(body.name, region=body.region,
                                    llm_provider=body.llm_provider, purpose=body.purpose,
                                    retention_days=body.retention_days)
        C.observability.event(tenant_id=t.id, stage="tenant.created", actor="admin", resource=t.id)
        return {"tenant_id": t.id, "status": "created"}

    @app.get("/tenants")
    def list_tenants() -> List[Dict[str, Any]]:
        return C.tenants.list_tenants()

    @app.get("/tenants/{tenant_id}")
    def get_tenant(tenant_id: str) -> Dict[str, Any]:
        t = C.tenants.get_tenant(tenant_or_404(tenant_id))
        p = C.tenants.get_company_profile(tenant_id)
        return {"tenant": t.to_dict(), "profile": p.to_dict() if p else None}

    @app.put("/tenants/{tenant_id}/company-profile")
    def set_profile(tenant_id: str, body: CompanyProfileIn) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        p = C.tenants.set_company_profile(tenant_id, body.model_dump())
        C.observability.event(tenant_id=tenant_id, stage="company_profile.updated", actor="owner")
        return {"tenant_id": tenant_id, "profile": p.to_dict()}

    @app.post("/tenants/{tenant_id}/datasources")
    def add_datasource(tenant_id: str, body: DataSourceIn) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        try:
            kind = DataSourceKind(body.kind)
        except ValueError:
            raise HTTPException(400, f"Invalid kind {body.kind}")
        ds = C.tenants.add_datasource(tenant_id, body.name, kind, body.dialect,
                                      tables=body.tables, connected=body.connected)
        C.observability.event(tenant_id=tenant_id, stage="connector.connected", actor="admin",
                              resource=ds.id)
        return {"datasource_id": ds.id}

    @app.get("/tenants/{tenant_id}/datasources")
    def list_datasources(tenant_id: str) -> List[Dict[str, Any]]:
        tenant_or_404(tenant_id)
        return C.tenants.list_datasources(tenant_id)
# <<PAPI3>>
    # -- questions / analyses ----------------------------------------------
    @app.post("/tenants/{tenant_id}/questions")
    def ask(tenant_id: str, body: QuestionIn) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        run = C.pipeline.run(tenant_id, body.question, mode_budget=body.mode_budget,
                             persisted_sql=body.sql)
        return run.to_dict()

    @app.get("/analyses/{tenant_id}/{run_id}")
    def get_analysis(tenant_id: str, run_id: str) -> Dict[str, Any]:
        run = C.pipeline.get_run(tenant_or_404(tenant_id), run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        return run.to_dict()

    @app.get("/tenants/{tenant_id}/analyses")
    def list_analyses(tenant_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        tenant_or_404(tenant_id)
        return [r.to_dict() for r in C.pipeline.list_runs(tenant_id, limit)]

    # -- review / promotion / ingest ---------------------------------------
    @app.post("/tenants/{tenant_id}/knowledge/ingest")
    def ingest_sql(tenant_id: str, body: IngestSQL) -> Dict[str, Any]:
        from .brain.ingest import ingest_sql as _ingest
        tenant_or_404(tenant_id)
        brain = C.pipeline.brain(tenant_id)
        nodes = _ingest(brain, body.sql, source_ref=body.source_ref, title=body.title)
        return {"created": [n.to_dict() for n in nodes]}

    @app.get("/tenants/{tenant_id}/knowledge")
    def search_knowledge(tenant_id: str, q: str = "", kind: Optional[str] = None,
                         usable_only: bool = True, limit: int = 20) -> List[Dict[str, Any]]:
        tenant_or_404(tenant_id)
        brain = C.pipeline.brain(tenant_id)
        k = NodeKind(kind) if kind else None
        return [n.to_dict() for n in brain.search(q, kind=k, usable_only=usable_only, limit=limit)]

    @app.post("/knowledge/{tenant_id}/{node_id}/review")
    def review(tenant_id: str, node_id: str, body: ReviewIn) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        brain = C.pipeline.brain(tenant_id)
        actions = {"approve": brain.approve, "approve_with_caveats": brain.approve_with_caveats,
                   "reject": brain.reject, "revise": brain.revise,
                   "submit": brain.submit, "stale": brain.mark_stale}
        fn = actions.get(body.action)
        if fn is None:
            raise HTTPException(400, f"Unknown action {body.action}")
        node = fn(node_id, by=body.by, notes=body.notes)
        C.observability.event(tenant_id=tenant_id, stage=f"knowledge.{body.action}",
                              actor=body.by, resource=node_id, status="OK")
        return node.to_dict()

    @app.post("/tenants/{tenant_id}/promote")
    def promote(tenant_id: str, body: PromoteIn) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        node = C.pipeline.promote_finding(tenant_id, body.run_id, by=body.by, notes=body.notes)
        if node is None:
            raise HTTPException(400, "Cannot promote: run not completed or not found")
        return node.to_dict()

    @app.post("/tenants/{tenant_id}/approve-query")
    def approve_query(tenant_id: str, body: IngestSQL) -> Dict[str, Any]:
        from .brain.ingest import extract
        tenant_or_404(tenant_id)
        node = C.pipeline.register_approved_query(
            tenant_id, body.sql, body.title or "Approved query",
            summary=f"Tables: {', '.join(extract(body.sql).get('tables', []))}",
            by="admin", source_ref=body.source_ref)
        return node.to_dict()

    # -- knowledge state / metrics ------------------------------------------
    @app.get("/tenants/{tenant_id}/brain")
    def brain_state(tenant_id: str) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        brain = C.pipeline.brain(tenant_id)
        return {"stats": brain.stats(), "conflicts": brain.conflicts()}

    @app.get("/tenants/{tenant_id}/metrics")
    def tenant_metrics(tenant_id: str) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        brain = C.pipeline.brain(tenant_id)
        return {"telemetry": C.observability.metrics(tenant_id), "brain": brain.stats()}

    @app.get("/metrics")
    def platform_metrics() -> Dict[str, Any]:
        return C.observability.metrics()

    return app


def main(port: int = 8000) -> None:
    import uvicorn
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()