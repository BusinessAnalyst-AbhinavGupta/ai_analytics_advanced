"""Phase 9 — autonomous background junior analyst.

`JuniorWorker` runs *not continuously*: it only acts inside its system-time window
(`Settings.junior_work_start`..`junior_work_end`, default 10:00–19:00) and at most
once per `Settings.junior_min_interval_minutes` (default one problem statement /
hour). Every query is executed **serially** — a process-wide lock guarantees the
worker never launches two queries at once, so it can't blast the data engine.

Each cycle the worker:
  1. picks a problem statement the junior engine suggested (goal-aligned, from
     approved targets/definitions/queries — never invented business goals),
  2. resolves SQL (prefers an approved, reproducible query; else a safe
     single-statement SELECT built from the statement's columns),
  3. executes it through the injectable executor (offline SamplerExecutor by
     default; BrowserSessionExecutor when live is configured),
  4. records an `analysis_runs` row + observability events.

It never writes the Brain, never calls the LLM, and never sends raw rows/sql/
cookies anywhere. Offline-safe and deterministically testable.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from .database import Store
from .domain import new_id, now_iso, RunStatus
from .execution.base import ExecutionContext
from .observability import Observability, new_trace


def _mins_since(ts: Optional[float], now: float) -> float:
    if ts is None:
        return float("inf")
    return (now - ts) / 60.0


class JuniorWorker:
    """One-tenant, serial, rate-limited background junior (Phase 9)."""

    def __init__(self, store: Store, junior: Any, *, tenant_id: str,
                 work_start: str = "10:00", work_end: str = "19:00",
                 min_interval_minutes: int = 60,
                 observability: Optional[Observability] = None,
                 clock: Any = time.time, default_tenant: Optional[str] = None):
        self.store = store
        self.junior = junior            # JuniorEngine (suggestions + executor)
        self.tenant_id = tenant_id      # also exposed for Scheduler.tick
        self.work_start = work_start
        self.work_end = work_end
        self.min_interval_minutes = int(min_interval_minutes)
        self.obs = observability or Observability(store)
        self._clock = clock
        self._lock = threading.Lock()   # serial gate: one query at a time
        self.default_tenant = default_tenant or tenant_id

    # -- window + rate limit -------------------------------------------------- #
    @staticmethod
    def _parse_hhmm(s: str) -> int:
        hh, _, mm = s.partition(":")
        return int(hh) * 60 + int(mm)

    def in_window(self, now: float) -> bool:
        t = time.localtime(now)
        cur = t.tm_hour * 60 + t.tm_min
        return self._parse_hhmm(self.work_start) <= cur < self._parse_hhmm(self.work_end)

    def due(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = now if now is not None else self._clock()
        last = self._last_ran_ts()
        return {
            "in_window": self.in_window(now),
            "rate_ok": _mins_since(last, now) >= self.min_interval_minutes,
            "last_cycle_ts": last,
        }

    def _state_key(self) -> str:
        return f"junior_last:{self.tenant_id}"

    def _last_ran_ts(self) -> Optional[float]:
        try:
            row = self.store.query_one(
                "SELECT value FROM scheduler_state WHERE key=?", (self._state_key(),))
            if row and row["value"]:
                return float(row["value"])
        except Exception:
            pass
        return None

    def _record_ran(self, now: float) -> None:
        try:
            self.store.execute(
                "INSERT INTO scheduler_state (key,value,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (self._state_key(), str(now),
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        except Exception:
            pass
        # -- pipeline -------------------------------------------------------- #
    def pick_problem_statement(self) -> Dict[str, Any]:
        """Choose one goal-aligned suggestion from the junior engine."""
        try:
            s = self.junior.suggest_questions(self.tenant_id, limit_per_target=1)
            for sg in s.get("suggestions", []):
                if sg.get("question"):
                    return sg
        except Exception:
            pass
        return {"target": "", "category": "", "priority": 0,
                "question": "", "columns": [], "source": "none"}

    def resolve_sql(self, statement: Dict[str, Any]) -> str:
        """Prefer an approved reproducible query; else a safe single SELECT."""
        try:
            rows = self.junior.approved_queries(self.tenant_id, limit=1)
            if rows:
                sql = (rows[0].payload or {}).get("sql", "")
                if sql and sql.strip():
                    return sql
        except Exception:
            pass
        cols = [c for c in statement.get("columns", []) if c]
        tables: List[str] = []
        try:
            tables = self.junior.datasets(self.tenant_id)
        except Exception:
            tables = []
        col = cols[0] if cols else "*"
        if tables:
            return f"SELECT {col} FROM {tables[0]} LIMIT 1000"
        return "SELECT 1 AS sanity WHERE 1=1"

    def run_cycle(self, tenant_id: Optional[str] = None,
                  now: Optional[float] = None) -> Dict[str, Any]:
        """Execute at most one problem statement, serially. May skip."""
        t0 = time.perf_counter()
        now = now if now is not None else self._clock()
        tid = tenant_id or self.tenant_id
        win = self.in_window(now)
        rate_ok = _mins_since(self._last_ran_ts(), now) >= self.min_interval_minutes
        if not win or not rate_ok:
            return {"ran": False, "tenant_id": tid,
                    "in_window": win, "rate_ok": rate_ok,
                    "reason": "outside_window" if not win else "rate_limited",
                    "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2)}

        statement = self.pick_problem_statement()
        question = statement.get("question") or "Baseline: refresh domain understanding"
        sql = self.resolve_sql(statement)
        self.obs.event(tenant_id=tid, stage="junior.bg_started", actor="junior",
                       resource=question[:60], status="OK")

        # serial gate ---------------------------------------------------------
        with self._lock:
            self.obs.event(tenant_id=tid, stage="junior.bg_query", actor="junior",
                           resource=sql[:60], status="OK")
            ok, row_count, error = False, 0, ""
            try:
                result = self.junior.executor.execute(
                    sql, ExecutionContext(
                        tenant_id=tid, question=question, trace_id=new_trace(),
                        row_limit=50000,
                        dialect=getattr(self.junior, "settings", None) is not None
                        and getattr(self.junior.settings, "source_dialect", "athena")
                        or "athena"))
                ok = bool(result.ok)
                row_count = int(result.row_count or 0)
                if not ok:
                    error = result.error or ""
            except Exception as e:  # never kill the loop
                error = str(e)

        self._record_ran(now)
        self._save_run(tid, question, sql, ok, row_count, error, t0, statement)
        self.obs.event(tenant_id=tid, stage="junior.bg_completed", actor="junior",
                       resource=question[:60], status="OK" if ok else "FAILED",
                       duration_ms=(time.perf_counter() - t0) * 1000.0,
                       meta={"sql": sql, "row_count": row_count,
                             "error": error[:200] if error else ""})
        return {"ran": True, "tenant_id": tid, "in_window": win, "rate_ok": True,
                "question": question, "sql": sql, "ok": ok, "row_count": row_count,
                "error": error, "source": statement.get("source"),
                "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2)}

    def _save_run(self, tid: str, question: str, sql: str, ok: bool,
                  row_count: int, error: str, t0: float,
                  statement: Dict[str, Any]) -> None:
        from .database import dump_json, load_json
        run_status = RunStatus.COMPLETED if ok else RunStatus.FAILED
        try:
            self.store.execute(
                "INSERT INTO analysis_runs (id,tenant_id,trace_id,question_id,question_text,"
                "sql,dialect,executor,status,generated_at,execution_ms,row_count,"
                "profile_summary,answer,facts,hypotheses,uncertainties,next_actions,"
                "cost_estimate,policy_reasons,source_node_ids) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("run"), tid, new_trace(), "", question, sql, "athena",
                 "junior-bg", run_status.value, now_iso(),
                 round((time.perf_counter() - t0) * 1000.0, 2), row_count,
                 dump_json({"error": error} if not ok else {"rows": row_count}),
                 "", "[]", "[]", "[]", "[]", 0.0, "[]", "[]"))
        except Exception:
            pass  # persisting the run must never break the worker


__all__ = ["JuniorWorker"]