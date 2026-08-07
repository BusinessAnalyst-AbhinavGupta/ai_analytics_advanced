"""P8 — Retention + deletion.

Implements the plan's retention/deletion exit ("customer data deletable per
policy") and the GDPR-readiness path: per-tenant retention purges old mutable
records (telemetry, runs, questions, stakeholder activity, research captures)
against each tenant's `retention_days`, and full tenant deletion wipes every
tenant-owned row while leaving an append-only audit record (no customer data).

Governed knowledge nodes are intentionally *not* time-purged here: they are
versioned, reviewed artifacts with their own freshness/stale lifecycle — deleting
them would silently lose reviewed knowledge. If a policy wants them gone, use
full tenant deletion (or archive them via the review workflow).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .database import Store
from .domain import new_id, now_iso
from .observability import Observability
from .tenancy import TenantService

# mutable activity tables with an ISO-8601 UTC timestamp column to age out
RETENTION_TABLES = [
    ("telemetry", "ts"),
    ("analysis_runs", "generated_at"),
    ("questions", "created_at"),
    ("stakeholder_answers", "created_at"),
    ("stakeholder_feedback", "created_at"),
    ("research_docs", "created_at"),
]


def _cutoff_iso(retention_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class RetentionService:
    def __init__(self, store: Store, tenants: Optional[TenantService] = None,
                 observability: Optional[Observability] = None):
        self.store = store
        self.tenants = tenants or TenantService(store)
        self.obs = observability or Observability(store)

    # -- review ---------------------------------------------------------------
    def review(self) -> Dict[str, Any]:
        """Per-tenant counts of rows that would be purged under current policy."""
        tenants = self.tenants.list_tenants()
        out = []
        total = 0
        for t in tenants:
            rows = self._count_expired(t["id"], int(t["retention_days"] or 90))
            out.append({"tenant_id": t["id"], "retention_days": t["retention_days"],
                        "expiring": rows})
            total += sum(rows.values())
        return {"tenants": out, "total_expiring_rows": total}

    def _count_expired(self, tenant_id: str, retention_days: int) -> Dict[str, int]:
        cutoff = _cutoff_iso(retention_days)
        counts: Dict[str, int] = {}
        for table, col in RETENTION_TABLES:
            r = self.store.query_one(
                f"SELECT COUNT(*) c FROM {table} WHERE tenant_id=? AND {col}<?",
                (tenant_id, cutoff))
            counts[table] = r["c"]
        return counts

    # -- purge ----------------------------------------------------------------
    def purge_expired(self, tenant_id: Optional[str] = None,
                      dry_run: bool = True) -> Dict[str, Any]:
        """Delete rows older than each tenant's retention policy."""
        tenants = self.tenants.list_tenants()
        if tenant_id:
            tenants = [t for t in tenants if t["id"] == tenant_id]
        removed = {"tenants": [], "tables": {}}
        for t in tenants:
            cutoff = _cutoff_iso(int(t["retention_days"] or 90))
            table_counts: Dict[str, int] = {}
            for table, col in RETENTION_TABLES:
                cur = self.store.query_one(
                    f"SELECT COUNT(*) c FROM {table} WHERE tenant_id=? AND {col}<?",
                    (t["id"], cutoff))["c"]
                if not dry_run and cur:
                    self.store.execute(
                        f"DELETE FROM {table} WHERE tenant_id=? AND {col}<?",
                        (t["id"], cutoff))
                table_counts[table] = cur
            removed["tables"][t["id"]] = table_counts
            removed["tenants"].append(t["id"])
            if not dry_run:
                self.obs.event(tenant_id=t["id"], stage="retention.purge", actor="system",
                               status="OK", meta={"removed": table_counts})
        return {"dry_run": dry_run, "removed": removed}

    # -- tenant deletion (GDPR) ------------------------------------------------
    def delete_tenant(self, tenant_id: str, by: str = "owner") -> Dict[str, Any]:
        """Wipe every tenant-owned row; keep an append-only audit record."""
        targets = [
            "knowledge_nodes", "company_profiles", "data_sources", "questions",
            "analysis_runs", "stakeholder_answers", "stakeholder_feedback",
            "research_sources", "research_docs", "auth_principals",
        ]
        deleted: Dict[str, int] = {}
        for table in targets:
            cur = self.store.query_one(
                f"SELECT COUNT(*) c FROM {table} WHERE tenant_id=?", (tenant_id,))["c"]
            if cur:
                self.store.execute(f"DELETE FROM {table} WHERE tenant_id=?", (tenant_id,))
            deleted[table] = cur
        tenants = self.tenants.list_tenants()
        if any(t["id"] == tenant_id for t in tenants):
            self.store.execute("DELETE FROM tenants WHERE id=?", (tenant_id,))
        self.store.execute(
            "INSERT INTO audit_log (ts,tenant_id,actor,role,action,resource,outcome,detail) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now_iso(), tenant_id, by, "owner", "tenant.delete", tenant_id, "OK",
             _b64dump(deleted)))
        self.obs.event(tenant_id=tenant_id, stage="retention.delete", actor=by,
                       status="OK", meta={"deleted": deleted})
        return {"tenant_id": tenant_id, "deleted_rows": deleted,
                "deleted_tables": [t for t, c in deleted.items() if c]}


def _b64dump(d: Dict[str, int]) -> str:
    import json as _json
    return _json.dumps(d)


__all__ = ["RetentionService"]