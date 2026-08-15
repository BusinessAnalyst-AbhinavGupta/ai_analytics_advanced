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

from .brain.embedding import Embedder
from .brain.index import BrainIndex
from .brain.store import CompanyBrain
from .config import Settings
from .database import Store, dump_json, load_json
from .domain import AnswerMode, NodeKind, new_id, now_iso
from .execution.base import ExecutionContext
from .execution.dataframe_cache import ConversationDataCache
from .execution.policy import QueryPolicy, resolve_template_placeholders
from .llm.client import make_role_client
from .observability import Observability, new_trace
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
        self.data_cache = ConversationDataCache()
        self.obs = observability or Observability(stores)
        self.settings = settings or Settings()
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

        has_nodes = bool(query_nodes or defn_nodes)
        if has_nodes and self._llm_live(llm):
            sql, exec_res, toks = self._synthesize_and_execute_sql(
                llm, tenant_id, question, query_nodes, defn_nodes)
            if exec_res is not None and exec_res.ok:
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

    def _synthesize_sql(self, llm: Any, question: str, query_nodes: List[Any],
                        defn_nodes: List[Any], prior_sql: str = "",
                        prior_error: str = "") -> Tuple[str, Tuple[int, int]]:
        """Let the analyst write ad-hoc SQL from approved context, instead of only
        reusing an approved query verbatim. Independent of the retrieval backend.

        When `prior_sql`/`prior_error` are set (a previous attempt was rejected by
        policy or failed execution), the prompt asks for a corrected query instead
        of a fresh one -- see _synthesize_and_execute_sql for the retry loop.
        """
        prompt = f"Question: {question}\n\nContext:\n"
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

    def _synthesize_and_execute_sql(self, llm: Any, tenant_id: str, question: str,
                                    query_nodes: List[Any], defn_nodes: List[Any],
                                    max_attempts: int = 3) -> Tuple[str, Any, Tuple[int, int]]:
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
                llm, question, query_nodes, defn_nodes, prior_sql=prior_sql, prior_error=prior_error)
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
            "freshness,tokens_in,tokens_out,cost,escalated,queries_run,conversation_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (answer_id, tenant_id, question, user_id, category, answer, mode.value, status,
             trace, now_iso(), dump_json(source_ids), dump_json(citations or []),
             dump_json(facts or []), dump_json(caveats or []), freshness,
             tokens_in, tokens_out, cost, int(escalated), dump_json(queries_run or []),
             conversation_id))
        return {"answer_id": answer_id, "tenant_id": tenant_id, "question": question,
                "category": category, "answer": answer, "answer_mode": mode.value,
                "status": status, "escalated": escalated, "citations": citations or [],
                "caveats": caveats or [], "facts": facts or [], "freshness": freshness,
                "cost": cost, "trace_id": trace, "queries_run": queries_run or [],
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