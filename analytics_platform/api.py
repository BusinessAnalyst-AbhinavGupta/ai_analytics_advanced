"""FastAPI surface for the standalone platform.

Endpoints mirror the plan's API groups (/tenants, /company-profile, /datasources,
/questions, /analyses, /knowledge, /reviews, /metrics) plus /triage (senior-review
inbox) and /junior (maturity + schema EDA + goal-aligned questions) as a
modular-monolith API. Components stay in-process but every call emits telemetry,
so the /metrics endpoint shows the pipeline as if each hop were a monitored API.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .analysis import evaluate_rules, profile_df
from .auth import AuthGate, Role, issue
from .billing import BillingService
from .brain.store import CompanyBrain
from .config import Settings
from .database import Store
from .domain import (AnswerMode, DataSourceKind, KnowledgeNode, NodeKind, ReviewStatus, RunStatus)
from .execution.sampler import SamplerExecutor
from .junior import JuniorEngine
from .llm.client import list_provider_models, make_client_from
from .observability import Observability
from .onboarding import OnboardingService
from .pipeline import Pipeline
from .junior_worker import JuniorWorker
from .research import ResearchService
from .retention import RetentionService
from .scheduler import Scheduler
from .senior import SeniorService
from .stakeholder import StakeholderService
from .tenancy import TenantService
from .triage import TriageService

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
    changed_by: Optional[str] = None


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


class JuniorRunIn(BaseModel):
    force: bool = False         # test/operator lever: bypass window+rate (serial+disable hold)


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


class LegacyItem(BaseModel):
    sql: str
    source_ref: str = ""
    title: Optional[str] = None


class OnboardCompanyIn(BaseModel):
    profile: Dict[str, Any]
    datasource: Optional[Dict[str, Any]] = None
    region: str = "global"
    purpose: str = "onboarding"


class MainTablesIn(BaseModel):
    tables: List[str]
    name: str = "primary"
    kind: str = "direct_db"
    dialect: str = "athena"


class LegacyIn(BaseModel):
    items: List[LegacyItem]
    by: str = "ingest"


class ReviewBatchIn(BaseModel):
    approve_ids: List[str] = []
    reject_ids: List[str] = []
    by: str = "senior"
    notes: str = ""


class AnalystToggleIn(BaseModel):
    enabled: Optional[bool] = None
    provider: str = ""
    model: str = ""


class AnalystConfigIn(BaseModel):
    """Config panel: per-analyst AI toggles + model selection. API keys are
    intentionally *not* accepted here — injected at runtime from env."""
    junior: Optional[AnalystToggleIn] = None
    senior: Optional[AnalystToggleIn] = None
    stakeholder: Optional[AnalystToggleIn] = None
    junior_depth: Optional[int] = None      # 0=basic | 1=standard | 2=advanced
    human_signoff_days: Optional[int] = None
    changed_by: Optional[str] = "owner"


class JuniorDepthIn(BaseModel):
    action: str = "up"                      # up | down | set
    level: Optional[int] = None             # used when action == set
    by: str = "human"


class SeniorReviewIn(BaseModel):
    run_id: str
    action: str = "approve"   # approve | reject | revise
    by: str = "human"         # the human playing senior (or an AI identity)
    notes: str = ""


class TriageIdsIn(BaseModel):
    ids: List[str]
    by: str = "senior"
    notes: str = ""


class TriageBulkIn(BaseModel):
    kind: Optional[str] = None
    action: str = "approve"     # approve | reject
    by: str = "senior"
    notes: str = ""
    limit: int = 500


class TriageDedupeIn(BaseModel):
    keep: str
    drop: List[str]
    by: str = "senior"
    notes: str = ""


class StakeholderIn(BaseModel):
    question: str
    user_id: str = ""


class FeedbackIn(BaseModel):
    answer_id: str
    user_id: str = ""
    rating: str = "up"          # up | down
    comment: str = ""


class ResearchBatchIn(BaseModel):
    query: str = ""
    results: List[Dict[str, Any]] = []   # what an approved provider returned


class PromoteIn2(BaseModel):
    doc_id: str
    by: str = "analyst"
    note: str = ""


class SourcePolicyIn(BaseModel):
    policy: str                 # allow | block


class LoginIn(BaseModel):
    tenant_id: str = ""
    role: str = "stakeholder"
    sub: str = ""


class PurgeIn(BaseModel):
    tenant_id: Optional[str] = None
    dry_run: bool = True


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
    onboarding: OnboardingService
    stakeholder: Optional[StakeholderService] = None
    research: Optional[ResearchService] = None
    auth: Optional[AuthGate] = None
    billing: Optional[BillingService] = None
    retention: Optional[RetentionService] = None
    scheduler: Optional[Any] = None
    junior_worker: Optional[Any] = None
    junior: Optional[Any] = None
    senior: Optional[Any] = None


def make_context(settings: Optional[Settings] = None,
                 warehouse: Optional[Dict[str, Any]] = None) -> AppContext:
    settings = settings or Settings.from_env()
    store = Store(settings.resolve_db_path())
    tenants = TenantService(store)
    obs = Observability(store)
    executor = SamplerExecutor(warehouse or {})
    pipeline = Pipeline(store, settings=settings, tenant_service=tenants,
                        executor=executor, observability=obs)
    onboarding = OnboardingService(store, tenants=tenants, pipeline=pipeline,
                                   observability=obs)
    stakeholder = StakeholderService(store, tenants=tenants, executor=executor,
                                     observability=obs,
                                     llm=make_client_from(settings),
                                     cost_per_1k_input=settings.cost_per_1k_input,
                                     cost_per_1k_output=settings.cost_per_1k_output)
    research = ResearchService(store, observability=obs)
    auth = AuthGate(settings)
    billing = BillingService(store, settings=settings, observability=obs)
    retention = RetentionService(store, tenants=tenants, observability=obs)
    junior = JuniorEngine(store, executor=executor, tenants=tenants,
                          observability=obs, llm=make_client_from(settings))
    junior_worker = _make_junior_worker(settings, store, junior, obs)
    scheduler = Scheduler(store, observability=obs,
                          retention_days=settings.log_retention_days,
                          maintenance_interval_days=settings.maintenance_interval_days,
                          junior_worker=junior_worker)
    senior = SeniorService(store, pipeline, tenants, observability=obs)
    return AppContext(settings=settings, store=store, tenants=tenants,
                      observability=obs, pipeline=pipeline, executor=executor,
                      onboarding=onboarding, stakeholder=stakeholder,
                      research=research, auth=auth, billing=billing,
                      retention=retention, scheduler=scheduler,
                      junior_worker=junior_worker, junior=junior, senior=senior)


def _make_junior_worker(settings: Settings, store: Store, junior: Any,
                        obs: Observability) -> Optional[JuniorWorker]:
    """Build a background youth worker bound to a tenant.

    The worker needs a concrete tenant. We pick the *oldest* active tenant as the
    default background target; if none exists it returns None (no-op) so a fresh
    DB never spins a worker with nowhere to go.
    """
    try:
        tenants = TenantService(store).list_tenants()
        if not tenants:
            return None
        target = sorted(tenants, key=lambda t: t.get("created_at", ""))[0]
        return JuniorWorker(
            store, junior, tenant_id=target["id"],
            work_start=settings.junior_work_start,
            work_end=settings.junior_work_end,
            min_interval_minutes=settings.junior_min_interval_minutes,
            daily_cap=settings.junior_daily_cap,
            observability=obs, default_tenant=target["id"])
    except Exception:
        return None


def ensure_services(ctx: AppContext) -> AppContext:
    """Backfill the P6-P8 services when an AppContext was built manually
    (e.g. by test helpers) with only the core P1-P3 fields."""
    if ctx.stakeholder is None:
        ctx.stakeholder = StakeholderService(
            ctx.store, tenants=ctx.tenants, executor=ctx.executor,
            observability=ctx.observability, llm=make_client_from(ctx.settings),
            cost_per_1k_input=ctx.settings.cost_per_1k_input,
            cost_per_1k_output=ctx.settings.cost_per_1k_output)
    if ctx.research is None:
        ctx.research = ResearchService(ctx.store, observability=ctx.observability)
    if ctx.auth is None:
        ctx.auth = AuthGate(ctx.settings)
    if ctx.billing is None:
        ctx.billing = BillingService(ctx.store, settings=ctx.settings,
                                     observability=ctx.observability)
    if ctx.retention is None:
        ctx.retention = RetentionService(ctx.store, tenants=ctx.tenants,
                                         observability=ctx.observability)
    if ctx.junior is None:
        from .junior import JuniorEngine
        ctx.junior = JuniorEngine(ctx.store, executor=ctx.executor,
                                  tenants=ctx.tenants,
                                  observability=ctx.observability,
                                  llm=make_client_from(ctx.settings))
    if ctx.junior_worker is None:
        ctx.junior_worker = _make_junior_worker(ctx.settings, ctx.store,
                                                ctx.junior, ctx.observability)
    if ctx.scheduler is None:
        ctx.scheduler = Scheduler(
            ctx.store, observability=ctx.observability,
            retention_days=ctx.settings.log_retention_days,
            maintenance_interval_days=ctx.settings.maintenance_interval_days,
            junior_worker=ctx.junior_worker)
    if ctx.senior is None:
        ctx.senior = SeniorService(ctx.store, ctx.pipeline, ctx.tenants,
                                   observability=ctx.observability)
    return ctx


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
# Helpers (thin, testable glue for the endpoints)
# --------------------------------------------------------------------------- #
def _coerce_kind(kind: Optional[str]) -> Optional[NodeKind]:
    if not kind:
        return None
    try:
        return NodeKind(kind.upper())
    except ValueError:
        raise HTTPException(400, f"Invalid kind {kind}")


def _api_junior_executor(settings: Settings, offline: Any) -> Any:
    """Executor for the junior endpoints: live BrowserSessionExecutor when the
    live gate (ANALYTICS_MB_LIVE=1) is set, else the offline SamplerExecutor.
    Keeps the browser-cookie path out of the default API surface."""
    if settings.metabase_live:
        from .execution.browser_session import make_live_executor
        return make_live_executor(settings=settings)
    return offline


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
def create_app(ctx: Optional[AppContext] = None) -> FastAPI:
    ctx = ctx or make_context()
    app = FastAPI(title="AI Analytics Platform", version="0.1.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])
    app.state.ctx = ctx
    ctx = ensure_services(ctx)
    C = ctx  # closure shorthand

    @app.middleware("http")
    async def _access_log_middleware(request: Request, call_next):
        import time as _t
        from starlette.requests import ClientDisconnect
        t0 = _t.perf_counter()
        tenant_id = ""
        # best-effort tenant from path (read-only; never derives secrets)
        parts = request.url.path.split("/")
        if "tenants" in parts:
            i = parts.index("tenants") + 1
            if i < len(parts) and not parts[i].startswith("{"):
                tenant_id = parts[i]
        try:
            response = await call_next(request)
        except ClientDisconnect:
            response = None
        except Exception:
            import traceback as _tb
            traceback_text = _tb.format_exc()[:300]
            C.observability.log_access(method=request.method, path=request.url.path,
                                       status=500, duration_ms=(_t.perf_counter() - t0) * 1000.0,
                                       tenant_id=tenant_id, meta={"err": traceback_text})
            raise
        ms = (_t.perf_counter() - t0) * 1000.0
        status = getattr(response, "status_code", 0) if response is not None else 0
        C.observability.log_access(method=request.method, path=request.url.path,
                                   status=status, duration_ms=ms, tenant_id=tenant_id)
        if response is None:
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse("", status_code=444)
        return response

    def tenant_or_404(tenant_id: str) -> str:
        try:
            C.tenants.require_tenant(tenant_id)
        except KeyError:
            raise HTTPException(404, f"Unknown tenant {tenant_id}")
        return tenant_id

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {"status": "ok", "app": C.settings.app_name, "db": C.store.db_path}

    @app.get("/llm/models")
    def llm_models(provider: str = "", key: str = "") -> Dict[str, Any]:
        """Config panel: live-ping a provider for model options. The key is used
        only for this call (never persisted). Ollama uses the configured base URL."""
        models = list_provider_models(
            provider=provider, api_key=key or C.settings.effective_api_key(),
            ollama_base_url=C.settings.ollama_base_url)
        return {"provider": provider or "null", "count": len(models), "models": models}

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
        payload = body.model_dump()
        changed_by = payload.pop("changed_by", None) or "owner"
        p = C.tenants.set_company_profile(tenant_id, payload, changed_by=changed_by)
        C.observability.event(tenant_id=tenant_id, stage="company_profile.updated",
                              actor=changed_by)
        return {"tenant_id": tenant_id, "profile": p.to_dict()}

    @app.get("/tenants/{tenant_id}/company-profile/history")
    def profile_history(tenant_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Config panel: versioned history of business context changes over time."""
        tenant_or_404(tenant_id)
        return C.tenants.get_company_profile_history(tenant_id, limit=limit)

    # -- analyst AI config panel (toggles, per-role model, depth, history) ------
    @app.get("/tenants/{tenant_id}/analyst-config")
    def get_analyst_config_route(tenant_id: str) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        cfg = C.tenants.get_analyst_config(tenant_id)
        return cfg.to_dict()

    @app.put("/tenants/{tenant_id}/analyst-config")
    def put_analyst_config(tenant_id: str, body: AnalystConfigIn) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        payload = body.model_dump()
        changed_by = payload.pop("changed_by", None) or "owner"
        cfg = C.tenants.set_analyst_config(tenant_id, payload, changed_by=changed_by)
        C.observability.event(tenant_id=tenant_id, stage="analyst_config.updated",
                              actor=changed_by,
                              meta={"junior_depth": cfg.junior_depth,
                                    "junior_enabled": cfg.junior.enabled,
                                    "senior_enabled": cfg.senior.enabled,
                                    "stakeholder_enabled": cfg.stakeholder.enabled})
        return cfg.to_dict()

    @app.get("/tenants/{tenant_id}/analyst-config/history")
    def analyst_config_history(tenant_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Config panel: versioned log of analyst-config changes (latest state too)."""
        tenant_or_404(tenant_id)
        return C.tenants.get_analyst_config_history(tenant_id, limit=limit)

    @app.post("/tenants/{tenant_id}/senior/junior-depth")
    def senior_junior_depth(tenant_id: str, body: JuniorDepthIn) -> Dict[str, Any]:
        """Human promote/downgrade of the junior question depth on the senior tab."""
        tenant_or_404(tenant_id)
        return C.senior.set_junior_depth(tenant_id, action=body.action,
                                         level=body.level, by=body.by or "human")

    @app.get("/tenants/{tenant_id}/senior/status")
    def senior_status(tenant_id: str) -> Dict[str, Any]:
        """Where the senior workload sits (AI vs human) + junior depth (R4/R7)."""
        tenant_or_404(tenant_id)
        return C.senior.status(tenant_id)

    @app.get("/senior/{tenant_id}/queue")
    def senior_queue(tenant_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Senior review inbox over completed analyst runs (R2/R3)."""
        tenant_or_404(tenant_id)
        return C.senior.queue(tenant_id, limit=limit)

    @app.post("/senior/{tenant_id}/review")
    def senior_review(tenant_id: str, body: SeniorReviewIn) -> Dict[str, Any]:
        """Senior decision (approve/reject/revise) on an analysis; enforces the
        human-signoff window + disabled-senior-AI gates (R2/R6)."""
        tenant_or_404(tenant_id)
        return C.senior.review(tenant_id, body.run_id, action=body.action,
                               by=body.by or "human", notes=body.notes or "")

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

    @app.get("/analyses/{tenant_id}/{run_id}/md")
    def get_analysis_md(tenant_id: str, run_id: str) -> Dict[str, Any]:
        """Each analysis rendered as markdown for human review (R3)."""
        tenant_or_404(tenant_id)
        return C.senior.analysis_md(tenant_id, run_id)

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

    # -- Phase 9: owner-facing observability --------------------------------
    @app.get("/observability/logs")
    def observability_logs(tenant_id: str = "", limit: int = 200) -> Dict[str, Any]:
        return {"logs": C.observability.logs(tenant_id=tenant_id, limit=limit)}

    @app.get("/observability/status")
    def observability_status() -> Dict[str, Any]:
        s = C.scheduler
        worker = C.junior_worker
        status = {
            "retention_days": C.settings.log_retention_days,
            "maintenance_interval_days": C.settings.maintenance_interval_days,
            "scheduler_enabled": C.settings.scheduler_enabled,
            "purge": {"last_purge_ts": s.last_purge_ts() if s else None,
                       "next_due_ts": None},
            "junior": None,
        }
        if s and s.last_purge_ts():
            status["purge"]["next_due_ts"] = (
                s.last_purge_ts() + C.settings.maintenance_interval_days * 86400.0)
        if worker is not None:
            due = worker.due()
            status["junior"] = {
                "tenant_id": worker.tenant_id,
                "work_window": f"{C.settings.junior_work_start}-{C.settings.junior_work_end}",
                "min_interval_minutes": C.settings.junior_min_interval_minutes,
                "in_window": due["in_window"], "rate_ok": due["rate_ok"],
                "last_cycle_ts": s.junior_last_ts() if s else None,
            }
        return status

    @app.post("/observability/purge")
    def observability_purge() -> Dict[str, Any]:
        result = C.scheduler.purge_logs_once()
        return {"status": "ok", **result}

    @app.post("/observability/junior/run")
    def observability_junior_run(tenant_id: str = "") -> Dict[str, Any]:
        """Manually trigger one serial junior cycle (still honours window/rate)."""
        worker = C.junior_worker
        if worker is None:
            raise HTTPException(400, "no background junior worker configured")
        tid = tenant_id or worker.tenant_id
        return {"status": "ok", **worker.run_cycle(tid)}

    # -- onboarding wizard -----------------------------------------------
    @app.post("/onboarding")
    def onboard_company(body: OnboardCompanyIn) -> Dict[str, Any]:
        t = C.onboarding.provision_company(body.profile, datasource=body.datasource,
                                           region=body.region, purpose=body.purpose)
        return {"tenant_id": t.id, "stage": "provisioned",
                "readiness": C.onboarding.readiness(t.id)}

    @app.post("/onboarding/{tenant_id}/main-tables")
    def onboard_main_tables(tenant_id: str, body: MainTablesIn) -> Dict[str, Any]:
        ds = C.onboarding.add_main_tables(tenant_id, body.tables, name=body.name,
                                          kind=body.kind, dialect=body.dialect)
        return {"datasource_id": ds.id, "tables": body.tables,
                "readiness": C.onboarding.readiness(tenant_id)}

    @app.post("/onboarding/{tenant_id}/legacy")
    def onboard_legacy(tenant_id: str, body: LegacyIn) -> Dict[str, Any]:
        items = [i.model_dump() for i in body.items]
        nodes = C.onboarding.ingest_legacy(tenant_id, items, by=body.by)
        return {"created": [n.to_dict() for n in nodes],
                "candidate_ids": [n.id for n in nodes]}

    @app.get("/onboarding/{tenant_id}/candidates")
    def onboard_candidates(tenant_id: str) -> List[Dict[str, Any]]:
        return [n.to_dict() for n in C.onboarding.candidates(tenant_id)]

    @app.post("/onboarding/{tenant_id}/review")
    def onboard_review(tenant_id: str, body: ReviewBatchIn) -> Dict[str, Any]:
        return C.onboarding.review(tenant_id, approve_ids=body.approve_ids,
                                   reject_ids=body.reject_ids, by=body.by, notes=body.notes)

    @app.get("/onboarding/{tenant_id}/readiness")
    def onboard_readiness(tenant_id: str) -> Dict[str, Any]:
        return C.onboarding.readiness(tenant_id)

    @app.get("/onboarding/{tenant_id}/digest")
    def onboard_digest(tenant_id: str) -> Dict[str, Any]:
        return C.onboarding.digest(tenant_id)

    # -- triage (senior-review inbox over the Brain) ----------------------
    def _triage(tenant_id: str) -> TriageService:
        tenant_or_404(tenant_id)
        return TriageService(ctx.store, ctx.observability)

    @app.get("/triage/{tenant_id}/summary")
    def triage_summary(tenant_id: str) -> Dict[str, Any]:
        return _triage(tenant_id).summary(tenant_id)

    @app.get("/triage/{tenant_id}/queue")
    def triage_queue(tenant_id: str, kind: Optional[str] = None, search: str = "",
                     limit: int = 100) -> List[Dict[str, Any]]:
        svc = _triage(tenant_id)
        return [n.to_dict() for n in
                svc.queue(tenant_id, kind=_coerce_kind(kind), search=search, limit=limit)]

    @app.get("/triage/{tenant_id}/conflicts")
    def triage_conflicts(tenant_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        return _triage(tenant_id).conflicts(tenant_id)[:limit]

    @app.post("/triage/{tenant_id}/approve")
    def triage_approve(tenant_id: str, body: TriageIdsIn) -> Dict[str, Any]:
        return _triage(tenant_id).approve(tenant_id, body.ids, by=body.by, notes=body.notes)

    @app.post("/triage/{tenant_id}/reject")
    def triage_reject(tenant_id: str, body: TriageIdsIn) -> Dict[str, Any]:
        return _triage(tenant_id).reject(tenant_id, body.ids, by=body.by, notes=body.notes)

    @app.post("/triage/{tenant_id}/bulk")
    def triage_bulk(tenant_id: str, body: TriageBulkIn) -> Dict[str, Any]:
        return _triage(tenant_id).bulk(tenant_id, kind=_coerce_kind(body.kind),
                                       action=body.action, by=body.by, notes=body.notes,
                                       limit=body.limit)

    @app.post("/triage/{tenant_id}/dedupe")
    def triage_dedupe(tenant_id: str, body: TriageDedupeIn) -> Dict[str, Any]:
        return _triage(tenant_id).dedupe(tenant_id, body.keep, body.drop,
                                         by=body.by, notes=body.notes)

    # -- junior (maturity + schema EDA + goal-aligned questions) -----------
    def _junior(tenant_id: str) -> JuniorEngine:
        tenant_or_404(tenant_id)
        return JuniorEngine(ctx.store, executor=_api_junior_executor(ctx.settings, ctx.executor),
                            tenants=ctx.tenants, observability=ctx.observability,
                            llm=make_client_from(ctx.settings))

    @app.get("/junior/{tenant_id}/stage")
    def junior_stage(tenant_id: str, limit: int = 200) -> Dict[str, Any]:
        return _junior(tenant_id).stage(tenant_id, limit=limit)

    @app.get("/junior/{tenant_id}/catalog")
    def junior_catalog(tenant_id: str) -> Dict[str, Any]:
        return _junior(tenant_id).catalog(tenant_id)

    @app.get("/junior/{tenant_id}/datasets")
    def junior_datasets(tenant_id: str) -> List[str]:
        return _junior(tenant_id).datasets(tenant_id)

    @app.get("/junior/{tenant_id}/questions")
    def junior_questions(tenant_id: str, limit_per_target: int = 2) -> Dict[str, Any]:
        return _junior(tenant_id).suggest_questions(tenant_id, limit_per_target=limit_per_target)

    @app.get("/junior/{tenant_id}/hypotheses")
    def junior_hypotheses(tenant_id: str, limit: int = 4) -> Dict[str, Any]:
        """Business hypotheses, scaled by junior depth (R4)."""
        return _junior(tenant_id).suggest_hypotheses(tenant_id, limit=limit)

    @app.get("/junior/{tenant_id}/reproduce")
    def junior_reproduce(tenant_id: str, limit: int = 200) -> Dict[str, Any]:
        return _junior(tenant_id).reproduce_metrics(tenant_id, limit=limit)

    @app.post("/tenants/{tenant_id}/junior/run")
    def junior_run(tenant_id: str, body: JuniorRunIn) -> Dict[str, Any]:
        """On-demand junior run for a controlled test drive (CP-12).

        Drives one self-picked question through the engine with the *live*
        read-only executor when `ANALYTICS_MB_LIVE=1`, analyses the result (insight
        / actionables / assumptions), writes the reviewable `.md`, and leaves the
        run in the human senior inbox (`CANDIDATE`). `force=True` bypasses only the
        clock window + 1/hr rate; serial single-flight and the disable gate hold.
        """
        tenant_or_404(tenant_id)
        from .junior_worker import JuniorWorker
        eng = JuniorEngine(ctx.store, executor=_api_junior_executor(ctx.settings, ctx.executor),
                           tenants=ctx.tenants, observability=ctx.observability,
                           llm=make_client_from(ctx.settings))
        worker = JuniorWorker(ctx.store, eng, tenant_id=tenant_id,
                              work_start=ctx.settings.junior_work_start,
                              work_end=ctx.settings.junior_work_end,
                              min_interval_minutes=ctx.settings.junior_min_interval_minutes,
                              daily_cap=ctx.settings.junior_daily_cap,
                              observability=ctx.observability)
        return worker.run_cycle(tenant_id=tenant_id, force=body.force)

    # -- P6 stakeholder analyst -------------------------------------------
    @app.post("/stakeholder/{tenant_id}/answer")
    def stakeholder_answer(tenant_id: str, body: StakeholderIn) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        return C.stakeholder.answer(tenant_id, body.question, user_id=body.user_id)

    @app.post("/stakeholder/{tenant_id}/feedback")
    def stakeholder_feedback(tenant_id: str, body: FeedbackIn) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        return C.stakeholder.record_feedback(tenant_id, body.answer_id,
                                             user_id=body.user_id, rating=body.rating,
                                             comment=body.comment)

    @app.get("/stakeholder/{tenant_id}/quality")
    def stakeholder_quality(tenant_id: str) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        return C.stakeholder.quality(tenant_id)

    # -- P7 external research ---------------------------------------------
    @app.post("/research/{tenant_id}/sources/seed")
    def research_seed(tenant_id: str) -> List[Dict[str, Any]]:
        tenant_or_404(tenant_id)
        return C.research.seed_sources(tenant_id)

    @app.get("/research/{tenant_id}/sources")
    def research_sources(tenant_id: str) -> List[Dict[str, Any]]:
        tenant_or_404(tenant_id)
        return C.research.list_sources(tenant_id)

    @app.post("/research/{tenant_id}/sources/{source_id}/policy")
    def research_policy(tenant_id: str, source_id: str, body: SourcePolicyIn) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        return C.research.set_source_policy(tenant_id, source_id, body.policy)

    @app.post("/research/{tenant_id}/search")
    def research_search(tenant_id: str, body: ResearchBatchIn) -> List[Dict[str, Any]]:
        tenant_or_404(tenant_id)
        return C.research.search(tenant_id, body.query, results=body.results)

    @app.post("/research/{tenant_id}/capture")
    def research_capture(tenant_id: str, body: ResearchBatchIn) -> List[Dict[str, Any]]:
        tenant_or_404(tenant_id)
        return C.research.capture(tenant_id, body.query, body.results)

    @app.get("/research/{tenant_id}/docs")
    def research_docs(tenant_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        tenant_or_404(tenant_id)
        return C.research.list_docs(tenant_id, limit)

    @app.post("/research/{tenant_id}/promote")
    def research_promote(tenant_id: str, body: PromoteIn2) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        node = C.research.promote(tenant_id, body.doc_id, by=body.by, note=body.note)
        if node is None:
            raise HTTPException(404, "document not found")
        return node

    @app.get("/research/{tenant_id}/overview")
    def research_overview(tenant_id: str) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        return C.research.overview(tenant_id)

    # -- P8 auth / billing / retention ------------------------------------
    @app.post("/auth/login")
    def auth_login(body: LoginIn) -> Dict[str, Any]:
        role = Role(body.role) if body.role in Role.__members__.values() else Role.STAKEHOLDER
        if not C.settings.auth_secret:
            return {"error": "auth disabled: set ANALYTICS_AUTH_SECRET"}
        token = issue(C.settings.auth_secret, body.tenant_id, role.value, sub=body.sub)
        return {"token": token, "tenant_id": body.tenant_id, "role": role.value}

    @app.get("/auth/me")
    def auth_me(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        return C.auth.principal(authorization)

    def _unauth(exc: AuthError) -> None:
        raise HTTPException(exc.status, str(exc))

    @app.get("/billing/{tenant_id}/usage")
    def billing_usage(tenant_id: str,
                      authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        try:
            C.auth.require(authorization, tenant_id, [Role.OWNER, Role.TENANT_ADMIN,
                                                       Role.AUDITOR, Role.SENIOR])
        except AuthError as e:
            _unauth(e)
        return C.billing.usage(tenant_id)

    @app.get("/billing/report")
    def billing_report(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        try:
            C.auth.require(authorization, "", [Role.OWNER])
        except AuthError as e:
            _unauth(e)
        return C.billing.platform_report()

    @app.get("/retention/review")
    def retention_review(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        try:
            C.auth.require(authorization, "", [Role.OWNER, Role.TENANT_ADMIN])
        except AuthError as e:
            _unauth(e)
        return C.retention.review()

    @app.post("/retention/purge")
    def retention_purge(body: PurgeIn,
                        authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        try:
            C.auth.require(authorization, body.tenant_id or "", [Role.OWNER, Role.TENANT_ADMIN])
        except AuthError as e:
            _unauth(e)
        return C.retention.purge_expired(tenant_id=body.tenant_id, dry_run=body.dry_run)

    @app.delete("/tenants/{tenant_id}")
    def delete_tenant(tenant_id: str,
                      authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        try:
            C.auth.require(authorization, tenant_id, [Role.OWNER])
        except AuthError as e:
            _unauth(e)
        return C.retention.delete_tenant(tenant_id)

    return app


def main(port: int = 8000) -> None:
    import uvicorn
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()