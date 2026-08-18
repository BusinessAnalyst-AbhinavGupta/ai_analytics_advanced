"""P6 — Stakeholder analyst: low-cost, ad-hoc answers over the governed Brain.

Modeled on the plan's low-cost stakeholder analyst. It classifies the question,
retrieves **approved** knowledge first, refreshes/adapts an accepted query,
routes to a low-cost model when one is configured, attaches citations +
freshness + caveats, escalates high-risk questions instead of auto-answering, and
records feedback so answer quality is measurable as a platform metric.

Exit criteria this implements: repeated questions reuse approved knowledge;
high-risk items escalate to the senior inbox; answers carry evidence + freshness;
cost per answer is tracked.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .base_view import BaseViewRegistry
from .brain.embedding import Embedder
from .brain.index import BrainIndex
from .brain.store import CompanyBrain
from .config import Settings
from .database import Store, dump_json, load_json
from .data_manager import CoverageVerdict, DataManager, DataRequirement
from .domain import (AnswerMode, AttributionRule, BaseView, CubeMeasure, CubeSpec,
                     NodeKind, TurnPlan, new_id, now_iso)
from .execution.base import ExecutionContext
from .execution.dataframe_cache import ConversationDataCache
from .execution.extract_store import ExtractMeta, ExtractStore
from .execution.policy import QueryPolicy, resolve_template_placeholders
from .execution.python_policy import PythonCodePolicy
from .execution.python_sandbox import (EXTRACT_MEMORY_MB, EXTRACT_TIMEOUT_S,
                                       run_python_sandboxed)
from .execution.workspace import AnalyticalWorkspace
from .llm.client import make_role_client
from .junior import JuniorEngine
from .observability import Observability, new_trace
from .schema_context import SchemaContext, SchemaContextBuilder
from .semantic import SemanticLayer
from .stores import TenantStoreProvider
from .tenancy import TenantService
from .skills import SkillRegistry, SkillEngine

logger = logging.getLogger(__name__)

CATEGORY_MARKERS: Dict[str, List[str]] = {
    "metric_lookup": ["how many", "count", "number of", "what is the value", "measure", "metric"],
    "trend": ["trend", "over time", "month over month", "week over week", "growth", "seasonal"],
    "comparison": ["compare", "versus", "vs ", "difference", "which is higher", "across"],
    "definition": ["what does", "what is", "defin", "meaning of", "how is it defined"],
    "process": ["why", "how do we", "process", "workflow", "cause"],
}
HIGH_RISK_MARKERS: List[str] = [
    "pii", "personally identifiable", "password", "credential", "secret", "salary",
    "ssn", "national id", "credit card", "bank account", "sensitive", "personal data",
    "gdpr", "compliance",
]


def _permissive_profile(column: str):
    """A stand-in profile used only when RE-composing a cube whose dimensions the
    guard has already approved. It must never be used for a first composition --
    that is what makes the guard fail closed on unprofiled columns."""
    from .domain import ColumnProfile
    return ColumnProfile(column=column, dtype="object", distinct_count=1,
                         null_fraction=0.0, values=[], values_complete=False)


def _parse_json_block(text: str, context: str = "") -> Optional[Dict[str, Any]]:
    """Pull a JSON object out of an LLM response, tolerating ```json fences."""
    import json
    body = (text or "").strip()
    if "```json" in body:
        body = body.split("```json")[1].split("```")[0].strip()
    elif "```" in body:
        body = body.split("```")[1].strip()
    try:
        parsed = json.loads(body)
    except (ValueError, IndexError):
        logger.warning("could not parse %s as JSON: %r", context or "LLM response",
                       (text or "")[:400])
        return None
    return parsed if isinstance(parsed, dict) else None


