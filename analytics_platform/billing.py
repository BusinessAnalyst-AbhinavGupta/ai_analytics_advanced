"""P8 — Per-tenant usage + cost metering.

Attributable usage/cost: the plan's exit is "per-tenant usage/cost attributable".
We aggregate the observability telemetry the platform already records
(spans/duration/tokens) and price it with the configured USD/1k rates, so every
tenant sees what it consumed and how much it cost — no separate billable event
path needed, and nothing sensitive is stored.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .config import Settings
from .database import Store
from .observability import Observability
from .stores import TenantStoreProvider


class BillingService:
    def __init__(self, stores: TenantStoreProvider, settings: Optional[Settings] = None,
                 observability: Optional[Observability] = None):
        self.stores = stores
        self.settings = settings or Settings()
        self.obs = observability or Observability(stores)

    def usage(self, tenant_id: str) -> Dict[str, Any]:
        store = self.stores.for_tenant(tenant_id)
        row = store.query_one(
            "SELECT COUNT(*) spans, "
            "SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) failed, "
            "AVG(duration_ms) avg_ms, "
            "SUM(COALESCE(tokens_in,0)) t_in, SUM(COALESCE(tokens_out,0)) t_out "
            "FROM telemetry WHERE tenant_id=?", (tenant_id,))
        by_stage = store.query_all(
            "SELECT stage, COUNT(*) c, AVG(duration_ms) a FROM telemetry "
            "WHERE tenant_id=? GROUP BY stage ORDER BY stage", (tenant_id,))
        runs = store.query_one(
            "SELECT COUNT(*) c FROM analysis_runs WHERE tenant_id=?", (tenant_id,))["c"]
        answers = store.query_one(
            "SELECT COUNT(*) c FROM stakeholder_answers WHERE tenant_id=?", (tenant_id,))["c"]
        tin = int(row["t_in"] or 0)
        tout = int(row["t_out"] or 0)
        cost_in = (tin / 1000.0) * self.settings.cost_per_1k_input
        cost_out = (tout / 1000.0) * self.settings.cost_per_1k_output
        return {
            "tenant_id": tenant_id,
            "spans": int(row["spans"] or 0),
            "failed_spans": int(row["failed"] or 0),
            "avg_span_ms": round(float(row["avg_ms"] or 0), 2),
            "tokens_in": tin,
            "tokens_out": tout,
            "cost_usd": {"input": round(cost_in, 6), "output": round(cost_out, 6),
                         "total": round(cost_in + cost_out, 6)},
            "attribution": {"analysis_runs": int(runs), "stakeholder_answers": int(answers)},
            "by_stage": [{"stage": r["stage"], "count": r["c"],
                          "avg_ms": round(r["a"], 2)} for r in by_stage],
        }

    def platform_report(self) -> Dict[str, Any]:
        # The tenant registry is control-plane; usage/telemetry is per-tenant, so
        # this aggregates one query per tenant against that tenant's own store
        # rather than a single GROUP BY over one flat file.
        tenants = self.stores.control.rows_to_dicts(
            self.stores.control.query_all("SELECT id, name FROM tenants ORDER BY id"))
        by_tenant = {t["id"]: {"name": t["name"], "spans": 0, "cost_usd": 0.0} for t in tenants}
        total = 0.0
        for t in tenants:
            store = self.stores.for_tenant(t["id"])
            r = store.query_one(
                "SELECT COUNT(*) spans, "
                "SUM(COALESCE(tokens_in,0)) t_in, SUM(COALESCE(tokens_out,0)) t_out "
                "FROM telemetry WHERE tenant_id=?", (t["id"],))
            tin = int(r["t_in"] or 0) if r else 0
            tout = int(r["t_out"] or 0) if r else 0
            cost = (tin / 1000.0) * self.settings.cost_per_1k_input \
                + (tout / 1000.0) * self.settings.cost_per_1k_output
            by_tenant[t["id"]]["spans"] = int(r["spans"] or 0) if r else 0
            by_tenant[t["id"]]["cost_usd"] = round(cost, 6)
            total += cost
        return {"as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "tenants": list(by_tenant.values()),
                "total_cost_usd": round(total, 6)}


__all__ = ["BillingService"]