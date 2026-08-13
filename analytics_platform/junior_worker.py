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

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from .analysis import (analyze_results, evaluate_rules, profile_df,
                       synthesize_analysis_llm)
from .brain.store import CompanyBrain
from .database import Store, dump_json, load_json
from .domain import (AnalysisRun, AnswerMode, NodeKind, ReviewStatus, RunStatus,
                     clamp_junior_depth, new_id, now_iso)
from .execution.base import ExecutionContext
from .markdown import write_analysis_md, write_workpaper_md
from .observability import Observability, new_trace
from .stores import TenantStoreProvider


logger = logging.getLogger(__name__)


def _mins_since(ts: Optional[float], now: float) -> float:
    if ts is None:
        return float("inf")
    return (now - ts) / 60.0


class JuniorWorker:
    """One-tenant, serial, rate-limited background junior (Phase 9)."""

    def __init__(self, stores: TenantStoreProvider, junior: Any, *, tenant_id: str,
                 work_start: str = "10:00", work_end: str = "19:00",
                 min_interval_minutes: int = 60,
                 daily_cap: int = 3,
                 review_backlog_max: int = 3,
                 autopromote_cap: int = 500,
                 supporting_cap: int = 5,
                 observability: Optional[Observability] = None,
                 clock: Any = time.time, default_tenant: Optional[str] = None,
                 reviews_dir: str = "data/reviews",
                 row_limit: int = 50000):
        self.stores = stores
        self.tenant_id = tenant_id      # also exposed for Scheduler.tick
        # One tenant per worker, so its own database is resolved once here.
        # `scheduler_state` (rate/daily-cap bookkeeping) is control-plane and is
        # accessed via `self.stores.control` explicitly where needed below.
        self.store = stores.for_tenant(tenant_id)
        self.junior = junior            # JuniorEngine (suggestions + executor)
        self.work_start = work_start
        self.work_end = work_end
        self.min_interval_minutes = int(min_interval_minutes)
        self.daily_cap = int(daily_cap)  # at most N analyses per UTC day (persisted)
        # CP-14: pause generating once this many completed analyses are still
        # awaiting (or returned for) senior review, so the inbox never grows unbounded.
        self.review_backlog_max = int(review_backlog_max)
        # CP-15: low-level exploratory analyses auto-fold to FINDINGs, bounded so
        # schema probes never spam the Brain; high-level runs may spawn supporting
        # low-level probes (exempt from daily/rate caps) up to `supporting_cap`.
        self.autopromote_cap = int(autopromote_cap)
        self.supporting_cap = int(supporting_cap)
        self.obs = observability or Observability(stores)
        self._clock = clock
        self._lock = threading.Lock()   # serial gate: one query at a time
        self.default_tenant = default_tenant or tenant_id
        self.reviews_dir = reviews_dir
        self.row_limit = int(row_limit)

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

    def _daily_key(self, now: float) -> str:
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        return f"junior_daily:{self.tenant_id}:{day}"

    def _runs_today(self, now: float) -> int:
        """Persisted count of junior analyses already run this UTC day.

        `scheduler_state` is a control-plane table (one file, not per tenant);
        this worker's own database has no such table."""
        try:
            row = self.stores.control.query_one(
                "SELECT value FROM scheduler_state WHERE key=?", (self._daily_key(now),))
            if row and row["value"]:
                return int(float(row["value"]))
        except Exception:  # noqa: BLE001 - best-effort
            pass
        return 0

    def _last_ran_ts(self) -> Optional[float]:
        try:
            row = self.stores.control.query_one(
                "SELECT value FROM scheduler_state WHERE key=?", (self._state_key(),))
            if row and row["value"]:
                return float(row["value"])
        except Exception:
            pass
        return None

    def _record_ran(self, now: float) -> None:
        try:
            self.stores.control.execute(
                "INSERT INTO scheduler_state (key,value,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (self._state_key(), str(now),
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))

            # persist the per-UTC-day count so restarts never reset the 3/day cap
            dkey = self._daily_key(now)
            self.stores.control.execute(
                "INSERT INTO scheduler_state (key,value,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (dkey, str(self._runs_today(now) + 1),
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        except Exception:
            pass
        # -- pipeline -------------------------------------------------------- #
    def pick_problem_statement(self) -> Dict[str, Any]:
        """Pick the next problem statement across the two-tier taxonomy (CP-15).

        Prefers genuinely new questions (never re-asks an answered one), rotates
        across categories for variety, and honours promotion/demotion: high-level
        questions are available only when `junior_depth>=2`. Reproducible approved
        queries are kept in the pool as low-level `reproduce` items so prior work
        stays usable alongside new schema/fill/breakdown/contribution probes.
        Falls back to a baseline so the worker always has a safe no-op.
        """
        tid = self.tenant_id
        depth = self._junior_depth(tid)
        answered = self._answered_questions()
        cands: List[Dict[str, Any]] = []
        try:
            s = self.junior.suggest_questions(tid, limit_per_target=2)
            cands = [x for x in s.get("suggestions", [])
                     if x.get("question")
                     and x["question"] not in answered
                     and ((x.get("level") or "low") != "high" or depth >= 2)]
        except Exception:  # noqa: BLE001 - best-effort
            cands = []
        # approved, reproducible queries stay in the pool (low-level, reuse prior work)
        for p in self._reproducible_approved():
            if p["question"] not in answered:
                cands.append({"target": "", "category": "reproduce", "priority": 0,
                              "question": p["question"], "columns": [],
                              "source": "approved", "level": "low", "sql": p["sql"]})
        if not cands:
            return {"target": "", "category": "", "priority": 0,
                    "question": "", "columns": [], "source": "none"}
        # promoted junior leads with high-level work; otherwise low-level first.
        if depth >= 2:
            order = ("rca", "hypothesis", "fill_rate", "success_trend", "breakdown",
                     "funnel_by_dim", "contribution_uni", "overview", "schema", "reproduce")
        else:
            order = ("fill_rate", "success_trend", "breakdown", "funnel_by_dim",
                     "contribution_uni", "overview", "schema", "reproduce", "rca", "hypothesis")
        last_cat = self._last_category(tid)
        start = (order.index(last_cat) + 1) if last_cat in order else 0
        for i in range(len(order)):  # rotate forward from the last used category
            cat = order[(start + i) % len(order)]
            hit = next((c for c in cands if c.get("category") == cat), None)
            if hit:
                return {**hit, "sql": hit.get("sql") or self.resolve_sql(hit)}
        chosen = cands[0]
        return {**chosen, "sql": chosen.get("sql") or self.resolve_sql(chosen)}

    def _reproducible_approved(self) -> List[Dict[str, Any]]:
        """Approved, template-free (reproducible) queries for this tenant.

        A query is reproducible only when it is pure SQL: no Metabase template
        tags and no embedded JSON/escape braces (which Metabase's parser rejects).
        """
        out: List[Dict[str, Any]] = []
        try:
            for n in self.junior.approved_queries(self.tenant_id, limit=200):
                sql = (n.payload or {}).get("sql", "")
                if sql and "{{" not in sql and "{" not in sql and "}" not in sql:
                    out.append({"id": n.id, "title": n.title,
                                "question": n.title, "sql": sql})
        except Exception:  # noqa: BLE001 - best-effort
            pass
        return out

    def _recent_sqls(self, k: int = 8) -> set:
        """SQL of the most recent junior runs (to avoid immediate repeats)."""
        try:
            rows = self.store.query_all(
                "SELECT sql FROM analysis_runs WHERE tenant_id=? AND executor='junior-bg' "
                "ORDER BY generated_at DESC LIMIT ?", (self.tenant_id, k))
            return {r["sql"] for r in rows}
        except Exception:  # noqa: BLE001 - best-effort
            return set()

    def _answered_questions(self) -> set:
        """Question texts the junior already answered (any completed run).

        CP-14: the worker records which questions it has asked (`analysis_runs`),
        so it never re-asks the same one. A question stays 'answered' whichever
        way the senior ruled on it — approved (now a FINDING in the knowledge
        graph) or rejected (declined) — the junior moves on to unexplored angles.
        """
        try:
            rows = self.store.query_all(
                "SELECT DISTINCT question_text FROM analysis_runs "
                "WHERE tenant_id=? AND executor='junior-bg' AND status IN (?,?)",
                (self.tenant_id, RunStatus.COMPLETED.value, RunStatus.EXECUTED.value))
            return {r["question_text"] for r in rows}
        except Exception:  # noqa: BLE001 - best-effort
            return set()

    def _pending_review_count(self, tenant_id: Optional[str] = None) -> int:
        """Completed junior analyses still waiting on (or returned for) senior review."""
        tid = tenant_id or self.tenant_id
        try:
            row = self.store.query_one(
                "SELECT COUNT(*) AS c FROM analysis_runs WHERE tenant_id=? "
                "AND executor='junior-bg' AND status IN (?,?) AND review_status IN (?,?) "
                "AND COALESCE(supportive_of, '')=''",
                (tid, RunStatus.COMPLETED.value, RunStatus.EXECUTED.value,
                 ReviewStatus.CANDIDATE.value, ReviewStatus.REVISION_REQUIRED.value))
            return int(row["c"]) if row else 0
        except Exception:  # noqa: BLE001 - best-effort
            return 0

    def _junior_depth(self, tid: Optional[str] = None) -> int:
        """Current junior_depth (0..2) for the run tenant; defaults to 1."""
        tid = tid or self.tenant_id
        try:
            return clamp_junior_depth(
                self.junior.tenants.get_analyst_config(tid).junior_depth)
        except Exception:  # noqa: BLE001 - best-effort
            return 1

    def _last_category(self, tid: str) -> Optional[str]:
        """Category of the most recent standalone junior run (variety rotation)."""
        try:
            row = self.store.query_one(
                "SELECT category FROM analysis_runs WHERE tenant_id=? AND executor='junior-bg' "
                "AND COALESCE(supportive_of,'')='' AND category IS NOT NULL AND category<>'' "
                "ORDER BY generated_at DESC LIMIT 1", (tid,))
            return row["category"] if row else None
        except Exception:  # noqa: BLE001 - best-effort
            return None

    def _autopromote_count(self, tid: str) -> int:
        """Approved FINDINGs for the tenant (bounds auto-accept Brain spam)."""
        try:
            row = self.store.query_one(
                "SELECT COUNT(*) AS c FROM knowledge_nodes WHERE tenant_id=? AND kind=? "
                "AND status=?", (tid, NodeKind.FINDING.value, ReviewStatus.APPROVED.value))
            return int(row["c"]) if row else 0
        except Exception:  # noqa: BLE001 - best-effort
            return 0

    def _maybe_autopromote(self, tid: str, run: Optional[AnalysisRun]) -> Optional[Any]:
        """Auto-fold a low-level exploratory run to an approved FINDING (CP-15).

        Guarded: only a COMPLETED low-level run that produced data and no
        HIGH/CRITICAL anomaly escalates a rule trigger — and only up to
        `autopromote_cap` approved FINDINGs per tenant. Schema/EDA probes (no
        rows) never promote.
        """
        if run is None or run.status != RunStatus.COMPLETED:
            return None
        if (run.level or "low") == "high":
            return None
        if any(r.get("severity") in ("CRITICAL", "HIGH")
               for r in (run.rule_triggers or [])):
            return None
        if not run.row_count:
            return None  # schema/EDA probe produced no data
        if self._autopromote_count(tid) >= self.autopromote_cap:
            self.obs.event(tenant_id=tid, stage="junior.autopromote_capped",
                           actor="junior", resource=run.id, status="WARN",
                           meta={"cap": self.autopromote_cap})
            return None
        brain = CompanyBrain(self.store, tid)
        node = brain.create(
            NodeKind.FINDING, title=run.question_text, summary=run.answer,
            payload={"facts": run.facts, "hypotheses": run.hypotheses,
                     "rule_triggers": run.rule_triggers, "sql": run.sql,
                     "row_count": run.row_count, "category": run.category,
                     "level": run.level, "supportive_of": run.supportive_of},
            evidence_ref=run.id, source_ref=run.question_id, created_by="junior")
        brain.submit(node.id, by="junior")
        node = brain.approve(node.id, by="junior",
                             notes="auto: low-level exploratory (CP-15)")
        self.obs.event(tenant_id=tid, stage="junior.autopromote", actor="junior",
                       resource=node.id, status="OK",
                       meta={"run": run.id, "category": run.category})
        return node

    def resolve_sql(self, statement: Dict[str, Any]) -> str:
        """Prefer a reproducible approved query (no Metabase template tags); else
        a safe single SELECT built from the statement's columns."""
        if statement.get("sql"):
            return statement["sql"]
        try:
            rows = self.junior.approved_queries(self.tenant_id, limit=200)
            for n in rows:
                sql = (n.payload or {}).get("sql", "")
                if sql and "{{" not in sql:  # reproducible: skip {{tag}} templating
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
                  now: Optional[float] = None,
                  force: bool = False) -> Dict[str, Any]:
        """Execute at most one problem statement, serially. May skip.

        `force=True` is the test/operator lever: it relaxes ONLY the clock window +
        the 1/hr rate so a human can drive a controlled run out-of-hours. The
        `junior.enabled` gate and the serial single-flight lock are always honoured.
        """
        t0 = time.perf_counter()
        now = now if now is not None else self._clock()
        tid = tenant_id or self.tenant_id
        # Defend the class invariant: this worker's self.store was resolved once,
        # in __init__, for self.tenant_id only (see the constructor's comment). A
        # caller passing a different tenant_id here would otherwise have every
        # subsequent read/write in this method use that OTHER tenant's identity
        # (tid) while physically hitting THIS worker's store -- a cross-tenant
        # write. Refuse rather than silently writing to the wrong database.
        if tid != self.tenant_id:
            logger.warning(
                "run_cycle called for tenant %r but this worker is bound to "
                "tenant %r; refusing rather than writing to the wrong store",
                tid, self.tenant_id)
            return {"ran": False, "tenant_id": tid, "reason": "wrong_tenant"}
        # R6 gate: the operator can turn the junior analyst off entirely. When off,
        # the worker never asks/solves, regardless of window/rate.
        try:
            junior_enabled = bool(self.junior.tenants.get_analyst_config(tid).junior.enabled)
        except Exception:  # noqa: BLE001 - default to enabled on any config read issue
            junior_enabled = True
        if not junior_enabled:
            return {"ran": False, "tenant_id": tid, "in_window": True,
                    "rate_ok": True, "reason": "junior_disabled"}
        win = True if force else self.in_window(now)
        rate_ok = True if force else (
            _mins_since(self._last_ran_ts(), now) >= self.min_interval_minutes)
        daily_ok = True if force else (self._runs_today(now) < self.daily_cap)
        if not win:
            return {"ran": False, "tenant_id": tid, "in_window": False,
                    "rate_ok": rate_ok, "daily_ok": daily_ok,
                    "reason": "outside_window",
                    "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2)}
        if not rate_ok:
            return {"ran": False, "tenant_id": tid, "in_window": True,
                    "rate_ok": False, "daily_ok": daily_ok,
                    "reason": "rate_limited",
                    "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2)}
        if not daily_ok:
            return {"ran": False, "tenant_id": tid, "in_window": True,
                    "rate_ok": True, "daily_ok": False,
                    "reason": "daily_cap",
                    "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2)}
        # CP-14 review-backlog gate: don't keep generating while humans are backed
        # up. Once `review_backlog_max` completed analyses await senior review, the
        # junior pauses (force bypasses for tests) until the inbox drains.
        pending = self._pending_review_count(tid)
        if not force and pending >= self.review_backlog_max:
            return {"ran": False, "tenant_id": tid, "in_window": True,
                    "rate_ok": True, "daily_ok": True,
                    "review_backlog": pending, "review_backlog_max": self.review_backlog_max,
                    "reason": "review_backlog",
                    "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2)}

        statement = self.pick_problem_statement()
        question = statement.get("question") or "Baseline: refresh domain understanding"
        sql = self.resolve_sql(statement)
        self.obs.event(tenant_id=tid, stage="junior.bg_started", actor="junior",
                       resource=question[:60], status="OK")
        result = None  # analysis path reads it after the serial block

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

        analysis = self._baseline_analysis(error, ok)
        if ok and getattr(result, "data", None) is not None and not result.data.empty:
            analysis = self._analyze(tid, question, sql, result.data)
            llm = None
            try:
                cfg = self.junior.tenants.get_analyst_config(tid)
                if cfg.junior.enabled:
                    from .llm.client import make_role_client
                    llm = make_role_client(getattr(self.junior, "settings", None), cfg.junior)
            except Exception:  # noqa: BLE001 - best-effort
                llm = None
            if llm is not None and getattr(llm, "name", "null") != "null":
                llm_insight = synthesize_analysis_llm(
                    analysis.get("profile", {}), analysis.get("rule_triggers", []),
                    question, analysis["insights"], analysis["assumptions"], llm)
                if llm_insight:
                    analysis["insights"].append(
                        {"text": llm_insight, "novel": True, "kind": "llm"})

        self._record_ran(now)  # each standalone attempt consumes the 1/hr + 3/day caps
        run = self._save_run(tid, question, sql, ok, row_count, error, t0, statement, analysis)
        promoted = bool(run and run.status == RunStatus.COMPLETED
                        and self._maybe_autopromote(tid, run))
        # CP-15: a completed high-level run may spawn on-the-spot supporting
        # low-level probes (exempt from the caps; separate workpapers).
        supporting: List[str] = []
        if run and run.status == RunStatus.COMPLETED and (run.level or "low") == "high":
            supporting = self._run_supporting(tid, run, t0)
        self.obs.event(tenant_id=tid, stage="junior.bg_completed", actor="junior",
                       resource=question[:60], status="OK" if ok else "FAILED",
                       duration_ms=(time.perf_counter() - t0) * 1000.0,
                       meta={"sql": sql, "row_count": row_count,
                             "run_id": run.id if run else None, "level": statement.get("level"),
                             "promoted": promoted, "supporting": len(supporting),
                             "error": error[:200] if error else ""})
        return {"ran": True, "tenant_id": tid, "in_window": win, "rate_ok": True,
                "run_id": run.id if run else None, "question": question, "sql": sql, "ok": ok,
                "row_count": row_count, "error": error, "source": statement.get("source"),
                "level": (run.level if run else None), "category": (run.category if run else None),
                "promoted": promoted, "supporting": supporting,
                "insights": len(analysis["insights"]),
                "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2)}

    def _baseline_analysis(self, error: str, ok: bool) -> Dict[str, Any]:
        """Minimal analysis for a run that produced no usable DataFrame."""
        return {"facts": [], "insights": [], "assumptions": [], "next_actions": [],
                "uncertainties": [error or "Empty / non-DataFrame result."],
                "profile": {"error": error} if not ok else {}, "rule_triggers": [],
                "summary": "Query did not yield an analyzable result set."}

    def _analyze(self, tid: str, question: str, sql: str, df: Any) -> Dict[str, Any]:
        """Run the shared deterministic analysis (insights / actionables /
        assumptions) and check novelty against the tenant's approved Brain FINDINGs."""
        from .brain.store import CompanyBrain
        profile = profile_df(df, title=question)
        rules = evaluate_rules(df)
        existing: Optional[List[str]] = None
        try:
            existing = [n.title for n in CompanyBrain(self.store, tid).all(limit=500)
                        if getattr(n, "kind", None) == NodeKind.FINDING
                        and getattr(getattr(n, "status", None), "is_usable", lambda: False)()]
        except Exception:  # noqa: BLE001 - novelty is best-effort
            existing = None
        frame = analyze_results(df, sql, question, rules=rules, existing=existing,
                                row_limit=self.row_limit)
        frame["profile"] = profile
        frame["rule_triggers"] = rules
        return frame

    def _run_supporting(self, tid: str, parent: AnalysisRun, t0: float) -> List[str]:
        """On-the-spot low-level supporting probes for a high-level analysis (CP-15).

        Each probe is `supportive_of=<parent>`: EXEMPT from the 1/hr + 3/day caps,
        EXEMPT from the don't-re-ask dedup, and written as a separate workpaper
        under the parent's namespace — never an independent review item.
        Bounded by `supporting_cap`; best-effort and serial.
        """
        if self.supporting_cap <= 0:
            return []
        suggestions: List[Dict[str, Any]] = []
        try:
            s = self.junior.suggest_questions(self.tenant_id, limit_per_target=1)
            suggestions = [x for x in s.get("suggestions", [])
                           if (x.get("level") or "low") == "low" and x.get("question")]
        except Exception:  # noqa: BLE001 - best-effort
            suggestions = []
        ids: List[str] = []
        for sg in suggestions[:self.supporting_cap]:
            q = sg["question"]
            sql = sg.get("sql") or self.resolve_sql(sg)
            t1 = time.perf_counter()
            ok, row_count, error = False, 0, ""
            result = None
            with self._lock:
                try:
                    result = self.junior.executor.execute(
                        sql, ExecutionContext(tenant_id=tid, question=q,
                                              trace_id=new_trace(), row_limit=50000,
                                              dialect="athena"))
                    ok, row_count = bool(result.ok), int(result.row_count or 0)
                    if not ok:
                        error = result.error or ""
                except Exception as e:  # never kill the worker
                    error = str(e)
            analysis = self._baseline_analysis(error, ok)
            if ok and getattr(result, "data", None) is not None and not result.data.empty:
                analysis = self._analyze(tid, q, sql, result.data)
            run = self._save_run(tid, q, sql, ok, row_count, error, t1,
                                 {**sg, "level": "low", "category": sg.get("category")},
                                 analysis, supportive_of=parent.id)
            if run:
                self._maybe_autopromote(tid, run)
                ids.append(run.id)
                self.obs.event(tenant_id=tid, stage="junior.supporting", actor="junior",
                               resource=run.id, status="OK",
                               meta={"parent": parent.id, "category": run.category})
        return ids

    def _save_run(self, tid: str, question: str, sql: str, ok: bool,
                  row_count: int, error: str, t0: float,
                  statement: Dict[str, Any],
                  analysis: Dict[str, Any],
                  supportive_of: Optional[str] = None) -> Optional[AnalysisRun]:
        """Persist a full analysis run (CP-12/15).

        CP-15 routing:
          * low-level, analyzable, no anomaly          -> auto-accepted; promoted to
            a FINDING in `run_cycle` (bounded by `autopromote_cap`), not in the inbox;
          * low-level with HIGH/CRITICAL rule trigger  -> REQUIRES_SENIOR_REVIEW (inbox);
          * high-level (hypothesis/RCA, depth 2)       -> REQUIRES_SENIOR_REVIEW (inbox);
          * supporting workpapers (`supportive_of`)    -> APPROVED, never in the inbox;
          * schema/EDA probes / failures               -> recorded, never promoted.
        """
        status = RunStatus.COMPLETED if ok else RunStatus.FAILED
        anomalies = [r for r in analysis.get("rule_triggers", [])
                     if r.get("severity") in ("CRITICAL", "HIGH")]
        level = statement.get("level") or "low"
        low = (level == "low")
        category = statement.get("category")
        if supportive_of:
            review_status, answer_mode = (ReviewStatus.APPROVED,
                                          AnswerMode.NEW_LOW_RISK_ANALYSIS)
        elif anomalies:
            review_status, answer_mode = (ReviewStatus.CANDIDATE,
                                          AnswerMode.REQUIRES_SENIOR_REVIEW)
        elif not low:
            review_status, answer_mode = (ReviewStatus.CANDIDATE,
                                          AnswerMode.REQUIRES_SENIOR_REVIEW)
        elif ok and row_count:
            review_status, answer_mode = (ReviewStatus.APPROVED,
                                          AnswerMode.NEW_LOW_RISK_ANALYSIS)
        else:
            review_status, answer_mode = (ReviewStatus.CANDIDATE,
                                          AnswerMode.NEW_LOW_RISK_ANALYSIS)
        run = AnalysisRun(
            id=new_id("run"), tenant_id=tid, trace_id=new_trace(), question_id="",
            question_text=question, sql=sql, dialect="athena", executor="junior-bg",
            status=status, answer_mode=answer_mode, review_status=review_status,
            level=level, category=category, supportive_of=supportive_of,
            execution_ms=round((time.perf_counter() - t0) * 1000.0, 2),
            row_count=row_count,
            profile_summary=analysis.get("profile", {}),
            rule_triggers=analysis.get("rule_triggers", []),
            answer=analysis.get("summary", ""),
            facts=analysis.get("facts", []),
            hypotheses=analysis.get("hypotheses", []),
            uncertainties=analysis.get("uncertainties", []),
            next_actions=analysis.get("next_actions", []),
            insights=analysis.get("insights", []),
            assumptions=analysis.get("assumptions", []),
            cost_estimate=0.0,
            policy_reasons=[],
            source_node_ids=[],
        )
        try:
            self.store.execute(
                "INSERT INTO analysis_runs (id,tenant_id,trace_id,question_id,question_text,"
                "sql,dialect,executor,status,answer_mode,review_status,generated_at,"
                "execution_ms,row_count,profile_summary,rule_triggers,answer,facts,"
                "hypotheses,uncertainties,next_actions,insights,assumptions,"
                "level,category,supportive_of,cost_estimate,policy_reasons,source_node_ids) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run.id, run.tenant_id, run.trace_id, run.question_id, run.question_text,
                 run.sql, run.dialect, run.executor, run.status.value,
                 run.answer_mode.value, run.review_status.value, run.generated_at,
                 run.execution_ms, run.row_count, dump_json(run.profile_summary),
                 dump_json(run.rule_triggers), run.answer, dump_json(run.facts),
                 dump_json(run.hypotheses), dump_json(run.uncertainties),
                 dump_json(run.next_actions), dump_json(run.insights),
                 dump_json(run.assumptions), run.level, run.category, run.supportive_of,
                 run.cost_estimate, dump_json(run.policy_reasons),
                 dump_json(run.source_node_ids)))
            if supportive_of:
                write_workpaper_md(run, self.reviews_dir)
            else:
                write_analysis_md(run, self.reviews_dir)
        except Exception:  # noqa: BLE001 - persisting the run must never break the worker
            return None
        return run


__all__ = ["JuniorWorker"]