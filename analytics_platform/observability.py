"""Observability: every internal hop emits an OpenTelemetry-style span + event.

Even though components are in-process, they always call ``Observability.span`` /
``event`` so the owner can answer "what fired, did it work, what did it cost" as
if each component were a monitored API. Per the plan (section 9.10) we never log
credentials, cookies, or sensitive rows.
"""
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from .database import Store, dump_json, load_json


def new_trace() -> str:
    return f"tr_{uuid.uuid4().hex[:16]}"


class Observability:
    def __init__(self, store: Store):
        self._store = store
        self._inmem: List[Dict[str, Any]] = []

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
            self._store.execute(sql, (rec["ts"], tenant_id, trace_id, stage, actor, resource,
                                      status, duration_ms, bytes_in, tokens_in, tokens_out, dump_json(rec["meta"])))
        except Exception:
            pass  # observability must never break the pipeline
        return trace_id

    # -- queries ---------------------------------------------------------------
    def recent(self, limit: int = 100, tenant_id: str = "") -> List[Dict[str, Any]]:
        rows = self._store.query_all(
            "SELECT * FROM telemetry WHERE (?='' OR tenant_id=?) ORDER BY id DESC LIMIT ?",
            (tenant_id, tenant_id, limit))
        out = [dict(r) for r in rows]
        for r in out:
            r["meta"] = load_json(r.get("meta"), {})
        return out

    def metrics(self, tenant_id: str = "") -> Dict[str, Any]:
        where = "WHERE (?='' OR tenant_id=?)"
        args = (tenant_id, tenant_id)
        total = self._store.query_one(f"SELECT COUNT(*) c, "
                                      f"SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) f, "
                                      f"AVG(duration_ms) a FROM telemetry {where}", args)
        by_stage = self._store.query_all(
            f"SELECT stage, COUNT(*) c, AVG(duration_ms) a FROM telemetry {where} GROUP BY stage ORDER BY stage", args)
        by_status = self._store.query_all(
            f"SELECT status, COUNT(*) c FROM telemetry {where} GROUP BY status", args)
        return {
            "scope": tenant_id or "platform",
            "total_spans": total["c"] if total else 0,
            "failed_spans": total["f"] if total else 0,
            "avg_span_ms": round(total["a"], 2) if total and total["a"] else 0.0,
            "by_stage": [{"stage": r["stage"], "count": r["c"], "avg_ms": round(r["a"], 2)} for r in by_stage],
            "by_status": [{"status": r["status"], "count": r["c"]} for r in by_status],
        }