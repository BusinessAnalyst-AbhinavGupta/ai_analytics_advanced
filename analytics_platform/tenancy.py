"""Tenant + Company Profile services. Every query is scoped by tenant_id.

Company targets are structured (not prose) so the junior analyst can reason about
"growth / margin / funnel / retention / risk / constraints" explicitly.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .database import Store, dump_json, load_json
from .domain import (AnalystAI, AnalystConfig, CompanyProfile, CompanyTarget,
                     DataSource, DataSourceKind, Tenant, TenantStatus, clamp_junior_depth,
                     new_id, now_iso)

# Analyst-role defaults (config panel). API keys never stored — injected at runtime.
_ROLES = ("junior", "senior", "stakeholder")


def _analyst_from_dict(role: str, d: Optional[Dict[str, Any]]) -> AnalystAI:
    d = d or {}
    return AnalystAI(role=role, enabled=bool(d.get("enabled", True)),
                     provider=d.get("provider", "") or "",
                     model=d.get("model", "") or "")


def _config_from_dict(tenant_id: str, cfg: Optional[Dict[str, Any]]) -> AnalystConfig:
    cfg = cfg or {}
    return AnalystConfig(
        tenant_id=tenant_id,
        junior=_analyst_from_dict("junior", cfg.get("junior")),
        senior=_analyst_from_dict("senior", cfg.get("senior")),
        stakeholder=_analyst_from_dict("stakeholder", cfg.get("stakeholder")),
        junior_depth=clamp_junior_depth(cfg.get("junior_depth", 1)),
        human_signoff_days=int(cfg.get("human_signoff_days", 7)),
        updated_at=cfg.get("updated_at", ""),
    )


class TenantService:
    def __init__(self, store: Store):
        self.store = store

    # -- tenants ---------------------------------------------------------------
    def create_tenant(self, name: str, region: str = "global",
                      llm_provider: str = "null", purpose: str = "",
                      retention_days: int = 90) -> Tenant:
        t = Tenant(id=new_id("tnt"), name=name, region=region,
                   llm_provider=llm_provider, purpose=purpose,
                   retention_days=retention_days, status=TenantStatus.ACTIVE)
        self.store.execute(
            "INSERT INTO tenants (id,name,region,llm_provider,retention_days,status,created_at,purpose) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (t.id, t.name, t.region, t.llm_provider, t.retention_days,
             t.status.value, t.created_at, t.purpose))
        return t

    def get_tenant(self, tenant_id: str) -> Optional[Tenant]:
        row = self.store.query_one("SELECT * FROM tenants WHERE id=?", (tenant_id,))
        if not row:
            return None
        r = dict(row)
        r["status"] = TenantStatus(r["status"]) if r["status"] else TenantStatus.ACTIVE
        return Tenant(**{k: r[k] for k in
                         ("id", "name", "region", "llm_provider", "retention_days",
                          "status", "created_at", "purpose")})

    def require_tenant(self, tenant_id: str) -> Tenant:
        t = self.get_tenant(tenant_id)
        if t is None:
            raise KeyError(f"Unknown tenant {tenant_id}")
        return t

    def list_tenants(self) -> List[Dict[str, Any]]:
        return self.store.rows_to_dicts(self.store.query_all("SELECT * FROM tenants ORDER BY created_at"))

    # -- company profile -------------------------------------------------------
    def set_company_profile(self, tenant_id: str, profile: Dict[str, Any],
                            changed_by: str = "owner") -> CompanyProfile:
        self.require_tenant(tenant_id)
        targets = [CompanyTarget(t["name"], **{k: v for k, v in t.items() if k != "name"})
                   if not isinstance(t, CompanyTarget) else t
                   for t in profile.get("targets", [])]
        p = CompanyProfile(tenant_id=tenant_id,
                           name=profile.get("name", ""),
                           industry=profile.get("industry", ""),
                           region=profile.get("region", ""),
                           description=profile.get("description", ""),
                           customers=profile.get("customers", ""),
                           product=profile.get("product", ""),
                           value_creation=profile.get("value_creation", ""),
                           revenue_model=profile.get("revenue_model", ""),
                           targets=targets,
                           constraints=list(profile.get("constraints", [])),
                           risks=list(profile.get("risks", [])),
                           competitors=list(profile.get("competitors", [])),
                           preferred_metrics=list(profile.get("preferred_metrics", [])))
        self.store.execute(
            "INSERT INTO company_profiles (tenant_id,name,industry,region,description,customers,"
            "product,value_creation,revenue_model,constraints,risks,competitors,preferred_metrics,targets) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(tenant_id) DO UPDATE SET name=excluded.name, industry=excluded.industry, "
            "region=excluded.region, description=excluded.description, customers=excluded.customers, "
            "product=excluded.product, value_creation=excluded.value_creation, "
            "revenue_model=excluded.revenue_model, constraints=excluded.constraints, "
            "risks=excluded.risks, competitors=excluded.competitors, "
            "preferred_metrics=excluded.preferred_metrics, targets=excluded.targets",
            (p.tenant_id, p.name, p.industry, p.region, p.description, p.customers,
             p.product, p.value_creation, p.revenue_model,
             dump_json(p.constraints), dump_json(p.risks), dump_json(p.competitors),
             dump_json(p.preferred_metrics), dump_json([t.to_dict() for t in p.targets])))
        # versioned history snapshot (business context changes over time) ---------
        previous = self.store.query_one(
            "SELECT COALESCE(MAX(version), 0) AS v FROM company_profile_history "
            "WHERE tenant_id=?", (tenant_id,))
        version = (previous["v"] if previous else 0) + 1
        self.store.execute(
            "INSERT INTO company_profile_history "
            "(id, tenant_id, version, snapshot, changed_by, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (new_id("cph"), tenant_id, version, dump_json(p.to_dict()),
             changed_by, now_iso()))
        return p

    def get_company_profile_history(self, tenant_id: str,
                                    limit: int = 20) -> List[Dict[str, Any]]:
        """Config-panel history: every profile version (business context over time)."""
        rows = self.store.query_all(
            "SELECT * FROM company_profile_history WHERE tenant_id=? "
            "ORDER BY version DESC LIMIT ?", (tenant_id, limit))
        out = []
        for r in rows:
            d = dict(r)
            d["snapshot"] = load_json(d.get("snapshot"), {})
            out.append(d)
        return out

    def get_company_profile(self, tenant_id: str) -> Optional[CompanyProfile]:
        row = self.store.query_one("SELECT * FROM company_profiles WHERE tenant_id=?", (tenant_id,))
        if not row:
            return None
        r = dict(row)
        r["constraints"] = load_json(r["constraints"], [])
        r["risks"] = load_json(r["risks"], [])
        r["competitors"] = load_json(r["competitors"], [])
        r["preferred_metrics"] = load_json(r["preferred_metrics"], [])
        target_dicts = load_json(r.pop("targets"), [])
        targets = [CompanyTarget(**t) for t in target_dicts]
        p = CompanyProfile(tenant_id=r.pop("tenant_id"), **r, targets=targets)
        return p

    # -- data sources ----------------------------------------------------------
    def add_datasource(self, tenant_id: str, name: str, kind: DataSourceKind,
                       dialect: str = "ANSI", tables: Optional[List[str]] = None,
                       connected: bool = True, config: Optional[Dict[str, Any]] = None) -> DataSource:
        self.require_tenant(tenant_id)
        ds = DataSource(id=new_id("ds"), tenant_id=tenant_id, name=name, kind=kind,
                        dialect=dialect, tables=tables or [], connected=connected,
                        config=config or {})
        self.store.execute(
            "INSERT INTO data_sources (id,tenant_id,name,kind,dialect,connected,tables,config,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ds.id, ds.tenant_id, ds.name, ds.kind.value, ds.dialect,
             int(ds.connected), dump_json(ds.tables), dump_json(ds.config), ds.created_at))
        return ds

    def list_datasources(self, tenant_id: str) -> List[Dict[str, Any]]:
        self.require_tenant(tenant_id)
        rows = self.store.query_all("SELECT * FROM data_sources WHERE tenant_id=?", (tenant_id,))
        out = []
        for r in self.store.rows_to_dicts(rows):
            r["tables"] = load_json(r["tables"], [])
            r["config"] = load_json(r["config"], {})
            out.append(r)
        return out

    # -- analyst AI config (toggles + per-role model) -----------------------
    def get_analyst_config(self, tenant_id: str) -> AnalystConfig:
        """Current analyst AI config (junior/senior/stakeholder) + model selection."""
        self.require_tenant(tenant_id)
        row = self.store.query_one(
            "SELECT config FROM analyst_configs WHERE tenant_id=?", (tenant_id,))
        cfg = load_json(row["config"]) if row and row["config"] else {}
        if not cfg:
            cfg = _config_from_dict(tenant_id, {}).to_dict()
        return _config_from_dict(tenant_id, cfg)

    def set_analyst_config(self, tenant_id: str, config: Dict[str, Any],
                           changed_by: str = "owner") -> AnalystConfig:
        """Persist analyst toggles + model config; append a versioned history \
        snapshot (config panel). API keys are ignored/stripped — never stored."""
        self.require_tenant(tenant_id)
        merged = self.get_analyst_config(tenant_id).to_dict()
        for role in _ROLES:
            incoming = config.get(role)
            if isinstance(incoming, dict):
                cur = merged.get(role, {})
                for k in ("enabled", "provider", "model"):
                    if k in incoming and incoming[k] is not None:
                        cur[k] = incoming[k] if k != "enabled" else \
                            (incoming[k] if isinstance(incoming[k], bool) else bool(incoming[k]))
                merged[role] = cur
        if config.get("junior_depth") is not None:
            merged["junior_depth"] = clamp_junior_depth(config["junior_depth"])
        if config.get("human_signoff_days") is not None:
            try:
                merged["human_signoff_days"] = max(0, int(config["human_signoff_days"]))
            except (TypeError, ValueError):
                pass
        cfg = _config_from_dict(tenant_id, merged)
        cfg.updated_at = now_iso()
        self.store.execute(
            "INSERT INTO analyst_configs (tenant_id, config, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(tenant_id) DO UPDATE SET config=excluded.config, "
            "updated_at=excluded.updated_at",
            (tenant_id, dump_json(cfg.to_dict()), cfg.updated_at))
        # versioned history (config panel log over time)
        prev = self.store.query_one(
            "SELECT COALESCE(MAX(version),0) AS v FROM analyst_config_history "
            "WHERE tenant_id=?", (tenant_id,))
        version = (prev["v"] if prev else 0) + 1
        self.store.execute(
            "INSERT INTO analyst_config_history "
            "(id, tenant_id, version, snapshot, changed_by, created_at) VALUES (?,?,?,?,?,?)",
            (new_id("acfg"), tenant_id, version, dump_json(cfg.to_dict()),
             changed_by, now_iso()))
        return cfg

    def get_analyst_config_history(self, tenant_id: str,
                                   limit: int = 20) -> List[Dict[str, Any]]:
        """Versioned log of analyst config changes (config panel history)."""
        rows = self.store.query_all(
            "SELECT * FROM analyst_config_history WHERE tenant_id=? "
            "ORDER BY version DESC LIMIT ?", (tenant_id, limit))
        out = []
        for r in self.store.rows_to_dicts(rows):
            r["snapshot"] = load_json(r.get("snapshot"), {})
            out.append(r)
        return out