"""Observability: every internal hop emits an OpenTelemetry-style span + event.

Even though components are in-process, they always call ``Observability.span`` /
``event`` so the owner can answer "what fired, did it work, what did it cost" as
if each component were a monitored API. Per the plan (section 9.10) we never log
credentials, cookies, or sensitive rows.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from .database import dump_json, load_json
from .stores import TenantIsolationError, TenantStoreProvider

logger = logging.getLogger(__name__)


def new_trace() -> str:
    return f"tr_{uuid.uuid4().hex[:16]}"


class Observability:
    def __init__(self, stores: TenantStoreProvider, on_event: Optional[Any] = None):
        self.stores = stores
        self._inmem: List[Dict[str, Any]] = []
        self.on_event = on_event

    # -- span context manager -------------------------------------------------
    @contextmanager
    def span(self, *, tenant_id: str = "", trace_id: str = "", stage: str,
             actor: str = "system", resource: str = "",
             extras: Optional[Dict[str, Any]] = None) -> Iterator["Observability"]:
        t0 = time.perf_counter()
        trace_id = trace_id or new_trace()
        metadata = dict(extras or {})
        try:
            yield self
        except Exception:
            self.event(tenant_id=tenant_id, trace_id=trace_id, stage=stage, actor=actor,
                       resource=resource, status="FAILED", duration_ms=0.0, meta=metadata)
            raise
        else:
            duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            self.event(tenant_id=tenant_id, trace_id=trace_id, stage=stage, actor=actor,
                       resource=resource, status="OK", duration_ms=duration_ms, meta=metadata)

    # -- event -----------------------------------------------------------------
    def event(self, *, tenant_id: str = "", trace_id: str = "", stage: str,
              actor: str = "system", resource: str = "",
              status: str = "OK", duration_ms: float = 0.0,
              bytes_in: int = 0, tokens_in: int = 0, tokens_out: int = 0,
              meta: Optional[Dict[str, Any]] = None) -> str:
        trace_id = trace_id or new_trace()
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tenant_id": tenant_id, "trace_id": trace_id, "stage": stage,
            "actor": actor, "resource": resource, "status": status,
            "duration_ms": duration_ms, "bytes_in": bytes_in,
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "meta": meta or {},
        }
        self._inmem.append(rec)
        sql = ("INSERT INTO telemetry (ts,tenant_id,trace_id,stage,actor,resource,status,"
               "duration_ms,bytes_in,tokens_in,tokens_out,meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
        try:
            # telemetry is tenant-scoped: it lives in the company's own database.
            # An empty tenant_id (platform-level events, e.g. the scheduler's
            # maintenance ticks) has no tenant database to route to, so
            # `for_tenant("")` raises and the write is skipped here — the record
            # still lands in `self._inmem` and reaches `on_event` below.
            self.stores.for_tenant(tenant_id).execute(
                sql, (rec["ts"], tenant_id, trace_id, stage, actor, resource,
                     status, duration_ms, bytes_in, tokens_in, tokens_out, dump_json(rec["meta"])))
        except TenantIsolationError as e:
            # An isolation failure is not an ordinary telemetry-write hiccup: it
            # means this tenant's database file records a DIFFERENT company as
            # its owner. This method's contract is that observability must never
            # break the pipeline, so it cannot re-raise — but it must not be
            # indistinguishable from a routine WARNING either. ERROR, named as
            # security-relevant, so it separates cleanly in the logs.
            logger.error(
                "SECURITY: tenant isolation failure while persisting a telemetry "
                "event (stage=%r, tenant_id=%r): %s. This tenant's database is "
                "owned by another company; the event was NOT persisted and the "
                "store must be investigated before it is used again.",
                stage, tenant_id, e, exc_info=True)
        except Exception as e:
            logger.warning(f"Failed to persist telemetry event (stage={stage!r}, tenant_id={tenant_id!r}): {e}")
            # observability must never break the pipeline

        if self.on_event:
            try:
                self.on_event(rec)
            except Exception:
                pass

        return trace_id

    # -- queries ---------------------------------------------------------------
    def _telemetry_stores(self, tenant_id: str) -> List[Any]:
        """The tenant store to query, or every known tenant's store when no
        single tenant is named (telemetry has no cross-tenant table anymore)."""
        if tenant_id:
            return [self.stores.for_tenant(tenant_id)]
        return [self.stores.for_tenant(t) for t in self.stores.known_tenants()]

    def recent(self, limit: int = 100, tenant_id: str = "") -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for store in self._telemetry_stores(tenant_id):
            rows = store.query_all(
                "SELECT * FROM telemetry WHERE (?='' OR tenant_id=?) ORDER BY id DESC LIMIT ?",
                (tenant_id, tenant_id, limit))
            out.extend(dict(r) for r in rows)
        out.sort(key=lambda r: (r.get("ts") or "", r.get("id") or 0), reverse=True)
        out = out[:limit]
        for r in out:
            r["meta"] = load_json(r.get("meta"), {})
        return out

    def metrics(self, tenant_id: str = "") -> Dict[str, Any]:
        where = "WHERE (?='' OR tenant_id=?)"
        args = (tenant_id, tenant_id)
        count = 0
        failed = 0
        duration_total = 0.0
        by_stage: Dict[str, Dict[str, Any]] = {}
        by_status: Dict[str, int] = {}
        for store in self._telemetry_stores(tenant_id):
            total = store.query_one(f"SELECT COUNT(*) c, "
                                    f"SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) f, "
                                    f"AVG(duration_ms) a FROM telemetry {where}", args)
            if total and total["c"]:
                count += total["c"]
                failed += total["f"] or 0
                duration_total += (total["a"] or 0.0) * total["c"]
            for r in store.query_all(
                    f"SELECT stage, COUNT(*) c, AVG(duration_ms) a FROM telemetry {where} "
                    f"GROUP BY stage ORDER BY stage", args):
                agg = by_stage.setdefault(r["stage"], {"count": 0, "duration_total": 0.0})
                agg["count"] += r["c"]
                agg["duration_total"] += (r["a"] or 0.0) * r["c"]
            for r in store.query_all(
                    f"SELECT status, COUNT(*) c FROM telemetry {where} GROUP BY status", args):
                by_status[r["status"]] = by_status.get(r["status"], 0) + r["c"]
        return {
            "scope": tenant_id or "platform",
            "total_spans": count,
            "failed_spans": failed,
            "avg_span_ms": round(duration_total / count, 2) if count else 0.0,
            "by_stage": [{"stage": stage, "count": agg["count"],
                         "avg_ms": round(agg["duration_total"] / agg["count"], 2) if agg["count"] else 0.0}
                        for stage, agg in sorted(by_stage.items())],
            "by_status": [{"status": status, "count": c} for status, c in by_status.items()],
        }

    # -- Phase 9: owner-facing API logs (30-day retention; control plane) ----
    def log_access(self, *, tenant_id: str = "", method: str = "", path: str = "",
                   status: int = 200, duration_ms: float = 0.0,
                   actor: str = "system", meta: Optional[Dict[str, Any]] = None) -> None:
        """Record one HTTP request in `api_logs` (owner-facing, never credentials)."""
        try:
            self.stores.control.execute(
                "INSERT INTO api_logs (ts,tenant_id,method,path,status,duration_ms,actor,meta) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), tenant_id,
                 method, path, int(status), round(float(duration_ms), 2), actor,
                 dump_json(meta or {})))
        except Exception:
            pass  # logging must never break the API

    def logs(self, *, tenant_id: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        rows = self.stores.control.query_all(
            "SELECT * FROM api_logs WHERE (?='' OR tenant_id=?) ORDER BY id DESC LIMIT ?",
            (tenant_id, tenant_id, limit))
        out = [dict(r) for r in rows]
        for r in out:
            r["meta"] = load_json(r.get("meta"), {})
        return out

    def purge_logs(self, retention_days: int = 30, dry_run: bool = True,
                   now: Optional[float] = None) -> Dict[str, Any]:
        """Delete API logs older than `retention_days` (Phase 9 retention)."""
        now = now if now is not None else time.time()
        cutoff_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(now - retention_days * 86400.0))
        cur = self.stores.control.query_one(
            "SELECT COUNT(*) c FROM api_logs WHERE ts < ?", (cutoff_iso,))
        count = cur["c"] if cur else 0
        if not dry_run and count:
            self.stores.control.execute("DELETE FROM api_logs WHERE ts < ?", (cutoff_iso,))
        return {"dry_run": dry_run, "retention_days": retention_days,
                "cutoff": cutoff_iso, "expired_rows": count}