class StakeholderService:
    def __init__(self, stores: TenantStoreProvider, tenants: Optional[TenantService] = None,
                 executor: Optional[Any] = None,
                 observability: Optional[Observability] = None,
                 cost_per_1k_input: float = 0.30,
                 cost_per_1k_output: float = 1.20,
                 settings: Optional[Settings] = None,
                 embedder: Optional[Embedder] = None):
        from .execution.sampler import SamplerExecutor
        self.stores = stores
        self.tenants = tenants or TenantService(stores)
        self.executor = executor or SamplerExecutor()
        self.obs = observability or Observability(stores)
        self.settings = settings or Settings()
        # The analytical workspace. Built once per service, not per turn: a
        # per-turn ExtractStore would be harmless but a per-turn DuckDB
        # connection would throw away every registered view between questions.
        self.extract_store = ExtractStore(self.settings.resolve_tenants_root())
        self.data_cache = ConversationDataCache(store=self.extract_store)
        self.workspace = AnalyticalWorkspace(self.extract_store, self.settings.policy)
        self.semantic = SemanticLayer(self.brain)
        self.base_views = BaseViewRegistry(self.brain)
        self.data_manager = DataManager(self.data_cache, self.workspace, self.settings)
        self.junior = JuniorEngine(stores, executor=self.executor, tenants=self.tenants,
                                   observability=self.obs, settings=self.settings,
                                   embedder=embedder)
        self.schema_context = SchemaContextBuilder(
            self.junior, self.brain, self.settings, self.semantic, self.base_views)
        self.cost_per_1k_input = cost_per_1k_input
        self.cost_per_1k_output = cost_per_1k_output
        self.embedder = embedder
        self.skill_registry = SkillRegistry()
        self.skill_registry.load_skills()
        self.skill_engine = SkillEngine()

    def brain(self, tenant_id: str) -> CompanyBrain:
        # The index is bound to one tenant's database, so it is built where that
        # store is resolved. The embedder is the expensive part and is shared.
        store = self.stores.for_tenant(tenant_id)
        return CompanyBrain(store, tenant_id,
                            index=BrainIndex(store, embedder=self.embedder))

    def classify(self, question: str) -> str:
        q = question.lower()
        for cat, markers in CATEGORY_MARKERS.items():
            if any(m in q for m in markers):
                return cat
        return "uncategorized"

    def is_high_risk(self, question: str, category: str) -> bool:
        """High-risk questions escalate instead of being auto-answered."""
        q = question.lower()
        if any(m in q for m in HIGH_RISK_MARKERS):
            return True
        return category in ("metric_lookup", "trend", "comparison") and "revenue" in q

    def _extract_search_intent(self, llm: Any, question: str) -> str:
        """Distill a verbose question into a generic 2-4 word retrieval topic.

        Applies to any retrieval backend: this only shapes the query string
        handed to CompanyBrain.search(), not the search implementation itself.
        """
        if not self._llm_live(llm):
            return question
        prompt = (
            "Extract the core analytical topic from this question for a semantic vector search. "
            "Remove specific filters (like dates, countries, service lines, channels). "
            "Only return a generic 2-4 word concept.\n\n"
            f"Question: {question}\nCore Topic:"
        )
        res = llm.generate(prompt, temperature=0.0)
        return res.text.strip() if res.ok and res.text else question

    # -- retrieve ----------------------------------------------------------
    def _retrieve(self, tenant_id: str, question: str) -> Tuple[List[Any], List[Any]]:
        """Approved knowledge first: reusable QUERY nodes, else DEFINITION nodes."""
        brain = self.brain(tenant_id)
        q = brain.search(question, kind=NodeKind.QUERY, usable_only=True, limit=3)
        d = brain.search(question, kind=NodeKind.DEFINITION, usable_only=True, limit=3)
        return (q or []), (d or [])

    # -- conversations -------------------------------------------------------
    def _ensure_conversation(self, tenant_id: str, conversation_id: str, question: str) -> str:
        """Reuse an existing conversation if the caller supplied a valid id for
        this tenant; otherwise start a new one. Never raises on a stale/foreign
        id -- a deleted or mistyped conversation_id just starts a fresh thread."""
        store = self.stores.for_tenant(tenant_id)
        if conversation_id:
            row = store.query_one(
                "SELECT id FROM stakeholder_conversations WHERE id=? AND tenant_id=?",
                (conversation_id, tenant_id))
            if row:
                store.execute(
                    "UPDATE stakeholder_conversations SET updated_at=? WHERE id=? AND tenant_id=?",
                    (now_iso(), conversation_id, tenant_id))
                return conversation_id
            logger.warning(
                "stakeholder._ensure_conversation: conversation_id %r not found for "
                "tenant %s -- starting a new conversation", conversation_id, tenant_id)
        cid = new_id("conv")
        title = question.strip()[:80] or "New conversation"
        ts = now_iso()
        store.execute(
            "INSERT INTO stakeholder_conversations (id,tenant_id,title,starred,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)", (cid, tenant_id, title, 0, ts, ts))
        return cid

    def list_conversations(self, tenant_id: str) -> List[Dict[str, Any]]:
        store = self.stores.for_tenant(tenant_id)
        rows = store.query_all(
            "SELECT c.id, c.title, c.starred, c.created_at, c.updated_at, "
            "COUNT(a.id) AS message_count "
            "FROM stakeholder_conversations c "
            "LEFT JOIN stakeholder_answers a ON a.conversation_id = c.id AND a.tenant_id = c.tenant_id "
            "WHERE c.tenant_id=? GROUP BY c.id ORDER BY c.starred DESC, c.updated_at DESC",
            (tenant_id,))
        return [{"id": r["id"], "title": r["title"], "starred": bool(r["starred"]),
                 "created_at": r["created_at"], "updated_at": r["updated_at"],
                 "message_count": r["message_count"]} for r in rows]

    # NOTE: messages don't carry prior feedback ratings (no join against
    # stakeholder_feedback) -- reloading a conversation loses the thumbs-up/down
    # highlight even though the rating itself is correctly persisted. Similarly,
    # chart_config/chart_data aren't persisted/returned here, so a reloaded
    # thread shows the answer text but not its original chart. Follow-up.
    def get_conversation(self, tenant_id: str, conversation_id: str) -> Optional[Dict[str, Any]]:
        store = self.stores.for_tenant(tenant_id)
        conv = store.query_one(
            "SELECT id, title, starred, created_at, updated_at FROM stakeholder_conversations "
            "WHERE id=? AND tenant_id=?", (conversation_id, tenant_id))
        if not conv:
            return None
        rows = store.query_all(
            "SELECT * FROM stakeholder_answers WHERE conversation_id=? AND tenant_id=? "
            "ORDER BY created_at ASC", (conversation_id, tenant_id))
        messages = [{
            "answer_id": r["id"], "question": r["question"], "answer": r["answer"],
            "answer_mode": r["answer_mode"], "status": r["status"],
            "citations": load_json(r["citations"], []), "caveats": load_json(r["caveats"], []),
            "facts": load_json(r["facts"], []), "queries_run": load_json(r["queries_run"], []),
            "python_cells": load_json(r["python_cells"], []),
            "produced_df_label": r["produced_df_label"] or "",
            "escalated": bool(r["escalated"]), "cost": r["cost"], "created_at": r["created_at"],
        } for r in rows]
        return {"id": conv["id"], "title": conv["title"], "starred": bool(conv["starred"]),
                "created_at": conv["created_at"], "updated_at": conv["updated_at"],
                "messages": messages}

    def update_conversation(self, tenant_id: str, conversation_id: str,
                            title: Optional[str] = None,
                            starred: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        store = self.stores.for_tenant(tenant_id)
        row = store.query_one(
            "SELECT id FROM stakeholder_conversations WHERE id=? AND tenant_id=?",
            (conversation_id, tenant_id))
        if not row:
            return None
        if title is not None:
            store.execute(
                "UPDATE stakeholder_conversations SET title=?, updated_at=? WHERE id=? AND tenant_id=?",
                (title, now_iso(), conversation_id, tenant_id))
        if starred is not None:
            store.execute(
                "UPDATE stakeholder_conversations SET starred=?, updated_at=? WHERE id=? AND tenant_id=?",
                (int(starred), now_iso(), conversation_id, tenant_id))
        return self.get_conversation(tenant_id, conversation_id)

    def delete_conversation(self, tenant_id: str, conversation_id: str) -> bool:
        store = self.stores.for_tenant(tenant_id)
        row = store.query_one(
            "SELECT id FROM stakeholder_conversations WHERE id=? AND tenant_id=?",
            (conversation_id, tenant_id))
        if not row:
            return False
        store.execute(
            "DELETE FROM stakeholder_feedback WHERE tenant_id = ? AND answer_id IN "
            "(SELECT id FROM stakeholder_answers WHERE conversation_id = ? AND tenant_id = ?)",
            (tenant_id, conversation_id, tenant_id))
        store.execute("DELETE FROM stakeholder_answers WHERE conversation_id=? AND tenant_id=?",
                      (conversation_id, tenant_id))
        store.execute("DELETE FROM stakeholder_conversations WHERE id=? AND tenant_id=?",
                      (conversation_id, tenant_id))
        return True

    def _refresh(self, tenant_id: str, node: Any, question: str) -> Dict[str, Any]:
        ec = ExecutionContext(tenant_id=tenant_id, question=question,
                              dialect=node.payload.get("dialect", "athena"))
        sql, placeholders = resolve_template_placeholders(node.payload.get("sql", ""))
        if placeholders:
            logger.warning(
                "stakeholder._refresh: resolved template placeholder(s) %s in stored "
                "query %r to a permissive filter for verbatim reuse",
                sorted(set(placeholders)), node.id)
        result = self.executor.execute(sql, ec)
        if not result.ok:
            return {"ok": False, "error": result.error, "row_count": 0,
                    "execution_ms": result.execution_ms, "sql": sql,
                    "placeholders_resolved": placeholders}
        preview = []
        rows = result.data
        if rows is not None:
            try:
                preview = rows.head(3).to_dict(orient="records")
            except Exception:  # noqa: BLE001 - non-DataFrame result
                preview = list(rows)[:3]
        return {"ok": True, "row_count": result.row_count,
                "execution_ms": result.execution_ms, "preview": preview,
                "sql": sql, "placeholders_resolved": placeholders}

    # -- answer ------------------------------------------------------------
    def answer(self, tenant_id: str, question: str, user_id: str = "",
               conversation_id: str = "") -> Dict[str, Any]:
        self.tenants.require_tenant(tenant_id)
        conversation_id = self._ensure_conversation(tenant_id, conversation_id, question)
        trace = new_trace()
        category = self.classify(question)

        cfg = self.tenants.get_analyst_config(tenant_id)
        if not cfg.stakeholder.enabled:
            answer = "AI Stakeholder analyst is disabled for this tenant."
            out = self._record(tenant_id, question, user_id, category, trace, answer,
                               AnswerMode.CANNOT_ANSWER, "CANNOT_ANSWER", False, [],
                               caveats=["stakeholder analyst AI disabled in tenant configuration"],
                               conversation_id=conversation_id)
            self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.answer",
                           actor="stakeholder", resource=out["answer_id"], status="DISABLED",
                           meta={"category": category, "mode": AnswerMode.CANNOT_ANSWER.value})
            return out

        llm = make_role_client(self.settings, cfg.stakeholder)
        search_intent = self._extract_search_intent(llm, question)
        query_nodes, defn_nodes = self._retrieve(tenant_id, search_intent)

        if self.is_high_risk(question, category):
            source_ids = [n.id for n in (query_nodes + defn_nodes)]
            if source_ids:
                self.brain(tenant_id).submit(source_ids[0], by="stakeholder")
            out = self._record(tenant_id, question, user_id, category, trace, "",
                               AnswerMode.REQUIRES_SENIOR_REVIEW, "ESCALATED", True,
                               source_ids, caveats=["high-risk question matched escalation rules"],
                               queries_run=[n.payload.get("sql", "") for n in query_nodes],
                               conversation_id=conversation_id)
            self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.escalate",
                           actor="stakeholder", resource=out["answer_id"], status="OK",
                           meta={"category": category})
            return out

        if self._llm_live(llm) and conversation_id:
            plan = self._plan_turn(llm, tenant_id, conversation_id, question,
                                   query_nodes, defn_nodes)
            path, df_label = self._legacy_compute_path(plan)
            if path == "python":
                code, exec_res, toks = self._synthesize_and_execute_python(
                    llm, tenant_id, conversation_id, question, df_label)
                if exec_res is not None and exec_res.ok:
                    rows_for_context = (exec_res.result_summary
                                        if isinstance(exec_res.result_summary, list)
                                        else [exec_res.result_summary])
                    data_context = {"rows": rows_for_context}
                    answer, syn_toks, chart_config = self._synthesize(llm, question, category, data_context)
                    t_in = toks[0] + syn_toks[0]
                    t_out = toks[1] + syn_toks[1]

                    out = self._record(tenant_id, question, user_id, category, trace, answer,
                                       AnswerMode.ADAPTED_APPROVED_QUERY, "ANSWERED", False, [],
                                       facts=[f"computed via Python over cached data '{df_label}'"],
                                       caveats=["dynamically generated Python over previously-fetched data"],
                                       tokens_in=t_in, tokens_out=t_out,
                                       python_cells=[{"code": code, "df_label": df_label,
                                                      "result_summary": exec_res.result_summary}],
                                       conversation_id=conversation_id)
                    out["chart_config"] = chart_config
                    out["chart_data"] = rows_for_context
                    self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.answer",
                                   actor="stakeholder", resource=out["answer_id"], status="OK",
                                   meta={"category": category,
                                         "mode": AnswerMode.ADAPTED_APPROVED_QUERY.value,
                                         "compute": "python"})
                    return out
                # Every Python synthesis/policy/execution attempt failed --
                # fall through to the existing SQL path below exactly as if
                # routing had chosen "sql" in the first place.

        has_nodes = bool(query_nodes or defn_nodes)
        if has_nodes and self._llm_live(llm):
            sql, exec_res, toks = self._synthesize_and_execute_sql(
                llm, tenant_id, question, query_nodes, defn_nodes)
            if exec_res is not None and exec_res.ok:
                label = ""
                if exec_res.data is not None and conversation_id:
                    label = self.data_cache.next_label(tenant_id, conversation_id)
                    self.data_cache.put(tenant_id, conversation_id, label, question[:200], exec_res.data)
                preview = []
                if exec_res.data is not None:
                    try:
                        preview = exec_res.data.head(3).to_dict(orient="records")
                    except Exception:  # noqa: BLE001 - non-DataFrame result
                        preview = list(exec_res.data)[:3]
                data_context = {"rows": preview}
                answer, syn_toks, chart_config = self._synthesize(llm, question, category, data_context)
                t_in = toks[0] + syn_toks[0]
                t_out = toks[1] + syn_toks[1]

                citations = [{
                    "node_id": n.id,
                    "title": n.title,
                    "evidence_ref": n.evidence_ref,
                    "freshness": n.confidence.get("freshness", 0.0),
                } for n in (query_nodes + defn_nodes)]

                out = self._record(tenant_id, question, user_id, category, trace, answer,
                                   AnswerMode.ADAPTED_APPROVED_QUERY, "ANSWERED", False,
                                   [n.id for n in (query_nodes + defn_nodes)],
                                   citations=citations,
                                   facts=["synthesized custom query based on approved knowledge"],
                                   caveats=["dynamically generated SQL"],
                                   tokens_in=t_in, tokens_out=t_out, queries_run=[sql],
                                   produced_df_label=label,
                                   conversation_id=conversation_id)
                out["chart_config"] = chart_config
                out["chart_data"] = preview
                self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.answer",
                               actor="stakeholder", resource=out["answer_id"], status="OK",
                               meta={"category": category, "mode": AnswerMode.ADAPTED_APPROVED_QUERY.value})
                return out
            # Every synthesis/repair attempt failed validation or execution —
            # fall through to the verbatim-reuse path below rather than
            # surfacing a raw SQL error.

        if query_nodes:
            all_details = []
            queries_run = []
            citations = []
            facts = []
            caveats = ["values from approved queries at review time"]
            any_failed = False
            last_err = ""
            for q_node in query_nodes:
                refreshed = self._refresh(tenant_id, q_node, question)
                all_details.append(refreshed)
                if refreshed.get("sql"):
                    queries_run.append(refreshed["sql"])
                if refreshed.get("placeholders_resolved"):
                    caveats.append(
                        f"'{q_node.title}': template filter(s) "
                        f"{sorted(set(refreshed['placeholders_resolved']))} defaulted to no filter")
                if not refreshed["ok"]:
                    any_failed = True
                    last_err = refreshed["error"]
                else:
                    facts.append(f"reused approved query: {q_node.title}")
                    citations.append({
                        "node_id": q_node.id, 
                        "title": q_node.title,
                        "evidence_ref": q_node.evidence_ref,
                        "freshness": q_node.confidence.get("freshness", 0.0)
                    })
            
            if not any_failed:
                if len(query_nodes) == 1:
                    answer = f"Reused approved query '{query_nodes[0].title}' ({all_details[0]['row_count']} rows)."
                else:
                    answer = f"Reused {len(query_nodes)} approved queries."
                mode = AnswerMode.REFRESHED_APPROVED_QUERY

                chart_config = None
                t_in, t_out = 0, 0
                if self._llm_live(llm) and len(all_details) > 0:
                    data_arg = all_details[0].get("preview", [])
                    _, (t_in, t_out), chart_config = self._synthesize(llm, question, category, data_arg)

                out = self._record(tenant_id, question, user_id, category, trace, answer, mode,
                                   "ANSWERED", False, [n.id for n in query_nodes],
                                   citations, facts=facts, caveats=caveats,
                                   tokens_in=t_in, tokens_out=t_out, queries_run=queries_run,
                                   conversation_id=conversation_id)
                out["_detail"] = all_details
                out["chart_config"] = chart_config
                out["chart_data"] = all_details[0].get("preview", []) if all_details else []
                self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.answer",
                               actor="stakeholder", resource=out["answer_id"], status="OK",
                               meta={"category": category, "mode": mode.value})
                return out

            # Every matched approved query failed to execute. Semantic retrieval
            # surfaced queries that are topically similar but not actually
            # applicable to this question (wrong table, stale column, dialect
            # mismatch, or a transient executor error) -- that is a bad match,
            # not evidence the question is unanswerable. Don't terminate here:
            # fall through to definitions / skill-matching / freeform synthesis
            # below, same as when no approved queries matched at all, so a bad
            # retrieval hit degrades to a real analysis instead of a denial.
            logger.warning(
                "stakeholder.answer: all %d matched approved queries failed to execute "
                "for tenant %s; falling through past approved-query reuse instead of "
                "returning CANNOT_ANSWER. last_err=%s", len(query_nodes), tenant_id, last_err)
        def _recite_definitions():
            answers = []
            facts = []
            citations = []
            for d_node in defn_nodes:
                answers.append(f"{d_node.title}: {d_node.summary or ''}")
                if d_node.summary:
                    facts.append(d_node.summary)
                citations.append({
                    "node_id": d_node.id, "title": d_node.title,
                    "evidence_ref": d_node.evidence_ref,
                    "freshness": d_node.confidence.get("freshness", 0.0)
                })

            answer = "Definitions: " + " | ".join(answers)
            out = self._record(tenant_id, question, user_id, category, trace, answer,
                               AnswerMode.DIRECT_FROM_APPROVED_KNOWLEDGE, "ANSWERED", False,
                               [n.id for n in defn_nodes],
                               citations,
                               facts=facts,
                               caveats=["from approved definitions at review time"],
                               conversation_id=conversation_id)
            self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.answer",
                           actor="stakeholder", resource=out["answer_id"], status="OK",
                           meta={"category": category, "mode": AnswerMode.DIRECT_FROM_APPROVED_KNOWLEDGE.value})
            return out

        if defn_nodes and not self._llm_live(llm):
            # No live LLM means skill-matching and freeform synthesis are both
            # unreachable below anyway, so recite immediately as before.
            return _recite_definitions()

        if self._llm_live(llm):
            # Check for skill match
            skill_match = self.skill_engine.match(question, self.skill_registry.meta, llm)
            if skill_match:
                skill = self.skill_registry.get_skill(skill_match.skill_name)
                if skill:
                    params, needs_clarif, clarif_q = self.skill_engine.extract_params(question, skill, llm)
                    if needs_clarif:
                        out = self._record(tenant_id, question, user_id, category, trace, clarif_q,
                                           AnswerMode.NEEDS_CLARIFICATION, "NEEDS_CLARIFICATION", False, [],
                                           caveats=["missing required parameters for skill: " + skill.meta.name],
                                           conversation_id=conversation_id)
                        self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.answer",
                                       actor="stakeholder", resource=out["answer_id"], status="OK",
                                       meta={"category": category, "mode": out["answer_mode"]})
                        return out
                    
                    ec = ExecutionContext(tenant_id=tenant_id, question=question, dialect="athena")
                    exec_res = self.skill_engine.execute(skill, params, self.executor, ec)
                    
                    if not exec_res.ok:
                        out = self._record(tenant_id, question, user_id, category, trace, "Skill execution failed: " + exec_res.error,
                                           AnswerMode.CANNOT_ANSWER, "CANNOT_ANSWER", False, [],
                                           queries_run=exec_res.queries_run,
                                           caveats=["skill execution error"],
                                           conversation_id=conversation_id)
                        self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.answer",
                                       actor="stakeholder", resource=out["answer_id"], status="ERROR",
                                       meta={"category": category, "mode": out["answer_mode"]})
                        return out

                    # Synthesize final answer using data from execution
                    data_context = {"rows": [p["preview"] for p in exec_res.data_previews]}
                    answer, toks, chart_config = self._synthesize(llm, question, category, data_context)
                    
                    out = self._record(tenant_id, question, user_id, category, trace, answer,
                                       AnswerMode.SKILL_EXECUTED_ANALYSIS, "ANSWERED", False, [],
                                       queries_run=exec_res.queries_run,
                                       caveats=["used specialized skill: " + skill.meta.name],
                                       tokens_in=toks[0], tokens_out=toks[1],
                                       conversation_id=conversation_id)
                    out["chart_config"] = chart_config
                    out["chart_data"] = exec_res.data_previews[-1]["preview"] if exec_res.data_previews else []
                    self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.answer",
                                   actor="stakeholder", resource=out["answer_id"], status="OK",
                                   meta={"category": category, "mode": out["answer_mode"]})
                    return out

            # No skill matched (or the matched skill couldn't be loaded).
            # Brain retrieval is rank-only with no absolute relevance
            # threshold (see brain/fusion.py's RRF fusion) -- a retrieved
            # definition being "best of what's there" is not proof it
            # actually addresses this question. Don't let it preempt a
            # purpose-built skill's chance to run above; recite it here only
            # as a fallback, still ahead of generic freeform synthesis.
            if defn_nodes:
                return _recite_definitions()

            # Fallback to direct LLM synthesis if no skill matched
            answer, toks, chart_config = self._synthesize(llm, question, category)
            out = self._record(tenant_id, question, user_id, category, trace, answer,
                               AnswerMode.NEW_LOW_RISK_ANALYSIS, "ANSWERED", False, [],
                               caveats=["no approved knowledge in the Brain; generated answer"],
                               tokens_in=toks[0], tokens_out=toks[1],
                               conversation_id=conversation_id)
            out["chart_config"] = chart_config
            out["chart_data"] = []
        else:
            answer = ("I don't have an approved query or definition matching this question yet. "
                      "Rephrase, or ask the senior analyst.")
            out = self._record(tenant_id, question, user_id, category, trace, answer,
                               AnswerMode.CANNOT_ANSWER, "CANNOT_ANSWER", False, [],
                               caveats=["no approved knowledge matched"],
                               conversation_id=conversation_id)
        self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.answer",
                       actor="stakeholder", resource=out["answer_id"], status="OK",
                       meta={"category": category, "mode": out["answer_mode"]})
        return out

    # -- helpers -------------------------------------------------------------
    def _llm_live(self, llm: Optional[Any] = None) -> bool:
        client = llm if llm is not None else getattr(self, "llm", None)
        if client is None:
            return False
        return getattr(client, "name", "null") != "null"

    def _legacy_compute_path(self, plan: TurnPlan) -> Tuple[str, str]:
        """Bridge from TurnPlan to the pre-Task-14 two-way branch in answer().

        answer() is restructured into the full analyst pipeline in Task 14; until
        then a reuse verdict over a cached cube is the only case the old branch
        can express, and everything else falls through to the SQL path exactly as
        before.
        """
        if plan.path == "reuse" and plan.df_label:
            return "python", plan.df_label
        return "sql", ""

    # -- planning ------------------------------------------------------------
    # The old _choose_compute_path only ever picked between "python" and "sql",
    # saw nothing but column names, and short-circuited to "sql" whenever the
    # cache was empty -- which is exactly why a real run produced two warehouse
    # queries and no Python at all. It is replaced by a planning call that does
    # the three things only a model can do (pick the population, state the cut,
    # choose how to compute) and hands everything else to code.

    PLAN_SYSTEM_PROMPT = """You are the planner for an analytical turn. You do NOT write SQL.

Respond with STRICT JSON and nothing else:
{"base_view": "<name from the list above>",
 "propose_base_view": null,
 "cube": {"dimensions": [], "measures": [{"name": "", "expr": ""}],
          "filters": {}, "time_column": "", "time_start": "", "time_end": ""},
 "analysis": "workspace_sql" | "python",
 "aggregate_only": false,
 "attributions": [],
 "rationale": ""}

- Name exactly ONE base_view from the list above. Every number in your answer comes
  from that one population, which is what lets this answer be compared against
  earlier ones. Prefer an [APPROVED] view over a [DRAFT] one.
- propose_base_view: fill this in ONLY when no listed view can answer the question,
  and set base_view to the name you are proposing. It must be at ID grain -- one row
  per identifier (session_id, order_id, user_id), never one row per dimension
  combination. A base at dimensional grain is useless for the next question. Say in
  rationale why no existing view fits.
- cube.dimensions: the columns to GROUP BY, drawn only from the chosen base's listed
  dimension columns. Fewer is better: a cube is reusable for every question over a
  SUBSET of its dimensions and cheap to widen later, but a cube that is too large is
  refused outright. Do not add a dimension "in case it is useful".
- cube.measures: prefer SUM, COUNT(*), MIN, MAX. Ask for AVG(x) as a plain AVG(x) and
  it will be stored as a sum and a count for you. COUNT(DISTINCT x), medians and
  percentiles DO NOT roll up -- a cube carrying one can answer only at its own
  dimensions, so name them only when the question truly needs them, and say so in
  rationale.
- cube.filters are equality sets only, e.g. {"country": ["Germany"]}, taken from the
  EXACT literals and casing in the schema block. These slice the population; they do
  not change it. A metric's ALWAYS APPLY filters belong in the base view, not here --
  if a matched metric has one and the chosen base does not enforce it, say so in
  rationale rather than patching it in as a slice.
- aggregate_only: true means no base view applies and none is worth proposing -- a
  one-off scalar or an operational lookup. Justify it in rationale. The answer will
  be marked as UNRECONCILABLE, so this is the exception, not the default.
- analysis -- how to compute the answer once the data is in hand:
    "workspace_sql" for set operations: filtering, grouping, joining two cubes,
      aggregating, ranking, windows. This runs as DuckDB SQL against the cubes as
      views and is the cheaper, more reliable path -- PREFER IT for any re-cut.
    "python" for statistics, trend decomposition, significance tests, anomaly
      detection, forecasting, clustering, correlation, and ANY turn that should
      produce a chart -- the chart spec is built in Python.

The cubes listed below carry base_view, dimensions, row_count, truncated and a
3-row sample. They are context for stating the requirement well, NOT a menu to pick
from -- code decides what actually gets reused."""

    PROPOSAL_PROMPT = """You are proposing a NEW base view, so you must also say how each
multi-valued column collapses onto the grain. The schema marks these FAN-OUT. Do NOT
add such a column to GROUP BY -- that changes the grain and double-counts those keys.

Prefer strategy "highest_intent": rank the column's values by business value and take
the highest-ranked value the key touched. Approved attribution rules for this company
are listed above -- reuse one verbatim when it covers the column, and set
source="brain". Only propose your own (source="llm") when none applies, and explain
the ranking in rationale.

Fall back to "most_frequent" (with a latest-timestamp tiebreak) only when no value
ordering is defensible. Never use "first" or "latest" alone for a column that drives
conversion or revenue -- that misattributes exactly the multi-value keys that matter
most."""

    def _render_attribution_pattern(self, rules: List[AttributionRule]) -> str:
        """Render the actual CTE, parameterised on the rule -- do not describe the
        technique in prose and hope. A model copies structure far more reliably
        than it follows instructions.

        This is the only moment an LLM writes attribution SQL: when it is
        authoring a proposed base view's source_sql. Once that base is approved
        every cube inherits the collapse for free and no prompt can override it.

        The shape is modeled on a production Athena query, with two deliberate
        departures. That query re-joins each attributed level back to the
        event-level base and filters on it, which keeps the output at *event*
        grain -- right for a filtered event feed, wrong for a base view, which
        must end at its own grain. And its ordering is pure most-frequent; the
        business case is for ranking by value, so highest_intent is what gets
        generated, with most-frequent as the documented fallback.
        """
        if not rules:
            return ""
        blocks = []
        for rule in rules:
            grain = ", ".join(rule.grain) or "<grain>"
            if rule.strategy == "highest_intent" and rule.priority_values:
                case = " ".join(f"WHEN '{v}' THEN {i + 1}"
                                for i, v in enumerate(rule.priority_values))
                order = (f"ORDER BY CASE {rule.column} {case} ELSE 99 END ASC\n"
                         f"                      , event_count DESC\n"
                         f"                      , latest_event DESC")
                ranking = (f"Business ranking, highest value first: "
                           f"{', '.join(rule.priority_values)}.")
            else:
                order = ("ORDER BY event_count DESC\n"
                         "                      , latest_event DESC")
                ranking = ("No defensible value ordering was supplied, so this "
                           "collapses to the most frequent value.")
            tiebreak = (f" Resolve ties with {', '.join(rule.tiebreakers)}."
                        if rule.tiebreakers else "")
            blocks.append(f"""The column `{rule.column}` holds more than one value per {grain}. Your base
view must collapse it with a ranked attribution CTE, not GROUP BY. {ranking}{tiebreak}
Use exactly this shape:

WITH ranked_{rule.column} AS (
    SELECT {grain}
         , {rule.column}
         , COUNT(*) AS event_count
         , MAX(<timestamp_column>) AS latest_event
    FROM <table>
    WHERE <the population's filters>
    GROUP BY {grain}, {rule.column}
)
, attributed_{rule.column} AS (
    SELECT *
         , ROW_NUMBER() OVER (
               PARTITION BY {grain}
               {order}
           ) AS rn
    FROM ranked_{rule.column}
)
-- then join back on {grain} AND rn = 1, exposing {rule.column} as the attributed value
-- ATTRIBUTION: {rule.column} -> one value per {grain} by {rule.strategy}""")
        blocks.append(
            "Emit one such CTE per attributed column and chain them: attribute the\n"
            "coarser level first, then the finer level within it. Every join back is ON\n"
            "the grain key AND rn = 1 -- the row count of your base view must equal the\n"
            "distinct count of the grain. That is what makes it an ID-grain base rather\n"
            "than an event feed, and it is checked.")
        return "\n\n".join(blocks)

    def _plan_turn(self, llm: Any, tenant_id: str, conversation_id: str, question: str,
                   query_nodes: List[Any], defn_nodes: List[Any],
                   schema_ctx: Optional[SchemaContext] = None) -> TurnPlan:
        """Resolve the population, compose and guard the cube, then ask the
        DataManager whether the workspace already covers it.

        Always calls the LLM, cached cubes or not.
        """
        if schema_ctx is None:
            schema_ctx = self.schema_context.build(tenant_id, question, query_nodes,
                                                   defn_nodes)
        frames = self.data_cache.list_available(tenant_id, conversation_id)
        prompt = self._plan_prompt(question, schema_ctx, frames)

        parsed = self._ask_planner(llm, prompt, schema_ctx)
        if parsed is None:
            return TurnPlan(path="aggregate", analysis="python",
                            rationale="the planner produced no usable plan")

        plan = self._resolve_plan(tenant_id, parsed, schema_ctx)
        if plan is None:
            return TurnPlan(path="aggregate", analysis="python",
                            rationale="the planner named a base view that does not exist")

        # The guard refused: feed the culprit back once. Do not silently drop a
        # dimension on the model's behalf -- the answer would then be to a
        # question nobody asked.
        if plan.cube_sql is not None and not plan.cube_sql.ok:
            retry = self._ask_planner(
                llm, prompt + "\n\n" + self._guard_feedback(plan.cube_sql), schema_ctx)
            retried = self._resolve_plan(tenant_id, retry, schema_ctx) if retry else None
            if retried is None or retried.cube_sql is None or not retried.cube_sql.ok:
                reason = plan.cube_sql.error
                return TurnPlan(path="aggregate", analysis=plan.analysis,
                                rationale=f"no cube could be composed: {reason}",
                                caveats=[f"the requested breakdown could not be sized: "
                                         f"{reason}"])
            plan = retried

        if parsed.get("aggregate_only"):
            plan.path = "aggregate"
            return plan

        plan.requirement = DataRequirement(
            base_view=plan.base_view.name,
            population_hash=plan.cube_sql.population_hash,   # from the base, never the LLM
            grain=list(plan.base_view.grain),
            dimensions=list(plan.cube.dimensions),
            measures=list(plan.cube_sql.measures),
            filters=dict(plan.cube.filters),
            time_column=plan.cube.time_column,
            time_start=plan.cube.time_start, time_end=plan.cube.time_end)
        plan.verdict = self.data_manager.assess(tenant_id, conversation_id, plan.requirement)
        plan.path = plan.verdict.decision
        plan.df_label = plan.verdict.label
        return plan

    def _plan_prompt(self, question: str, schema_ctx: SchemaContext,
                     frames: List[Dict[str, Any]]) -> str:
        lines = [f"Question: {question}", ""]
        if schema_ctx.rendered:
            lines.extend([schema_ctx.rendered, ""])
        if frames:
            lines.append("Cubes already in this conversation's workspace:")
            for f in frames:
                lines.append(
                    f"- {f['label']}: {f.get('description', '')} | base_view="
                    f"{f.get('base_view') or 'none'} | dimensions={f.get('dimensions')} "
                    f"| rows={f.get('row_count')} | truncated={f.get('truncated')} "
                    f"| sample={f.get('sample')}")
            lines.append("")
        return "\n".join(lines)

    def _ask_planner(self, llm: Any, prompt: str,
                     schema_ctx: SchemaContext) -> Optional[Dict[str, Any]]:
        system = self.PLAN_SYSTEM_PROMPT
        if self._has_fanout(schema_ctx):
            system += "\n\n" + self.PROPOSAL_PROMPT
        system += "\n\n" + self._render_attribution_pattern(
            self._fanout_rules(schema_ctx))
        try:
            res = llm.generate(prompt=prompt, system_prompt=system, temperature=0.0)
            text = (res.text or "").strip() if res and hasattr(res, "text") else ""
        except Exception as exc:  # noqa: BLE001 - a dead gateway degrades, never raises
            logger.warning("turn planning failed: %s", exc, exc_info=True)
            return None
        return _parse_json_block(text, context="turn plan")

    @staticmethod
    def _guard_feedback(cube_sql: Any) -> str:
        return (f"Your previous cube was refused: {cube_sql.error} "
                f"The largest dimensions are {cube_sql.offending_dimensions}. "
                f"Drop or bucket one and answer again with the same JSON shape.")

    # -- resolving what the planner said --------------------------------------
    def _resolve_plan(self, tenant_id: str, parsed: Optional[Dict[str, Any]],
                      schema_ctx: SchemaContext) -> Optional[TurnPlan]:
        if not parsed:
            return None
        name = (parsed.get("base_view") or "").strip()
        if not name:
            return None

        proposal = parsed.get("propose_base_view")
        caveats: List[str] = []
        if isinstance(proposal, dict) and proposal:
            view = self._store_proposal(tenant_id, proposal, parsed, schema_ctx)
            if view is None:
                return None
            approved = False
            caveats.append(
                f"this answer rests on an unreviewed base view definition "
                f"({view.name}); figures are provisional until it is approved.")
        else:
            view = self.base_views.get(tenant_id, name, approved_only=False)
            if view is None:
                return None
            approved = self.base_views.is_approved(tenant_id, name)
            if not approved:
                caveats.append(
                    f"this answer rests on an unreviewed base view definition "
                    f"({view.name}); figures are provisional until it is approved.")

        cube = self._parse_cube(parsed.get("cube") or {}, view)
        cube_sql = self.base_views.compose_cube(view, cube, schema_ctx.profiles or {})
        return TurnPlan(
            path="retrieve",
            analysis="workspace_sql" if parsed.get("analysis") == "workspace_sql" else "python",
            base_view=view, base_view_approved=approved, cube=cube, cube_sql=cube_sql,
            grain=list(view.grain), dimensions=list(cube.dimensions),
            measures=list(cube_sql.measures), profiles=dict(schema_ctx.profiles or {}),
            time_window=(f"{cube.time_start}..{cube.time_end}"
                         if cube.time_start and cube.time_end else ""),
            rationale=str(parsed.get("rationale") or ""), caveats=caveats,
            # Attribution is a property of the population. On an existing base it
            # is already baked in and inherited; the planner may not override it.
            attributions=[])

    def _store_proposal(self, tenant_id: str, proposal: Dict[str, Any],
                        parsed: Dict[str, Any],
                        schema_ctx: SchemaContext) -> Optional[BaseView]:
        try:
            rules = [AttributionRule(**r) for r in (parsed.get("attributions") or [])
                     if isinstance(r, dict)]
            payload = {k: v for k, v in proposal.items()
                       if k in {"name", "grain", "source_sql", "dimension_columns",
                                "measure_columns", "time_column", "row_count_estimate",
                                "description", "owner", "aliases"}}
            view = BaseView(**payload)
            view.attributions = rules + self._default_attributions(
                view, rules, schema_ctx)
        except (TypeError, ValueError) as exc:
            logger.warning("could not build the proposed base view: %s", exc)
            return None
        if not view.name or not view.source_sql or not view.grain:
            return None
        self.base_views.upsert(tenant_id, view, by="stakeholder")
        return view

    def _default_attributions(self, view: BaseView, existing: List[AttributionRule],
                              schema_ctx: SchemaContext) -> List[AttributionRule]:
        """A fanned-out column carried onto the grain with no rule would silently
        double-count. Synthesize a most_frequent rule and mark it source="default"
        so a reviewer can see the machine chose it, not the business."""
        covered = {r.column for r in existing}
        out = []
        for column in view.dimension_columns:
            if column in covered:
                continue
            profile = (schema_ctx.profiles or {}).get(column)
            if profile is None:
                continue
            if any(share > 0 for key, share in profile.fanout_by_key.items()
                   if key in view.grain):
                out.append(AttributionRule(
                    column=column, grain=list(view.grain), strategy="most_frequent",
                    tiebreakers=["event_count DESC"], source="default",
                    rationale="synthesized because this column fans out at the base "
                              "grain and the proposal supplied no ranking; a human "
                              "should replace it with a business ordering"))
        return out

    @staticmethod
    def _parse_cube(raw: Dict[str, Any], view: BaseView) -> CubeSpec:
        measures = []
        for m in raw.get("measures") or []:
            if isinstance(m, dict) and m.get("name"):
                measures.append(CubeMeasure(name=str(m["name"]), expr=str(m.get("expr", ""))))
        filters = {str(k): [str(x) for x in v] for k, v in (raw.get("filters") or {}).items()
                   if isinstance(v, list)}
        return CubeSpec(
            base_name=view.name,
            dimensions=[str(d) for d in (raw.get("dimensions") or [])],
            measures=measures, filters=filters,
            time_column=str(raw.get("time_column") or view.time_column or ""),
            time_start=str(raw.get("time_start") or ""),
            time_end=str(raw.get("time_end") or ""))

    @staticmethod
    def _has_fanout(schema_ctx: SchemaContext) -> bool:
        return any(share > 0 for p in (schema_ctx.profiles or {}).values()
                   for share in p.fanout_by_key.values())

    @staticmethod
    def _fanout_rules(schema_ctx: SchemaContext) -> List[AttributionRule]:
        out = []
        for p in (schema_ctx.profiles or {}).values():
            for key, share in sorted(p.fanout_by_key.items()):
                if share > 0:
                    out.append(AttributionRule(column=p.column, grain=[key],
                                               strategy="most_frequent"))
                    break
        return out

    AGGREGATE_CONTEXT_PROMPT = """
The BUSINESS SEMANTICS, BASE VIEWS, and DATABASE SCHEMA sections below are
authoritative. Semantics describe what a metric MEANS -- its formula, the grain
it is valid at, and the filters that must always be applied. Schema describes
the real tables, the real columns, and the real values in this warehouse. The
Example Queries are historical and may reference columns that no longer exist --
where they disagree, semantics win over schema, and schema wins over the
examples.

Never invent a column name, and never invent a filter literal: use the exact
values listed for that column, with their exact casing. Every filter listed
under ALWAYS APPLY for a metric you are computing must appear in the WHERE
clause, whether or not the user mentioned it.

You are on the fallback path: no base view governs this query, so its result
cannot be reconciled against any other answer. Keep it narrow and answer only
what was asked."""

    def _synthesize_sql(self, llm: Any, question: str, query_nodes: List[Any],
                        defn_nodes: List[Any], prior_sql: str = "",
                        prior_error: str = "",
                        plan: Optional[TurnPlan] = None,
                        schema_ctx: Optional[SchemaContext] = None) -> Tuple[str, Tuple[int, int]]:
        """Let the analyst write ad-hoc SQL from approved context, instead of only
        reusing an approved query verbatim. Independent of the retrieval backend.

        When `prior_sql`/`prior_error` are set (a previous attempt was rejected by
        policy or failed execution), the prompt asks for a corrected query instead
        of a fresh one -- see _synthesize_and_execute_sql for the retry loop.
        """
        prompt = f"Question: {question}\n\n"
        if schema_ctx is not None and schema_ctx.rendered:
            prompt += schema_ctx.rendered + "\n"
        prompt += "Context:\n"
        for d in defn_nodes:
            prompt += f"Definition - {d.title}: {d.summary}\n"
        for q in query_nodes:
            prompt += f"Example Query - {q.title}:\n{q.payload.get('sql', '')}\n"
        if prior_sql:
            prompt += (
                f"\nYour previous attempt was NOT valid SQL, or failed to execute:\n"
                f"{prior_sql}\n\nError:\n{prior_error}\n\n"
                "Write a corrected query that fixes this specific problem."
            )
        dialect = (self.settings and self.settings.source_dialect) or "athena"
        sys_prompt = (
            f"You are an expert SQL analyst. Write a highly accurate SQL query in {dialect.upper()} "
            "dialect to answer the user's question, using the provided Definitions and Example "
            "Queries as context. The Example Queries were written for Metabase's UI and may "
            "contain placeholders like {{Date}}, {{osname}}, or {{category}} -- these are NOT "
            "valid SQL and will fail if copied as-is. Your query is executed directly with no "
            "parameter substitution, so replace every such placeholder with a concrete literal "
            "condition (e.g. a real recent date range, or drop the filter if the question doesn't "
            "need it) and never emit {{...}} syntax. Return ONLY the SQL query in a ```sql block. "
            "If the context is completely insufficient, output NOTHING."
        )
        if schema_ctx is not None or plan is not None:
            sys_prompt += "\n" + self.AGGREGATE_CONTEXT_PROMPT
        try:
            res = llm.generate(prompt=prompt, system_prompt=sys_prompt, temperature=0.0)
            text = (res.text or "").strip() if res and hasattr(res, "text") else ""

            if "```sql" in text:
                sql = text.split("```sql")[1].split("```")[0].strip()
            elif "```" in text:
                sql = text.split("```")[1].strip()
            else:
                sql = text.strip()

            return sql, (getattr(res, "tokens_in", 0), getattr(res, "tokens_out", 0))
        except Exception as exc:  # noqa: BLE001 - SQL synthesis is best-effort
            logger.warning("SQL synthesis failed for question %r: %s", question, exc,
                           exc_info=True)
            return "", (0, 0)

    # -- executing what Task 7 composed ---------------------------------------
    # On the cube paths there is no SQL to write: compose_cube already returned a
    # complete, hashed, guarded statement with the approved source_sql inlined
    # byte for byte. Asking a model to re-author it would break the one thing the
    # base exists to guarantee, because a re-emitted base is a different string
    # and therefore, correctly, a different population_hash. So `retrieve` and
    # `widen` never reach the synthesis LLM at all.

    def _allowed_tables(self, tenant_id: str) -> Optional[List[str]]:
        sources = self.tenants.list_datasources(tenant_id)
        return [t for s in sources for t in s.get("tables", [])] or None

    def _run_composed(self, tenant_id: str, sql: str, question: str) -> Tuple[str, Any]:
        """Validate through policy, then one round trip bounded by the transport
        ceiling. No LIMIT of our own: compose_cube did not emit one and the
        policy's injection is the single place that decides it."""
        policy = QueryPolicy(self.settings.policy)
        decision = policy.validate(sql, allowed_tables=self._allowed_tables(tenant_id),
                                   row_limit=self.settings.policy.max_transport_rows,
                                   dialect=self.settings.source_dialect)
        if decision.denied:
            from .execution.base import QueryResult
            return sql, QueryResult(ok=False, error="; ".join(decision.reasons))
        ctx = ExecutionContext(tenant_id=tenant_id, question=question,
                               dialect=self.settings.source_dialect,
                               row_limit=self.settings.policy.max_transport_rows)
        return decision.approved_sql, self.executor.execute(decision.approved_sql, ctx)

    def _cube_spec_to_run(self, plan: TurnPlan) -> CubeSpec:
        """What actually goes to the warehouse for this plan.

        A widen on a missing DIMENSION re-runs the whole cube over the union of
        old and new dimensions -- adding `device` re-splits every existing country
        cell, so there is no 'just the device part' to fetch. A widen on a TIME
        gap only is different: cells over disjoint date ranges are disjoint and
        every measure that survived the additivity gate sums across them, so that
        one really is a gap fetch.
        """
        import dataclasses
        spec = dataclasses.replace(plan.cube)
        verdict = plan.verdict
        if plan.path != "widen" or verdict is None:
            return spec
        if verdict.missing_dimensions:
            union = list(dict.fromkeys(list(verdict.existing_dimensions)
                                       + list(spec.dimensions)))
            spec.dimensions = union
            return spec
        if verdict.missing_time_ranges:
            start, end = verdict.missing_time_ranges[0]
            spec.time_start, spec.time_end = start, end
        return spec

    def _execute_cube(self, tenant_id: str, plan: TurnPlan,
                      question: str) -> Tuple[str, Any]:
        """One round trip, or a keyset walk when the cube is bigger than one.

        MAX_CUBE_CELLS and max_transport_rows are different numbers on purpose,
        so a cube between them is legal and simply takes more than one trip.
        """
        spec = self._cube_spec_to_run(plan)
        if spec == plan.cube and plan.cube_sql is not None and plan.cube_sql.ok:
            # Nothing changed: run exactly what was composed, hashed and guarded.
            recomposed = plan.cube_sql
        else:
            recomposed = self.base_views.compose_cube(
                plan.base_view, spec, self._plan_profiles(plan))
            if not recomposed.ok:
                from .execution.base import QueryResult
                return "", QueryResult(ok=False, error=recomposed.error)
            plan.cube_sql = recomposed
        transport = self.settings.policy.max_transport_rows
        if recomposed.estimated_cells <= transport:
            return self._run_composed(tenant_id, recomposed.sql, question)
        return self._fetch_keyset_chunks(tenant_id, plan, question, spec=spec)

    def _plan_profiles(self, plan: TurnPlan) -> Dict[str, Any]:
        """Re-compose against the SAME real cardinalities the plan was sized
        against. A permissive stand-in here would under-estimate the widened
        cube's cell count and quietly skip the paging it needs."""
        profiles = dict(plan.profiles or {})
        for d in (set(plan.cube.dimensions) | set(plan.dimensions)
                  | set(getattr(plan.verdict, "existing_dimensions", []) or [])):
            profiles.setdefault(d, _permissive_profile(d))
        return profiles

    def _fetch_keyset_chunks(self, tenant_id: str, plan: TurnPlan, question: str,
                             spec: Optional[CubeSpec] = None,
                             keys: Optional[List[str]] = None) -> Tuple[str, Any]:
        """Walk the result in keyset pages and concatenate.

        Cube cells are disjoint by construction, so concatenation is exact -- no
        dedupe, no re-aggregation. The stop condition is a SHORT PAGE, never the
        cell estimate: a cube that estimated 40,000 and returns exactly 50,000
        must page again rather than assume it is complete.
        """
        import pandas as pd
        from .execution.base import QueryResult

        spec = spec if spec is not None else plan.cube
        page_keys = keys or (list(spec.dimensions) or list(plan.base_view.grain))
        chunk = min(self.settings.policy.extract_chunk_rows,
                    self.settings.policy.max_transport_rows)
        ceiling = self.settings.policy.raw_extract_row_limit

        pages: List[Any] = []
        sql_run: List[str] = []
        warnings: List[str] = []
        cursor: Any = ""
        total = 0
        truncated = False

        while True:
            page_sql = self.base_views.compose_keyset_chunk(
                plan.base_view, spec, cursor, chunk, keys=page_keys)
            approved, res = self._run_composed(tenant_id, page_sql, question)
            sql_run.append(approved or page_sql)
            if not res.ok:
                return (sql_run[-1], res)
            df = res.data if res.data is not None else pd.DataFrame()
            warnings.extend(res.warnings)
            if len(df):
                pages.append(df)
                total += len(df)
            if total >= ceiling:
                # Stop paging and say so. Never keep going silently.
                truncated = True
                warnings.append(f"result truncated at {ceiling} rows")
                break
            if len(df) < chunk:
                break
            missing = [k for k in page_keys if k not in df.columns]
            if missing:
                # No cursor means no safe next page. Stopping here under-reports;
                # guessing a cursor would silently skip or duplicate cells, which
                # is worse. Say what happened rather than raising mid-turn.
                truncated = True
                warnings.append(
                    f"result truncated at {total} rows: cannot page further because "
                    f"the result is missing key column(s) {missing}")
                break
            cursor = [df.iloc[-1][k] for k in page_keys]
            if len(cursor) == 1:
                cursor = cursor[0]

        combined = pd.concat(pages, ignore_index=True) if pages else pd.DataFrame()
        if truncated and len(combined) > ceiling:
            combined = combined.head(ceiling)
        return (sql_run[-1] if sql_run else "",
                QueryResult(ok=True, data=combined, row_count=len(combined),
                            columns=list(combined.columns), warnings=warnings,
                            truncated=truncated))

    def _synthesize_and_execute_sql(self, llm: Any, tenant_id: str, question: str,
                                    query_nodes: List[Any], defn_nodes: List[Any],
                                    max_attempts: int = 3,
                                    plan: Optional[TurnPlan] = None,
                                    schema_ctx: Optional[SchemaContext] = None
                                    ) -> Tuple[str, Any, Tuple[int, int]]:
        """Dispatch on the plan, then fall through to today's synthesis loop.

        With `plan` None (or on the aggregate path) this behaves exactly as it
        always has, which is what keeps every existing test green.
        """
        if plan is not None and plan.path in ("retrieve", "widen") and plan.cube_sql:
            sql, res = self._execute_cube(tenant_id, plan, question)
            if res is not None and res.ok:
                return sql, res, (0, 0)
            return self._own_the_cube_failure(llm, tenant_id, plan, question,
                                              sql, res, schema_ctx)
        return self._synthesize_sql_loop(llm, tenant_id, question, query_nodes,
                                         defn_nodes, max_attempts, plan, schema_ctx)

    def _own_the_cube_failure(self, llm: Any, tenant_id: str, plan: TurnPlan,
                              question: str, sql: str, res: Any,
                              schema_ctx: Optional[SchemaContext]
                              ) -> Tuple[str, Any, Tuple[int, int]]:
        """The failure is in one of two places, and they have different owners."""
        error = getattr(res, "error", "") or "unknown warehouse error"
        name = plan.base_view.name if plan.base_view else "?"

        if plan.base_view_approved:
            # A governance failure, not a prompt failure. Never let a model
            # rewrite an approved base's source_sql: the review flow exists so a
            # human owns that string, and an answer computed from a silently
            # patched base carries a population_hash that no longer describes the
            # SQL that ran.
            logger.error("approved base view %r failed against the warehouse for "
                         "tenant %s: %s", name, tenant_id, error)
            plan.caveats.append(
                f"base view `{name}` no longer executes against the warehouse "
                f"({error}) -- it needs review. This answer fell back to an "
                f"ungoverned query and cannot be reconciled with others.")
            return sql, res, (0, 0)

        # A DRAFT base proposed this same turn: the model authored that source_sql
        # minutes ago and nobody reviewed it, so it may repair it. Once.
        repaired = self._repair_draft_base(llm, tenant_id, plan, question, error,
                                           schema_ctx)
        if repaired is None:
            plan.caveats.append(
                f"the proposed base view `{name}` could not be made to run "
                f"({error}); this answer is not reconcilable.")
            return sql, res, (0, 0)
        sql2, res2 = self._execute_cube(tenant_id, repaired, question)
        if res2 is not None and res2.ok:
            plan.base_view = repaired.base_view
            plan.cube = repaired.cube
            plan.cube_sql = repaired.cube_sql
            return sql2, res2, (0, 0)
        plan.caveats.append(
            f"the proposed base view `{name}` still failed after one repair "
            f"({getattr(res2, 'error', '')}); this answer is not reconcilable.")
        return sql2, res2, (0, 0)

    def _repair_draft_base(self, llm: Any, tenant_id: str, plan: TurnPlan,
                           question: str, error: str,
                           schema_ctx: Optional[SchemaContext]) -> Optional[TurnPlan]:
        if schema_ctx is None:
            schema_ctx = SchemaContext()
        prompt = (f"Question: {question}\n\n{schema_ctx.rendered}\n\n"
                  f"The base view you proposed failed to execute against the "
                  f"warehouse:\n\n{plan.base_view.source_sql if plan.base_view else ''}"
                  f"\n\nError:\n{error}\n\nRe-propose a corrected base view that "
                  f"fixes this specific problem, using the same JSON shape.")
        parsed = self._ask_planner(llm, prompt, schema_ctx)
        return self._resolve_plan(tenant_id, parsed, schema_ctx)

    def _synthesize_sql_loop(self, llm: Any, tenant_id: str, question: str,
                             query_nodes: List[Any], defn_nodes: List[Any],
                             max_attempts: int, plan: Optional[TurnPlan],
                             schema_ctx: Optional[SchemaContext]
                             ) -> Tuple[str, Any, Tuple[int, int]]:
        """Synthesize SQL and run it, retrying with the failure fed back to the LLM
        when policy rejects the query or execution fails -- e.g. a leftover
        {{Date}}-style Metabase placeholder, a typo'd column, wrong dialect syntax.
        Stops at the first successful execution, or after `max_attempts`.

        Returns (sql, exec_result_or_None, total_tokens). exec_result is None only
        if every attempt failed -- the caller falls back to verbatim query reuse.
        """
        policy = QueryPolicy(self.settings.policy)
        sources = self.tenants.list_datasources(tenant_id)
        allowed_tables = [t for s in sources for t in s.get("tables", [])] or None
        dialect = self.settings.source_dialect

        prior_sql, prior_error = "", ""
        t_in_total, t_out_total = 0, 0
        for attempt in range(1, max_attempts + 1):
            sql, (t_in, t_out) = self._synthesize_sql(
                llm, question, query_nodes, defn_nodes, prior_sql=prior_sql,
                prior_error=prior_error, plan=plan, schema_ctx=schema_ctx)
            t_in_total += t_in
            t_out_total += t_out
            if not sql:
                break  # LLM declined (context insufficient) -- retrying won't help

            decision = policy.validate(sql, allowed_tables=allowed_tables, dialect=dialect)
            if decision.denied:
                logger.warning("synthesized SQL rejected by policy for tenant %s "
                               "(attempt %d/%d): %s", tenant_id, attempt, max_attempts,
                               decision.reasons)
                prior_sql, prior_error = sql, "; ".join(decision.reasons)
                continue

            ec = ExecutionContext(tenant_id=tenant_id, question=question, dialect="athena")
            exec_res = self.executor.execute(decision.approved_sql, ec)
            if exec_res.ok:
                return decision.approved_sql, exec_res, (t_in_total, t_out_total)

            logger.warning("synthesized SQL execution failed for tenant %s "
                           "(attempt %d/%d): %s", tenant_id, attempt, max_attempts, exec_res.error)
            prior_sql, prior_error = decision.approved_sql, exec_res.error

        return "", None, (t_in_total, t_out_total)

    def _synthesize_python(self, llm: Any, question: str, df_label: str,
                           frame_desc: Dict[str, Any], prior_code: str = "",
                           prior_error: str = "") -> Tuple[str, Tuple[int, int]]:
        prompt = (
            f"Question: {question}\n\n"
            f"A pandas DataFrame named `{df_label}` is available with columns "
            f"{frame_desc['columns']} and dtypes {frame_desc['dtypes']} "
            f"({frame_desc['row_count']} rows).\n"
        )
        if prior_code:
            prompt += (
                f"\nYour previous attempt failed:\n{prior_code}\n\nError:\n{prior_error}\n\n"
                "Write corrected code that fixes this specific problem."
            )
        sys_prompt = (
            "You are an expert data analyst. Write pandas Python code that computes the "
            f"answer to the question using the DataFrame `{df_label}` (already in scope -- "
            "do not redefine it or read it from any file/database). Assign your final "
            "answer to a variable named `result` (a scalar, dict, list, or small DataFrame "
            "-- not the full raw DataFrame unmodified). Only `pandas` (as `pd`), `numpy`, "
            "`math`, `statistics`, `datetime`, `collections`, and `re` may be imported; no "
            "file, network, or system access is available and will be rejected. Return "
            "ONLY the Python code in a ```python block. If the question can't be answered "
            "from this DataFrame, output NOTHING."
        )
        try:
            res = llm.generate(prompt=prompt, system_prompt=sys_prompt, temperature=0.0)
            text = (res.text or "").strip() if res and hasattr(res, "text") else ""
            if "```python" in text:
                code = text.split("```python")[1].split("```")[0].strip()
            elif "```" in text:
                code = text.split("```")[1].strip()
            else:
                code = text.strip()
            return code, (getattr(res, "tokens_in", 0), getattr(res, "tokens_out", 0))
        except Exception as exc:  # noqa: BLE001 - Python synthesis is best-effort
            logger.warning("Python synthesis failed for question %r: %s", question, exc,
                           exc_info=True)
            return "", (0, 0)

    def _synthesize_and_execute_python(self, llm: Any, tenant_id: str, conversation_id: str,
                                       question: str, df_label: str,
                                       max_attempts: int = 3) -> Tuple[str, Any, Tuple[int, int]]:
        """Mirrors _synthesize_and_execute_sql's retry loop: synthesize Python,
        run it through PythonCodePolicy then the sandbox, and on
        rejection/failure feed the reason back to the LLM for a corrected
        attempt. Stops at the first successful execution, or after
        max_attempts.

        Returns (code, exec_result_or_None, total_tokens). exec_result is
        None if the label isn't cached, or if every attempt failed -- the
        caller falls back to the SQL path either way.
        """
        df = self.data_cache.get(tenant_id, conversation_id, df_label)
        if df is None:
            return "", None, (0, 0)
        frame_desc = next(
            (f for f in self.data_cache.list_available(tenant_id, conversation_id)
             if f["label"] == df_label),
            {"columns": [], "dtypes": {}, "row_count": 0})

        policy = PythonCodePolicy()
        prior_code, prior_error = "", ""
        t_in_total, t_out_total = 0, 0
        for attempt in range(1, max_attempts + 1):
            code, (t_in, t_out) = self._synthesize_python(
                llm, question, df_label, frame_desc, prior_code=prior_code, prior_error=prior_error)
            t_in_total += t_in
            t_out_total += t_out
            if not code:
                break  # LLM declined -- retrying won't help

            decision = policy.validate(code)
            if decision.denied:
                logger.warning("synthesized Python rejected by policy for tenant %s "
                               "(attempt %d/%d): %s", tenant_id, attempt, max_attempts,
                               decision.reasons)
                prior_code, prior_error = code, "; ".join(decision.reasons)
                continue

            exec_res = run_python_sandboxed(decision.approved_code, {df_label: df})
            if exec_res.ok:
                return decision.approved_code, exec_res, (t_in_total, t_out_total)

            logger.warning("synthesized Python execution failed for tenant %s "
                           "(attempt %d/%d): %s", tenant_id, attempt, max_attempts, exec_res.error)
            prior_code, prior_error = decision.approved_code, exec_res.error

        return "", None, (t_in_total, t_out_total)

    def _synthesize(self, llm: Any, question: str, category: str, data: Optional[Dict[str, Any]] = None) -> Tuple[str, Tuple[int, int], Optional[Dict[str, Any]]]:
        try:
            data_context = ""
            if data and isinstance(data, dict) and data.get("rows"):
                data_context = f"\nData context (top 3 rows): {data['rows'][:3]}\nColumns: {data.get('columns', [])}"
            elif data and isinstance(data, list):
                data_context = f"\nData context (top 3 rows): {data[:3]}"

            sys_prompt = (
                "You are a cautious internal analytics assistant. State what you know and what data you would need. Do not invent figures. "
                "You must respond with a strict JSON object with the following schema: "
                "{\"answer\": \"Your 2-3 sentence answer text here\", "
                "\"chart_config\": {\"type\": \"LineChart|BarChart|AreaChart|ScatterChart\", \"xKey\": \"col_name\", \"series\": [{\"key\": \"col_name\"}]} } "
                "If a chart is not applicable, omit chart_config."
            )
            res = llm.generate(
                prompt="Answer the question: " + question + data_context,
                system_prompt=sys_prompt,
                temperature=0.2)
            
            text = (res.text or "").strip() if res and hasattr(res, "text") else ""
            
            # Simple heuristic to extract JSON if LLM returned markdown blocks
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].strip()
                
            import json
            try:
                parsed = json.parse(text) if hasattr(json, "parse") else json.loads(text)
                return parsed.get("answer", "No answer provided in JSON."), (getattr(res, "tokens_in", 0), getattr(res, "tokens_out", 0)), parsed.get("chart_config")
            except Exception:
                return text, (getattr(res, "tokens_in", 0), getattr(res, "tokens_out", 0)), None
        except Exception as e:  # noqa: BLE001 - LLM is optional
            return "Could not generate an answer: " + str(e), (0, 0), None

    def _record(self, tenant_id: str, question: str, user_id: str, category: str,
                trace: str, answer: str, mode: AnswerMode, status: str,
                escalated: bool, source_ids: List[str],
                citations: Optional[List[Dict[str, Any]]] = None,
                facts: Optional[List[str]] = None,
                caveats: Optional[List[str]] = None,
                tokens_in: int = 0, tokens_out: int = 0,
                queries_run: Optional[List[str]] = None,
                python_cells: Optional[List[Dict[str, Any]]] = None,
                produced_df_label: str = "",
                conversation_id: str = "") -> Dict[str, Any]:
        answer_id = new_id("ans")
        cost = round((tokens_in / 1000.0) * self.cost_per_1k_input
                     + (tokens_out / 1000.0) * self.cost_per_1k_output, 6)
        freshness = 0.0
        for c in citations or []:
            freshness = max(freshness, float(c.get("freshness", 0.0)))
        self.stores.for_tenant(tenant_id).execute(
            "INSERT INTO stakeholder_answers (id,tenant_id,question,user_id,category,answer,"
            "answer_mode,status,trace_id,created_at,source_node_ids,citations,facts,caveats,"
            "freshness,tokens_in,tokens_out,cost,escalated,queries_run,python_cells,"
            "produced_df_label,conversation_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (answer_id, tenant_id, question, user_id, category, answer, mode.value, status,
             trace, now_iso(), dump_json(source_ids), dump_json(citations or []),
             dump_json(facts or []), dump_json(caveats or []), freshness,
             tokens_in, tokens_out, cost, int(escalated), dump_json(queries_run or []),
             dump_json(python_cells or []), produced_df_label, conversation_id))
        return {"answer_id": answer_id, "tenant_id": tenant_id, "question": question,
                "category": category, "answer": answer, "answer_mode": mode.value,
                "status": status, "escalated": escalated, "citations": citations or [],
                "caveats": caveats or [], "facts": facts or [], "freshness": freshness,
                "cost": cost, "trace_id": trace, "queries_run": queries_run or [],
                "python_cells": python_cells or [], "produced_df_label": produced_df_label,
                "conversation_id": conversation_id}

    # -- feedback + quality -------------------------------------------------
    def record_feedback(self, tenant_id: str, answer_id: str, user_id: str,
                        rating: str, comment: str = "") -> Dict[str, Any]:
        store = self.stores.for_tenant(tenant_id)
        row = store.query_one(
            "SELECT * FROM stakeholder_answers WHERE id=? AND tenant_id=?", (answer_id, tenant_id))
        if not row:
            return {"error": "answer not found"}
        fid = new_id("fb")
        store.execute(
            "INSERT INTO stakeholder_feedback (id,tenant_id,answer_id,user_id,rating,comment,"
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (fid, tenant_id, answer_id, user_id, rating, comment, now_iso()))
        self.obs.event(tenant_id=tenant_id, stage="stakeholder.feedback", actor=user_id or "unknown",
                       resource=answer_id, meta={"rating": rating})
        return {"feedback_id": fid, "answer_id": answer_id, "rating": rating}

    def quality(self, tenant_id: str) -> Dict[str, Any]:
        store = self.stores.for_tenant(tenant_id)
        answers = store.query_all(
            "SELECT * FROM stakeholder_answers WHERE tenant_id=?", (tenant_id,))
        feedbacks = store.query_all(
            "SELECT * FROM stakeholder_feedback WHERE tenant_id=?", (tenant_id,))
        total = len(answers)
        answered = sum(1 for a in answers if a["status"] == "ANSWERED")
        escalated = sum(1 for a in answers if a["escalated"])
        ratings = [f["rating"] for f in feedbacks]
        accept = sum(1 for r in ratings if r in ("up", "thumbs_up", "1", "yes"))
        reuses = sum(1 for a in answers if a["answer_mode"] in (
            AnswerMode.REFRESHED_APPROVED_QUERY.value, AnswerMode.DIRECT_FROM_APPROVED_KNOWLEDGE.value))
        cost = sum(float(a["cost"] or 0) for a in answers)
        return {
            "tenant_id": tenant_id,
            "total_questions": total,
            "answered": answered,
            "escalated": escalated,
            "escalation_rate": round(escalated / total, 3) if total else 0.0,
            "feedback_count": len(feedbacks),
            "acceptance_rate": round(accept / len(ratings), 3) if ratings else 0.0,
            "reuse_count": reuses,
            "total_cost_usd": round(cost, 6),
            "avg_cost_usd": round(cost / total, 6) if total else 0.0,
        }


__all__ = ["StakeholderService", "CATEGORY_MARKERS", "HIGH_RISK_MARKERS"]