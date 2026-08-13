"""Orchestration pipeline: question -> plan -> policy -> execute -> analyze -> persist.

Human-in-the-loop by design: novel analyses and anomalies land as CANDIDATE /
REQUIRES_SENIOR_REVIEW and are only promoted to the Brain by an authorized
"senior" review. Approved-query reuse is the only auto-answer path.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional

from .analysis import analyze_results, evaluate_rules, profile_df, synthesize_analysis_llm
from .brain.store import CompanyBrain
from .config import Settings
from .database import Store, dump_json, load_json
from .domain import (AnswerMode, AnalysisRun, KnowledgeNode, NodeKind, Question,
                     ReviewStatus, RunStatus, new_id)
from .execution.base import ExecutionContext, QueryExecutor
from .execution.policy import QueryPolicy
from .llm.client import LLMClient, make_client
from .observability import Observability, new_trace
from .stores import TenantStoreProvider
from .tenancy import TenantService

BrainFactory = Callable[[Store, str], CompanyBrain]


class Pipeline:
    def __init__(self, stores: TenantStoreProvider, settings: Optional[Settings] = None,
                 tenant_service: Optional[TenantService] = None,
                 brain_factory: Optional[BrainFactory] = None,
                 executor: Optional[QueryExecutor] = None,
                 llm: Optional[LLMClient] = None,
                 observability: Optional[Observability] = None):
        self.stores = stores
        self.settings = settings or Settings()
        self.tenants = tenant_service or TenantService(stores)
        self.brain_factory = brain_factory if brain_factory is not None else (
            lambda s, t: CompanyBrain(s, t))
        self.executor = executor
        self.llm = llm or make_client(self.settings.llm_provider,
                                      self.settings.llm_model,
                                      self.settings.effective_api_key(),
                                      self.settings.ollama_base_url)
        self.policy = QueryPolicy(self.settings.policy)
        self.obs = observability or Observability(stores)

    def brain(self, tenant_id: str) -> CompanyBrain:
        self.tenants.require_tenant(tenant_id)
        return self.brain_factory(self.stores.for_tenant(tenant_id), tenant_id)

    # ------------------------------------------------------------------ #
    def run(self, tenant_id: str, question_text: str, mode_budget: str = "low_cost",
            persisted_sql: Optional[str] = None) -> AnalysisRun:
        store = self.stores.for_tenant(tenant_id)
        trace = new_trace()
        q = Question(id=new_id("q"), tenant_id=tenant_id, text=question_text,
                     mode_budget=mode_budget)
        store.execute("INSERT INTO questions (id,tenant_id,text,mode_budget,created_at) "
                      "VALUES (?,?,?,?,?)",
                      (q.id, q.tenant_id, q.text, q.mode_budget, q.created_at))

        run = AnalysisRun(id=new_id("run"), tenant_id=tenant_id, trace_id=trace,
                          question_id=q.id, question_text=question_text,
                          sql=persisted_sql or "", dialect=self.settings.source_dialect,
                          executor="unknown")
        self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="question.submitted",
                       actor="stakeholder", resource=q.id)

        brain = self.brain(tenant_id)

        # --- PLAN ----------------------------------------------------------
        with self.obs.span(tenant_id=tenant_id, trace_id=trace, stage="planning",
                           actor="junior", resource=question_text[:60]):
            approved = brain.search(question_text, kind=NodeKind.QUERY, usable_only=True)
            if approved:
                node = approved[0]
                run.sql = node.payload.get("sql", persisted_sql or "")
                run.source_node_ids = [node.id]
                run.answer_mode = AnswerMode.DIRECT_FROM_APPROVED_KNOWLEDGE
                run.executor = "approved-knowledge"
            elif persisted_sql:
                run.sql = persisted_sql
                run.answer_mode = AnswerMode.NEW_LOW_RISK_ANALYSIS
            else:
                run.answer_mode = AnswerMode.REQUIRES_SENIOR_REVIEW
                run.review_status = ReviewStatus.CANDIDATE

        if not run.sql:
            run.status = RunStatus.PLANNED
            self._save_run(run)
            return run

        # --- POLICY ---------------------------------------------------------
        with self.obs.span(tenant_id=tenant_id, trace_id=trace, stage="query.policy",
                           actor="policy", resource=run.sql[:60]):
            decision = self.policy.validate(run.sql,
                                            allowed_tables=self._allowed_tables(tenant_id),
                                            dialect=run.dialect)
        if decision.denied:
            run.status = RunStatus.POLICY_REJECTED
            run.policy_reasons = decision.reasons
            run.review_status = ReviewStatus.CANDIDATE
            self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="query.policy_rejected",
                           actor="policy", resource=run.id, status="REJECTED")
            self._save_run(run)
            return run
        run.sql = decision.approved_sql or run.sql

# <<P2>>

        # --- EXECUTE ---------------------------------------------------------
        self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="query.execution_started",
                       actor="executor", resource=run.id)
        result = self.executor.execute(run.sql, ExecutionContext(
            tenant_id=tenant_id, question=question_text, trace_id=trace,
            row_limit=self.settings.policy.default_row_limit, dialect=run.dialect))
        run.execution_ms = result.execution_ms
        if not result.ok:
            run.status = RunStatus.FAILED
            run.profile_summary = {"error": result.error}
            run.uncertainties = [result.error]
            self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="query.execution_failed",
                           actor="executor", resource=run.id, status="FAILED",
                           duration_ms=result.execution_ms)
            self._save_run(run)
            return run
        df = result.data
        run.row_count = result.row_count
        run.executor = type(self.executor).__name__ if self.executor is not None else "unknown"
        self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="query.execution_completed",
                       actor="executor", resource=run.id, duration_ms=result.execution_ms,
                       bytes_in=len(df) if df is not None else 0)

        # --- ANALYZE ---------------------------------------------------------
        with self.obs.span(tenant_id=tenant_id, trace_id=trace, stage="analysis",
                           actor="analyst"):
            profile = profile_df(df, title=question_text)
            rules = evaluate_rules(df)
            existing = [n.title for n in self.brain(tenant_id).all(limit=500)
                        if n.kind == NodeKind.FINDING and n.status.is_usable()]
            frame = analyze_results(df, run.sql, question_text, rules=rules,
                                    existing=existing,
                                    row_limit=self.settings.policy.default_row_limit)
        run.profile_summary = profile
        run.rule_triggers = rules
        run.answer = frame["summary"]
        run.facts = frame["facts"]
        run.hypotheses = frame["hypotheses"]
        run.uncertainties = list(frame["uncertainties"])
        run.next_actions = frame["actionables"]
        run.insights = frame["insights"]
        run.assumptions = frame["assumptions"]
        if getattr(self.llm, "name", "null") != "null":  # optional LLM narration
            llm_insight = synthesize_analysis_llm(
                profile, rules, question_text, run.insights, run.assumptions, self.llm)
            if llm_insight:
                run.insights.append({"text": llm_insight, "novel": True, "kind": "llm"})

        has_anomaly = any(r["severity"] in ("CRITICAL", "HIGH") for r in rules)
        if has_anomaly:
            run.review_status = ReviewStatus.CANDIDATE   # block auto-promotion
            run.answer_mode = AnswerMode.REQUIRES_SENIOR_REVIEW
        elif run.answer_mode is None:
            run.answer_mode = AnswerMode.NEW_LOW_RISK_ANALYSIS
        run.status = RunStatus.COMPLETED
        self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="analysis.completed",
                       actor="analyst", resource=run.id, status="OK")
        self._save_run(run)
        from .markdown import write_analysis_md  # per-analysis .md for human review (R3)
        write_analysis_md(run, os.path.join(os.path.dirname(self.settings.resolve_db_path()), "reviews"))
        return run
# <<P3>>
    # ------------------------------------------------------------------ #
    def _allowed_tables(self, tenant_id: str) -> Optional[List[str]]:
        sources = self.tenants.list_datasources(tenant_id)
        tables: List[str] = []
        for s in sources:
            tables.extend(s.get("tables", []))
        return tables or None

    def _save_run(self, run: AnalysisRun) -> None:
        self.stores.for_tenant(run.tenant_id).execute(
            "INSERT INTO analysis_runs (id,tenant_id,trace_id,question_id,question_text,sql,"
            "dialect,executor,status,answer_mode,review_status,generated_at,execution_ms,row_count,"
            "profile_summary,rule_triggers,answer,facts,hypotheses,uncertainties,next_actions,"
            "insights,assumptions,cost_estimate,policy_reasons,source_node_ids) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, answer_mode=excluded.answer_mode, "
            "review_status=excluded.review_status, execution_ms=excluded.execution_ms, "
            "row_count=excluded.row_count, profile_summary=excluded.profile_summary, "
            "rule_triggers=excluded.rule_triggers, answer=excluded.answer, facts=excluded.facts, "
            "hypotheses=excluded.hypotheses, uncertainties=excluded.uncertainties, "
            "next_actions=excluded.next_actions, insights=excluded.insights, "
            "assumptions=excluded.assumptions, policy_reasons=excluded.policy_reasons, "
            "source_node_ids=excluded.source_node_ids",
            (run.id, run.tenant_id, run.trace_id, run.question_id, run.question_text, run.sql,
             run.dialect, run.executor, run.status.value, run.answer_mode.value if run.answer_mode else None,
             run.review_status.value, run.generated_at, run.execution_ms, run.row_count,
             dump_json(run.profile_summary), dump_json(run.rule_triggers), run.answer,
             dump_json(run.facts), dump_json(run.hypotheses), dump_json(run.uncertainties),
             dump_json(run.next_actions), dump_json(run.insights), dump_json(run.assumptions),
             run.cost_estimate, dump_json(run.policy_reasons),
             dump_json(run.source_node_ids)))

    def get_run(self, tenant_id: str, run_id: str) -> Optional[AnalysisRun]:
        row = self.stores.for_tenant(tenant_id).query_one(
            "SELECT * FROM analysis_runs WHERE id=? AND tenant_id=?", (run_id, tenant_id))
        return self._run_from_row(row) if row else None

    def _run_from_row(self, row) -> AnalysisRun:
        r = dict(row)
        r["status"] = RunStatus(r["status"]) if r["status"] else RunStatus.PLANNED
        r["answer_mode"] = AnswerMode(r["answer_mode"]) if r["answer_mode"] else None
        r["review_status"] = ReviewStatus(r["review_status"]) if r["review_status"] else ReviewStatus.CANDIDATE
        r["profile_summary"] = load_json(r["profile_summary"], {})
        r["rule_triggers"] = load_json(r["rule_triggers"], [])
        r["facts"] = load_json(r["facts"], [])
        r["hypotheses"] = load_json(r["hypotheses"], [])
        r["uncertainties"] = load_json(r["uncertainties"], [])
        r["next_actions"] = load_json(r["next_actions"], [])
        r["insights"] = load_json(r["insights"], [])
        r["assumptions"] = load_json(r["assumptions"], [])
        r["policy_reasons"] = load_json(r["policy_reasons"], [])
        r["source_node_ids"] = load_json(r["source_node_ids"], [])
        return AnalysisRun(**r)

    def list_runs(self, tenant_id: str, limit: int = 50) -> List[AnalysisRun]:
        rows = self.stores.for_tenant(tenant_id).query_all(
            "SELECT * FROM analysis_runs WHERE tenant_id=? "
            "ORDER BY generated_at DESC LIMIT ?", (tenant_id, limit))
        return [self._run_from_row(r) for r in rows]
# <<P4>>
    # ------------------------------------------------------------------ #
    def register_approved_query(self, tenant_id: str, sql: str, title: str,
                                summary: str = "", by: str = "admin",
                                source_ref: str = "") -> KnowledgeNode:
        """Seed a trusted query (e.g. from onboarding / legacy review). Instant approve."""
        brain = self.brain(tenant_id)
        node = brain.create(NodeKind.QUERY, title=title, summary=summary,
                            payload={"sql": sql, "structure": {"read_only": True}},
                            evidence_ref="", source_ref=source_ref, created_by=by)
        brain.submit(node.id, by=by)
        brain.approve(node.id, by=by, notes="seeded as trusted golden query")
        self.obs.event(tenant_id=tenant_id, stage="knowledge.promoted", actor=by,
                       resource=node.id, status="OK")
        return node

    def promote_finding(self, tenant_id: str, run_id: str, by: str = "senior",
                        notes: str = "") -> Optional[KnowledgeNode]:
        run = self.get_run(tenant_id, run_id)
        if run is None or run.status != RunStatus.COMPLETED:
            return None
        brain = self.brain(tenant_id)
        node = brain.create(NodeKind.FINDING, title=run.question_text,
                            summary=run.answer,
                            payload={"facts": run.facts, "hypotheses": run.hypotheses,
                                     "rule_triggers": run.rule_triggers,
                                     "sql": run.sql, "row_count": run.row_count},
                            evidence_ref=run.id, source_ref=run.question_id,
                            created_by=by)
        brain.submit(node.id, by=by)
        node = brain.approve(node.id, by=by, notes=notes or "finding approved after senior review")
        self.obs.event(tenant_id=tenant_id, stage="knowledge.promoted", actor=by,
                       resource=node.id, status="OK")
        return node