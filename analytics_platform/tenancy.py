"""Tenant + Company Profile services. Every query is scoped by tenant_id.

Company targets are structured (not prose) so the junior analyst can reason about
"growth / margin / funnel / retention / risk / constraints" explicitly.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .database import Store, dump_json, load_json
from .domain import (CompanyProfile, CompanyTarget, DataSource, DataSourceKind,
                     Tenant, TenantStatus, new_id)


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
    def set_company_profile(self, tenant_id: str, profile: Dict[str, Any]) -> CompanyProfile:
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
        return p

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