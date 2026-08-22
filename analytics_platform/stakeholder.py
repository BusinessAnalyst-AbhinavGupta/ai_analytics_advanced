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
import re
from dataclasses import asdict
from time import perf_counter
from typing import (Any, Dict, Iterator, List, Optional, Sequence,
                    Tuple)

from .base_view import BaseViewRegistry, reconcile
from .brain.embedding import Embedder
from .brain.index import BrainIndex
from .brain.store import CompanyBrain
from .config import Settings
from .database import Store, dump_json, load_json
from .data_manager import CoverageVerdict, DataManager, DataRequirement
from .domain import (PIPELINE_STEPS, AnalysisArtifact, AnswerMode, AttributionRule,
                     BaseView, CubeMeasure, CubeSpec, NodeKind, ReconcileResult,
                     StepEvent, TurnPlan, new_id, now_iso)
from .execution.base import ExecutionContext
from .execution.dataframe_cache import ConversationDataCache
from .execution.extract_store import SAFE_ID, ExtractMeta, ExtractStore
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

# How much of the question a cube records as its description. This was 200, which
# cut the live question inside the phrase "last 30 days worth of " -- losing the
# very timeframe the user had stated, in the one field a follow-up turn reads to
# find out what the previous turn was about.
CUBE_DESCRIPTION_CHARS = 600

_ISO_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _as_date_literal(value: Any) -> str:
    """The date part of whatever the planner offered, or "" if it is not a date.

    compose_cube emits `DATE '<literal>'`, and Trino/Athena accept that only as
    YYYY-MM-DD. The planner sometimes answers with a full ISO timestamp instead
    -- seen live as "2026-07-19T00:00:00Z" -- which composes to a literal the
    warehouse rejects, failing the whole query. Anything that is not a date at
    all is dropped rather than passed through: an unfiltered turn is visible and
    recoverable, a syntax error is neither.
    """
    match = _ISO_DATE_RE.match(str(value or "").strip())
    return match.group(1) if match else ""

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


def _escape_ident(name: str) -> str:
    """Reused from base_view so both SQL builders quote identifiers identically."""
    from .base_view import _escape_ident as _impl
    return _impl(name)


def _sql_literal(value: Any) -> str:
    from .base_view import _quote
    return _quote(value)


def _extract_sql_block(text: str) -> str:
    """The fenced SQL an LLM emitted, or bare text if it emitted no fence."""
    if "```sql" in text:
        return text.split("```sql")[1].split("```")[0].strip()
    if "```" in text:
        return text.split("```")[1].strip()
    return (text or "").strip()


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
            # Replay carries the whole provenance: what population the answer
            # rests on, what was reused, what ran where, what was assumed. An
            # answer that can only be re-read as prose is not reproducible.
            "extract_meta": load_json(r["extract_meta"], {}) if "extract_meta" in r.keys() else {},
            "analysis": load_json(r["analysis"], {}) if "analysis" in r.keys() else {},
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
        # Deleting a chat deletes its raw data too. Leaving Parquet on disk after
        # the conversation is gone means retention silently stops applying to it,
        # and the open DuckDB connection would keep serving views over files
        # nobody can reach any more.
        self.workspace.close(tenant_id, conversation_id)
        try:
            self.extract_store.delete_conversation(tenant_id, conversation_id)
        except (OSError, ValueError) as exc:
            logger.warning("could not delete extracts for conversation %s: %s",
                           conversation_id, exc)
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
        """Unchanged in signature and in return value -- now a thin drain of
        `answer_stream`. There is deliberately no second implementation: two
        code paths for one pipeline is how a streamed answer and a blocking one
        start quietly disagreeing about what the analyst actually did."""
        out: Optional[Dict[str, Any]] = None
        for ev in self.answer_stream(tenant_id, question, user_id=user_id,
                                     conversation_id=conversation_id):
            if ev["type"] == "answer":
                out = ev["payload"]
        return out

    def answer_stream(self, tenant_id: str, question: str, user_id: str = "",
                      conversation_id: str = "") -> Iterator[Dict[str, Any]]:
        """The turn, observable while it happens.

        Yields zero or more {"type": "step"} events, then exactly one
        {"type": "answer"} event carrying the payload `answer()` returns.

        Exceptions propagate. That is deliberate: `answer()` raised before Plan B
        and has to still raise, and a pipeline crash dressed up as a cheerful
        terminal answer hides the bug instead of reporting it. Turning a
        mid-stream failure into a transport-level `event: error` is the streaming
        route\'s job -- see the stream endpoint in api.py.
        """
        out = yield from self._answer_steps(tenant_id, question, user_id,
                                            conversation_id)
        yield {"type": "answer", "payload": out}

    @staticmethod
    def _step(step: str, state: str = "done", detail: str = "",
              t0: Optional[float] = None) -> Dict[str, Any]:
        assert step in PIPELINE_STEPS, step
        return {"type": "step", "payload": asdict(StepEvent(
            step=step, state=state, detail=detail,
            elapsed_ms=((perf_counter() - t0) * 1000.0) if t0 is not None else 0.0))}

    def _answer_steps(self, tenant_id: str, question: str, user_id: str = "",
                      conversation_id: str = "") -> Iterator[Dict[str, Any]]:
        """What used to be the body of `answer()`, verbatim apart from the yields.

        Every `return out` below is the same `return out` it has always been: a
        generator\'s return value is picked up by the `yield from` in
        `answer_stream`, which is what keeps this a control-flow change and
        nothing else.
        """
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
            # What escalates is the ANSWER, not the knowledge behind it. The
            # sources are recorded below so the reviewer sees what was matched,
            # but they are left alone: `_retrieve` only ever returns APPROVED /
            # APPROVED_WITH_CAVEATS nodes, so re-submitting one for review was
            # both illegal (neither status can move to UNDER_REVIEW) and wrong
            # -- it would have pulled senior-approved knowledge out of every
            # other stakeholder's reach because one question said "revenue".
            source_ids = [n.id for n in (query_nodes + defn_nodes)]
            out = self._record(tenant_id, question, user_id, category, trace, "",
                               AnswerMode.REQUIRES_SENIOR_REVIEW, "ESCALATED", True,
                               source_ids, caveats=["high-risk question matched escalation rules"],
                               queries_run=[n.payload.get("sql", "") for n in query_nodes],
                               conversation_id=conversation_id)
            self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.escalate",
                           actor="stakeholder", resource=out["answer_id"], status="OK",
                           meta={"category": category})
            return out

        if self._llm_live(llm):
            out = yield from self._run_analyst_pipeline_stream(
                llm, tenant_id, conversation_id, question, user_id, category, trace,
                query_nodes, defn_nodes)
            if out is not None:
                return out
            # The pipeline owns the whole synthesis path now, including the
            # aggregate one-off it falls back to internally. Re-running a
            # standalone SQL block here would just re-bill the same three
            # attempts, so the only thing left below is VERBATIM REUSE of an
            # already-approved stored query.

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
                    answer, toks, chart_config = self._synthesize(
                        llm, question, category,
                        {"skill_steps": exec_res.data_previews})
                    
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

    # -- the analyst pipeline -------------------------------------------------
    # One turn is: resolve semantics -> plan -> check the workspace -> retrieve
    # only what is missing -> analyse -> interpret. Each stage is its own method
    # returning its piece of the artifact, so the answer's provenance is built up
    # as the turn happens rather than reconstructed at the end from whatever
    # locals survived.

    def _run_analyst_pipeline(self, llm: Any, tenant_id: str, conversation_id: str,
                              question: str, user_id: str, category: str, trace: str,
                              query_nodes: List[Any], defn_nodes: List[Any]
                              ) -> Optional[Dict[str, Any]]:
        """The whole turn, blocking. Returns None when nothing could be produced,
        which leaves answer() free to fall through to the older paths.

        Drains the streaming form exactly as `answer()` drains `answer_stream()`:
        one implementation, two ways of consuming it. Callers that want the step
        events use `_run_analyst_pipeline_stream` directly.
        """
        gen = self._run_analyst_pipeline_stream(
            llm, tenant_id, conversation_id, question, user_id, category, trace,
            query_nodes, defn_nodes)
        while True:
            try:
                next(gen)
            except StopIteration as stop:
                return stop.value

    def _run_analyst_pipeline_stream(self, llm: Any, tenant_id: str, conversation_id: str,
                                     question: str, user_id: str, category: str, trace: str,
                                     query_nodes: List[Any], defn_nodes: List[Any]
                                     ) -> Iterator[Dict[str, Any]]:
        """The whole turn, yielding a step event at each named boundary and
        returning the answer payload (or None) as the generator's value."""
        t0 = perf_counter()
        yield self._step("understanding", "start")
        schema_ctx = self.schema_context.build(tenant_id, question, query_nodes, defn_nodes)
        yield self._step("understanding", "done",
                         self._understanding_detail(schema_ctx), t0)

        t0 = perf_counter()
        yield self._step("planning", "start")
        plan = self._plan_turn(llm, tenant_id, conversation_id, question,
                               query_nodes, defn_nodes, schema_ctx=schema_ctx)
        yield self._step("planning", "done", self._planning_detail(plan), t0)

        clarification = (self._planner_failure_refusal(tenant_id, conversation_id, plan)
                         or self._timeframe_clarification(tenant_id, conversation_id,
                                                          plan, schema_ctx))
        if clarification:
            out = self._record(
                tenant_id, question, user_id, category, trace, clarification,
                AnswerMode.NEEDS_CLARIFICATION, "NEEDS_CLARIFICATION", False,
                [n.id for n in (query_nodes + defn_nodes)],
                caveats=["the population this conversation is built on was not "
                         "changed to answer this turn"] if plan.planner_failed else
                        ["no timeframe was given, and none was assumed"],
                conversation_id=conversation_id)
            self.obs.event(tenant_id=tenant_id, trace_id=trace,
                           stage="stakeholder.clarify", actor="stakeholder",
                           resource=out["answer_id"], status="OK",
                           meta={"category": category, "reason": "timeframe"})
            return out

        artifact = AnalysisArtifact(question=question, plan_rationale=plan.rationale)
        self._record_semantics(artifact, schema_ctx)
        caveats: List[str] = list(plan.caveats)
        caveats.extend(self._uncertainty_caveats(schema_ctx))

        label, meta = "", None
        t_in_total, t_out_total = 0, 0

        t0 = perf_counter()
        yield self._step("checking_workspace", "start")
        if plan.path in ("reuse", "widen"):
            for existing in self._verdict_labels(plan):
                self.workspace.register(tenant_id, conversation_id, existing)
        yield self._step("checking_workspace", "done", self._workspace_detail(plan), t0)

        # "aggregate" retrieves too -- it just retrieves an ungoverned one-off,
        # which is exactly why it is marked unreconcilable below.
        label = ""
        if plan.path in ("retrieve", "widen", "aggregate"):
            t0 = perf_counter()
            yield self._step("retrieving", "start")
            label, meta, toks = self._retrieve_cube(
                llm, tenant_id, conversation_id, question, plan, artifact, caveats,
                schema_ctx)
            t_in_total += toks[0]
            t_out_total += toks[1]
            yield self._step("retrieving", "done",
                             self._retrieving_detail(label, meta), t0)
        else:
            # Not a hole in the trail -- the reason this turn is cheap. Rendered
            # as deliberately-not-run, never as failed.
            yield self._step("retrieving", "skipped",
                             self._retrieve_skipped_detail(plan))

        self._record_population(artifact, plan, meta)
        caveats.extend(self._population_caveats(plan, meta))

        t0 = perf_counter()
        yield self._step("analysing", "start")
        result, code, workspace_sql, toks = self._analyse(
            llm, tenant_id, conversation_id, question, plan, artifact)
        t_in_total += toks[0]
        t_out_total += toks[1]
        if result is None:
            # The analyst produced nothing usable and the turn falls back to the
            # legacy approved-knowledge path. Returning here without closing the
            # step would leave the trail pinned at "Analysing" forever, which is
            # the exact mystery the trail exists to remove -- the turn did not
            # stall, it went somewhere else.
            yield self._step("analysing", "abandoned",
                             "no analysis was produced -- answering from "
                             "approved knowledge instead", t0)
            return None
        yield self._step("analysing", "done",
                         self._analysing_detail(plan, code, workspace_sql), t0)

        rows = (result.result_summary if isinstance(result.result_summary, list)
                else [result.result_summary])

        # Describing a drop is not explaining one. On a causal question over a
        # governed population, take ONE further cut chosen to test the leading
        # hypothesis -- same rows, same filters, same window. Failure here is
        # never fatal: a diagnosis improves an answer, it is not a precondition
        # for one.
        t0 = perf_counter()
        yield self._step("interpreting", "start")
        diagnostic, probe_toks = self._diagnostic_probe(
            llm, tenant_id, conversation_id, question, plan, artifact, caveats,
            schema_ctx, rows)
        t_in_total += probe_toks[0]
        t_out_total += probe_toks[1]

        answer, syn_toks, chart_config = self._synthesize(
            llm, question, category,
            # On a retrieve the cube is created BY this turn, so plan.df_label
            # (the cube the plan meant to reuse) is empty and only the label
            # retrieval just issued names it. Without this, a first turn reads
            # its own complete cube and still disclaims it as possibly filtered.
            {"rows": rows,
             "frame_rows": self._frame_rows(tenant_id, conversation_id,
                                            plan.df_label or label),
             "full_cube": self._full_cube(tenant_id, conversation_id,
                                          plan.df_label or label),
             "diagnostic": diagnostic})
        t_in_total += syn_toks[0]
        t_out_total += syn_toks[1]
        yield self._step("interpreting", "done", "", t0)

        artifact.result_summary = result.result_summary
        artifact.chart_spec = getattr(result, "chart_spec", None) or chart_config
        artifact.assumptions = list(caveats)

        python_cells = [{"code": code, "df_label": artifact.datasets_used[0]
                         if artifact.datasets_used else "",
                         "result_summary": result.result_summary}] if code else []
        out = self._record(
            tenant_id, question, user_id, category, trace, answer,
            AnswerMode.ADAPTED_APPROVED_QUERY, "ANSWERED", False,
            [n.id for n in (query_nodes + defn_nodes)],
            citations=[{"node_id": n.id, "title": n.title,
                        "evidence_ref": n.evidence_ref,
                        "freshness": n.confidence.get("freshness", 0.0)}
                       for n in (query_nodes + defn_nodes)],
            facts=self._pipeline_facts(plan, artifact),
            caveats=caveats, tokens_in=t_in_total, tokens_out=t_out_total,
            queries_run=list(artifact.warehouse_sql),
            python_cells=python_cells, produced_df_label=label,
            conversation_id=conversation_id,
            extract_meta=asdict(meta) if meta is not None else {},
            analysis=artifact.to_dict())
        out["chart_config"] = artifact.chart_spec
        out["chart_data"] = rows
        self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.answer",
                       actor="stakeholder", resource=out["answer_id"], status="OK",
                       meta={"category": category, "path": plan.path,
                             "analysis": plan.analysis,
                             "population_hash": artifact.population_hash})
        return out

    # -- stage: is the question even answerable as asked? ----------------------
    def _planner_failure_refusal(self, tenant_id: str, conversation_id: str,
                                 plan: TurnPlan) -> str:
        """The refusal to put back to the user, or "" to carry on.

        When the planner returns nothing usable the turn falls through to a
        one-off query that re-derives its own FROM and WHERE from the question
        text. On a fresh conversation that is an honest best effort: the answer
        is marked unreconcilable, and there is no earlier number for it to
        contradict.

        Inside a conversation that already has a governed population it is
        something else. Caught live: turn 1 answered over DE for the last 30 days
        from a governed base, turn 2 asked to split "that same drop-off" by
        checkout type, and the improvised query dropped both filters and answered
        over all eight natcos and all 443 days -- 2,702,510 consent sessions
        where turn 1 had 136,573. It was correctly flagged unreconcilable, but
        nothing in the prose said the ground had moved, and the two read as one
        analysis. So: do not improvise a population on top of a governed one.
        Ask instead.
        """
        if not plan.planner_failed:
            return ""
        governed = [f for f in self.data_cache.list_available(tenant_id, conversation_id)
                    if f.get("population_hash")]
        if not governed:
            # Nothing to contradict. Leave the existing fallback alone.
            return ""
        names = sorted({f.get("base_view") for f in governed if f.get("base_view")})
        population = ", ".join(names) or "a governed population"
        applied: Dict[str, List[str]] = {}
        for frame in governed:
            for column, values in (frame.get("filters") or {}).items():
                applied.setdefault(column, list(values))
        slice_note = ""
        if applied:
            slice_note = (" It was filtered to "
                          + "; ".join(f"{c} = {', '.join(v)}" for c, v in sorted(applied.items()))
                          + ".")
        return (
            f"I could not work out what this follow-up is asking for, and I would "
            f"rather say so than guess. Everything in this conversation so far was "
            f"computed over {population}.{slice_note} To answer this one I would have "
            f"to write a fresh query and re-derive those filters from your wording -- "
            f"and if I got them wrong the result would read as a breakdown of the "
            f"previous answer while actually resting on a different set of rows. "
            f"Could you restate it, naming the breakdown you want?")

    def _timeframe_clarification(self, tenant_id: str, conversation_id: str,
                                 plan: TurnPlan, schema_ctx: SchemaContext) -> str:
        """The question to put back to the user, or "" to carry on.

        A base view carries no date filter on purpose: a window baked into one is
        inlined verbatim into every derived query, and nothing above it can reach
        past it. The consequence is that an unstated timeframe does not quietly
        mean "recently" -- it means the whole of history. Until now the planner
        simply proceeded on whatever window the model inferred, so a question that
        named no period got a default nobody chose and the answer read as though
        that period had been asked for. Ask instead, once, before the scan is
        spent.
        """
        if plan.timeframe_stated:
            return ""
        view = plan.base_view
        if view is None or not view.time_column:
            # No population, or a population with no time in it. There is nothing
            # to slice by, so there is nothing to ask about.
            return ""
        # A follow-up re-cutting a cube that already carries a window inherits it.
        # Asking again would interrogate the user about a decision they made one
        # turn ago.
        for frame in self.data_cache.list_available(tenant_id, conversation_id):
            if frame.get("time_start") and frame.get("time_end"):
                return ""
            # A cube filtered to July but grouped by country carries no date
            # column, so it measured no window -- and the user still gave it one.
            if frame.get("requested_time_start") and frame.get("requested_time_end"):
                return ""

        column = view.time_column
        extent = ""
        profile = (schema_ctx.profiles or {}).get(column)
        if profile is not None and profile.min_value and profile.max_value:
            # Attributed to profiling rather than stated as fact: a profile can be
            # stale or sampled, and a range presented as ground truth would be
            # believed.
            extent = (f" Profiling puts {column} between {profile.min_value} and "
                      f"{profile.max_value}.")
        return (
            f"Which period should this cover? The question does not say, and I would "
            f"rather ask than choose for you: the {view.name} population carries no "
            f"date filter, so with nothing specified this would be answered over the "
            f"whole of its history rather than over any recent window -- and the "
            f"answer would not look any different for it. Name a range and I will "
            f"slice on {column}.{extent}")

    # -- stage: diagnosing -----------------------------------------------------
    DIAGNOSTIC_PROMPT = """You have just measured something. Now work out WHY it is happening.

You are given the question and the result already in hand. Name the single most
likely explanation, then state the ONE further cut of the SAME population that would
best distinguish it from the alternatives.

Respond with STRICT JSON and nothing else:
{"hypothesis": "<one sentence: what you think is happening and why>",
 "friction_type": "matching" | "educational" | "operational" | "motivational",
 "cube": {"dimensions": [], "measures": [{"name": "", "expr": ""}]}}

- Draw dimensions and measures ONLY from the base view's listed columns.
- Pick the cut that would CHANGE YOUR MIND if the hypothesis is wrong. A cut that
  looks the same either way is worth nothing, however interesting.
- Prefer error and failure measures, and the intermediate steps of a funnel: those
  separate a broken flow (operational) from an uninterested one (motivational).
- Keep dimensions few. One or two is usually enough to settle a hypothesis, and a
  wide cube is refused outright.
- You may NOT change the filters or the time window. The point is to explain THIS
  result, and a cut over different rows would explain a different one."""

    def _diagnostic_probe(self, llm: Any, tenant_id: str, conversation_id: str,
                          question: str, plan: TurnPlan, artifact: AnalysisArtifact,
                          caveats: List[str], schema_ctx: SchemaContext,
                          rows: Any) -> Tuple[Optional[Dict[str, Any]], Tuple[int, int]]:
        """One extra, hypothesis-driven cut over the same population.

        The pipeline was one-shot: plan, retrieve, analyse, answer. Nothing in it
        could turn "tariffChange drops at 81% while acquisition drops at 7%" into
        "so go and test whether that is errors or abandonment", which is the step
        between reporting a number and explaining one.

        Bounded to exactly one probe, and only on a causal question over a
        governed population -- it costs a warehouse query, so it fires only where
        it can pay for itself. It may RE-CUT but never RE-SLICE: filters and the
        time window are inherited verbatim, because a probe over different rows
        would explain a different drop-off than the one being asked about.

        Every failure returns None and leaves the turn to answer from what it
        already has. A diagnosis is an improvement on an answer, never a
        precondition for one.
        """
        if not self._is_causal_question(question):
            return None, (0, 0)
        view = plan.base_view
        if view is None or plan.cube is None or plan.path == "aggregate":
            return None, (0, 0)

        prompt = (f"Question: {question}\n\n"
                  f"{self.base_views.render([view], tenant_id)}\n"
                  f"The result so far:\n{rows}\n")
        toks = (0, 0)
        try:
            res = llm.generate(prompt=prompt, system_prompt=self.DIAGNOSTIC_PROMPT,
                               temperature=0.0)
            toks = (getattr(res, "tokens_in", 0), getattr(res, "tokens_out", 0))
            parsed = _parse_json_block((res.text or "").strip(), context="diagnostic probe")
        except Exception as exc:  # noqa: BLE001 - a dead gateway degrades, never raises
            logger.warning("diagnostic probe failed: %s", exc)
            return None, toks
        if not parsed or not parsed.get("cube"):
            return None, toks

        spec = self._parse_cube(parsed["cube"], view)
        # The slice is NOT the model's to change -- inherited verbatim, whatever
        # it asked for.
        spec.filters = dict(plan.cube.filters)
        spec.time_column = plan.cube.time_column
        spec.time_start, spec.time_end = plan.cube.time_start, plan.cube.time_end
        cube_sql = self.base_views.compose_cube(view, spec, plan.profiles or {})
        if not cube_sql.ok:
            caveats.append(f"the diagnostic follow-up could not be sized "
                           f"({cube_sql.error}), so this answer describes the drop "
                           f"without testing a cause.")
            return None, toks

        sql, exec_res = self._run_composed(tenant_id, cube_sql.sql,
                                           f"diagnostic probe: {question}"[:200])
        artifact.warehouse_sql.append(sql)
        if exec_res is None or not exec_res.ok or exec_res.data is None:
            caveats.append(f"the diagnostic follow-up did not run "
                           f"({getattr(exec_res, 'error', 'no result')}), so this "
                           f"answer describes the drop without testing a cause.")
            return None, toks
        probe_rows = exec_res.data.to_dict(orient="records")
        return ({"hypothesis": str(parsed.get("hypothesis") or ""),
                 "friction_type": str(parsed.get("friction_type") or ""),
                 "dimensions": list(spec.dimensions),
                 "rows": probe_rows}, toks)

    # -- stage: understanding --------------------------------------------------
    @staticmethod
    def _record_semantics(artifact: AnalysisArtifact, schema_ctx: SchemaContext) -> None:
        resolution = schema_ctx.semantics
        if resolution is None:
            return
        artifact.semantics_used = ([m.name for m in getattr(resolution, "metrics", [])]
                                   + [d.name for d in getattr(resolution, "dimensions", [])])
        artifact.unresolved_terms = list(getattr(resolution, "unresolved_terms", []) or [])

    @staticmethod
    def _uncertainty_caveats(schema_ctx: SchemaContext) -> List[str]:
        """Say what was not known. A measure with no approved definition was
        computed from raw events against somebody's guess at what it means, and
        an unprofiled table means the filter literals were never checked against
        the data -- both change how much weight the number deserves."""
        out: List[str] = []
        resolution = schema_ctx.semantics
        for term in (getattr(resolution, "unresolved_terms", []) or []):
            out.append(f"'{term}' is not a defined metric for this company -- this figure "
                       f"was computed from raw events and has not been validated against "
                       f"an approved definition.")
        if schema_ctx.unprofiled:
            out.append(f"tables {', '.join(sorted(schema_ctx.unprofiled))} could not be "
                       f"profiled -- filter values were not verified against the data.")
        for collision in schema_ctx.collisions:
            out.append(collision)
        return out

    # -- step detail: what the trail actually says -----------------------------
    # A step that says only "Analysing" is a spinner with extra steps. These
    # build the one sentence per step that tells a user what the system is
    # spending their time and money on, out of what the pipeline already knows.

    @staticmethod
    def _understanding_detail(schema_ctx: Any) -> str:
        resolution = getattr(schema_ctx, "semantics", None)
        if resolution is None:
            return ""
        matched = [getattr(m, "name", "") for m in getattr(resolution, "metrics", ()) or ()]
        matched += [getattr(d, "name", "") for d in getattr(resolution, "dimensions", ()) or ()]
        matched = [m for m in matched if m]
        parts = []
        if matched:
            parts.append("matched " + ", ".join(matched))
        # An undefined measure is the single most important thing the trail can
        # say, so it is stated here and not left to the caveats alone.
        parts += [f"no defined metric matched \'{t}\'"
                  for t in (getattr(resolution, "unresolved_terms", ()) or []) if t]
        return "; ".join(parts)

    @staticmethod
    def _planning_detail(plan: TurnPlan) -> str:
        parts = []
        if plan.grain:
            parts.append("one row per " + ", ".join(plan.grain))
        parts.append("analysing in DuckDB" if plan.analysis == "workspace_sql"
                     else "analysing in Python")
        return "; ".join(parts)

    @staticmethod
    def _workspace_detail(plan: TurnPlan) -> str:
        """Plan A wrote `verdict.reason` for a human. Show it to one, verbatim."""
        verdict = plan.verdict
        if verdict is None:
            return "nothing in the workspace to reuse yet"
        reason = (getattr(verdict, "reason", "") or "").strip()
        return reason or f"decision: {getattr(verdict, 'decision', '')}"

    @staticmethod
    def _retrieving_detail(label: str, meta: Optional[ExtractMeta]) -> str:
        if meta is None:
            return f"retrieved {label}" if label else "queried the warehouse"
        head = f"{label} ({meta.row_count:,} rows)" if label else f"{meta.row_count:,} rows"
        if meta.truncated:
            head += " -- truncated, totals and rates may be understated"
        return head

    @staticmethod
    def _retrieve_skipped_detail(plan: TurnPlan) -> str:
        label = plan.df_label or ""
        reused = f"reusing {label}" if label else "the workspace already covers this"
        return f"{reused} -- no warehouse query needed"

    @staticmethod
    def _analysing_detail(plan: TurnPlan, code: str, workspace_sql: List[str]) -> str:
        """Athena and DuckDB are different claims about where a number came
        from, so the trail names which one ran rather than blurring them."""
        if workspace_sql:
            return f"DuckDB re-cut over {plan.df_label or 'the workspace'}"
        if code:
            return "Python: 1 cell"
        return "read the extract directly"

    # -- stage: checking the workspace ----------------------------------------
    @staticmethod
    def _verdict_labels(plan: TurnPlan) -> List[str]:
        labels = [plan.df_label] if plan.df_label else []
        verdict = plan.verdict
        if verdict is not None and getattr(verdict, "label", ""):
            labels.append(verdict.label)
        return list(dict.fromkeys(l for l in labels if l))

    # -- stage: retrieving -----------------------------------------------------
    def _retrieve_cube(self, llm: Any, tenant_id: str, conversation_id: str,
                       question: str, plan: TurnPlan, artifact: AnalysisArtifact,
                       caveats: List[str], schema_ctx: SchemaContext
                       ) -> Tuple[str, Optional[ExtractMeta], Tuple[int, int]]:
        sql, exec_res, toks = self._synthesize_and_execute_sql(
            llm, tenant_id, question, [], [], plan=plan, schema_ctx=schema_ctx)
        pages = list(getattr(self, "_sql_pages", []) or [])
        if len(pages) > 1:
            artifact.warehouse_sql.extend(pages)
        elif sql:
            artifact.warehouse_sql.append(sql)
        if exec_res is None or not exec_res.ok or exec_res.data is None:
            # A silent downgrade is the worst outcome here: the answer stops
            # being reconcilable and nothing says so. (_population_caveats adds
            # the "cannot be reconciled" line for the aggregate path itself; this
            # one names the cube that was lost getting there.)
            if plan.path == "aggregate":
                return "", None, toks
            # Downgrade, then actually retrieve on the downgraded path -- giving
            # up here would abandon the turn to the older code paths, and the
            # caveats explaining what was lost would go with it.
            plan.path = "aggregate"
            caveats.append(
                f"the governed cube could not be retrieved "
                f"({getattr(exec_res, 'error', 'no result')}), so this answer fell "
                f"back to a one-off query.")
            label, meta, retry_toks = self._retrieve_cube(
                llm, tenant_id, conversation_id, question, plan, artifact, caveats,
                schema_ctx)
            return label, meta, (toks[0] + retry_toks[0], toks[1] + retry_toks[1])

        label = self.data_cache.next_label(tenant_id, conversation_id)
        meta = self._extract_meta(label, question, plan, exec_res, sql)
        self.data_cache.put(tenant_id, conversation_id, label,
                            question[:CUBE_DESCRIPTION_CHARS],
                            exec_res.data, meta=meta)
        self.workspace.register(tenant_id, conversation_id, label)
        artifact.datasets_used.append(label)
        if plan.verdict is not None and getattr(plan.verdict, "supersedes", ""):
            # Both stay on disk and both stay registered: they share a
            # population_hash, so nothing already computed from the narrower cube
            # is invalidated by the wider one arriving.
            artifact.supersedes = plan.verdict.supersedes
        return label, meta, toks

    def _extract_meta(self, label: str, question: str, plan: TurnPlan,
                      exec_res: Any, sql: str) -> ExtractMeta:
        df = exec_res.data
        # On the aggregate path there is no governed population, so there is no
        # hash to carry. An empty population_hash reconciles with nothing, which
        # is the truth about a one-off query and must not be papered over with
        # whatever cube_sql happens to be lying around on the plan.
        cube_sql = plan.cube_sql if plan.path != "aggregate" else None
        time_column = plan.cube.time_column if plan.cube else ""
        start, end = "", ""
        if time_column and time_column in getattr(df, "columns", []):
            # From the FRAME, never from the plan: what the SQL asked for and
            # what the warehouse had are not the same thing, and coverage would
            # otherwise happily reuse a cube for a window it never contained.
            try:
                start, end = str(df[time_column].min()), str(df[time_column].max())
            except Exception:  # noqa: BLE001 - an unorderable column is not fatal
                start, end = "", ""
        ceiling = self.settings.policy.raw_extract_row_limit
        truncated = bool(getattr(exec_res, "truncated", False)
                         or len(df) >= ceiling
                         or any("truncated" in w for w in (exec_res.warnings or [])))
        return ExtractMeta(
            label=label, description=question[:CUBE_DESCRIPTION_CHARS],
            grain=list(plan.base_view.grain) if plan.base_view else [],
            columns=[str(c) for c in df.columns],
            dtypes={str(c): str(t) for c, t in df.dtypes.items()},
            row_count=len(df), truncated=truncated, sql=sql, created_at=now_iso(),
            base_view=plan.base_view.name if plan.base_view else "",
            population_hash=cube_sql.population_hash if cube_sql else "",
            projection_hash=cube_sql.projection_hash if cube_sql else "",
            dimensions=list(plan.cube.dimensions) if (plan.cube and cube_sql) else [],
            non_additive=list(cube_sql.non_additive) if cube_sql else [],
            filters=dict(plan.cube.filters) if (plan.cube and cube_sql) else {},
            time_column=time_column, time_start=start, time_end=end,
            requested_time_start=(plan.cube.time_start if plan.cube else ""),
            requested_time_end=(plan.cube.time_end if plan.cube else ""),
            grain_violated=bool(getattr(self, "_grain_violated", False)))

    # -- the population, and what it obliges us to say -------------------------
    def _record_population(self, artifact: AnalysisArtifact, plan: TurnPlan,
                           meta: Optional[ExtractMeta]) -> None:
        view = plan.base_view
        aggregate = plan.path == "aggregate"
        artifact.base_view = "" if aggregate or view is None else view.name
        artifact.base_view_approved = bool(not aggregate and plan.base_view_approved)
        artifact.base_view_grain_verified = bool(
            not aggregate and view is not None and view.grain_verified)
        if not aggregate and plan.cube_sql is not None:
            artifact.population_hash = plan.cube_sql.population_hash
            artifact.projection_hash = plan.cube_sql.projection_hash
            artifact.non_additive = list(plan.cube_sql.non_additive)
        if not aggregate and plan.cube is not None:
            artifact.slice_filters = dict(plan.cube.filters)
            artifact.dimensions = list(plan.cube.dimensions)
        if plan.requirement is not None:
            artifact.requirement = asdict(plan.requirement)
        if plan.verdict is not None:
            artifact.coverage = plan.verdict.to_dict()
        # Reconcilable means: there IS a population to compare against, and it
        # was verified to be at the grain it claims. Either one missing and a
        # comparison with another answer proves nothing.
        artifact.reconcilable = bool(artifact.population_hash
                                     and artifact.base_view_grain_verified)
        if meta is not None and meta.truncated:
            artifact.assumptions.append("truncated")

    def _population_caveats(self, plan: TurnPlan,
                            meta: Optional[ExtractMeta]) -> List[str]:
        """Four caveats, each attached to the condition that produces it. None of
        these may be dropped for brevity -- they are what stops a provisional
        number from reading like a settled one."""
        out: List[str] = []
        view = plan.base_view
        if plan.path == "aggregate" or view is None:
            out.append("dynamically generated SQL")
            out.append("no base view governs this query, so this number cannot be "
                       "reconciled against other answers in this conversation.")
            return out
        if not plan.base_view_approved:
            out.append(f"this answer rests on an unreviewed base view definition "
                       f"({view.name}); figures are provisional until it is approved.")
        if not view.grain_verified:
            # The cube guard refuses an unverified base, so reaching here means
            # something bypassed it. Worth knowing about.
            logger.error("answered over base view %s whose grain is unverified", view.name)
            out.append(f"base view {view.name} is not verified to be at the grain it "
                       f"claims, so every measure over it may be multiplied")
        for rule in view.attributions or []:
            out.append(self._attribution_caveat(rule))
        if meta is not None and meta.truncated:
            out.append(f"cube truncated at {meta.row_count} rows -- totals and rates "
                       f"may be understated")
        return out

    @staticmethod
    def _attribution_caveat(rule: AttributionRule) -> str:
        """Rides along on EVERY turn over the base, including pure reuse turns
        that ran no SQL at all -- the number still depends on the ranking."""
        how = {"highest_intent": "highest intent", "most_frequent": "most frequent value",
               "latest": "the latest value", "first": "the first value"}.get(
                   rule.strategy, rule.strategy)
        ranked = (f" ({' > '.join(rule.priority_values)})" if rule.priority_values else "")
        grain = ", ".join(rule.grain) or "the grain"
        return (f"{rule.column} attributed to each {grain} by {how}{ranked}; rows "
                f"touching multiple {rule.column} values are counted once, under their "
                f"highest-ranked one.")

    # -- stage: analysing ------------------------------------------------------
    def _analyse(self, llm: Any, tenant_id: str, conversation_id: str, question: str,
                 plan: TurnPlan, artifact: AnalysisArtifact
                 ) -> Tuple[Optional[Any], str, List[str], Tuple[int, int]]:
        """Compute the answer over the local workspace. Returns
        (result, python_code, workspace_sql, tokens); result None means nothing
        worked and the caller should fall through."""
        labels = self._analysis_labels(tenant_id, conversation_id, plan, artifact)
        if not labels:
            return None, "", [], (0, 0)
        if plan.path == "aggregate":
            # The one-off SQL already computed the answer; a second pass over its
            # own output would be a wasted LLM call and a wasted round of code.
            # (This is the pre-Task-14 behaviour, kept deliberately.)
            preview = self._preview_result(tenant_id, conversation_id, labels[0])
            return preview, "", [], (0, 0)
        t_in, t_out = 0, 0

        if plan.analysis == "workspace_sql":
            sqls, ws_res, toks = self._synthesize_and_execute_workspace_sql(
                llm, tenant_id, conversation_id, question, labels)
            t_in, t_out = toks
            artifact.workspace_sql.extend(sqls)
            if ws_res is not None and ws_res.ok:
                return self._workspace_result_as_analysis(ws_res), "", sqls, (t_in, t_out)
            # Fall back to PYTHON, not to a new warehouse query: the data is
            # already on this disk and a bad local query is not a reason to
            # re-bill the warehouse.
            logger.info("workspace SQL failed for tenant %s; falling back to Python",
                        tenant_id)

        code, py_res, toks = self._synthesize_and_execute_python(
            llm, tenant_id, conversation_id, question, labels[0])
        t_in += toks[0]
        t_out += toks[1]
        if py_res is None or not py_res.ok:
            # The data was fetched and is on disk. Throwing the turn away over a
            # failed analysis would re-bill the warehouse on the fallback path
            # for rows we already have, so interpret a preview of the frame
            # instead and say that is what happened.
            preview = self._preview_result(tenant_id, conversation_id, labels[0])
            if preview is None:
                return None, "", list(artifact.workspace_sql), (t_in, t_out)
            artifact.assumptions.append(
                "the analysis code could not be produced; this answer reads the first "
                "rows of the extract rather than a computed result")
            return preview, "", list(artifact.workspace_sql), (t_in, t_out)
        artifact.python_code.append(code)
        return py_res, code, list(artifact.workspace_sql), (t_in, t_out)

    def _preview_result(self, tenant_id: str, conversation_id: str,
                        label: str) -> Optional[Any]:
        from .execution.python_sandbox import PythonExecResult

        df = self.data_cache.get(tenant_id, conversation_id, label)
        if df is None:
            return None
        try:
            rows = df.head(3).to_dict(orient="records")
        except Exception:  # noqa: BLE001
            return None
        return PythonExecResult(ok=True, result_summary=rows,
                                result_shape={"rows": len(df),
                                              "columns": len(df.columns)})

    def _analysis_labels(self, tenant_id: str, conversation_id: str, plan: TurnPlan,
                         artifact: AnalysisArtifact) -> List[str]:
        """What the analysis may read: this turn's extract first, then whatever
        the coverage verdict named. Newest first, because a widen supersedes."""
        labels = list(artifact.datasets_used) + self._verdict_labels(plan)
        labels = list(dict.fromkeys(labels))
        if labels:
            artifact.datasets_used = labels
            return labels
        available = [f["label"] for f in
                     self.data_cache.list_available(tenant_id, conversation_id)]
        artifact.datasets_used = available[-1:] if available else []
        return artifact.datasets_used

    @staticmethod
    def _workspace_result_as_analysis(ws_res: Any) -> Any:
        """Present a WorkspaceResult the way the interpretation stage expects a
        PythonExecResult: a small row list, capped the same way."""
        from .execution.python_sandbox import MAX_RESULT_ROWS, PythonExecResult

        df = ws_res.data
        try:
            rows = df.head(MAX_RESULT_ROWS).to_dict(orient="records")
        except Exception:  # noqa: BLE001
            rows = []
        return PythonExecResult(ok=True, result_summary=rows,
                                result_shape={"rows": int(ws_res.row_count),
                                              "columns": len(getattr(df, "columns", []))})

    @staticmethod
    def _pipeline_facts(plan: TurnPlan, artifact: AnalysisArtifact) -> List[str]:
        facts: List[str] = []
        if artifact.base_view:
            facts.append(f"computed over the governed base view {artifact.base_view!r} "
                         f"(population {artifact.population_hash[:12]})")
        if plan.path in ("reuse", "widen") and artifact.coverage.get("reason"):
            facts.append(artifact.coverage["reason"])
        if artifact.workspace_sql:
            facts.append("re-cut locally in the analytical workspace; no warehouse query")
        if artifact.python_code:
            facts.append("analysed in Python over the materialised cube")
        return facts

    # -- helpers -------------------------------------------------------------
    def _llm_live(self, llm: Optional[Any] = None) -> bool:
        client = llm if llm is not None else getattr(self, "llm", None)
        if client is None:
            return False
        return getattr(client, "name", "null") != "null"

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
 "timeframe_stated": true | false,
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
- timeframe_stated: false when the QUESTION names no time window at all -- no dates,
  no "last 30 days", no "this quarter", no "since launch". Report it honestly and leave
  cube.time_start / cube.time_end EMPTY; do NOT invent a window to fill the gap. Base
  views carry no date filter, so an unstated window silently means the whole of history,
  and the user gets an answer about a period nobody chose. Saying false costs nothing:
  the user is simply asked which period they meant. Set it true when the question names
  a window, and also when it is a follow-up plainly re-cutting an earlier cube, which
  already carries one.
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
        # A follow-up is a rewrite of an earlier request, so the planner is given
        # the conversation rather than a summary of its leftovers.
        history = self._conversation_thread(tenant_id, conversation_id)

        parsed = self._ask_planner(llm, prompt, schema_ctx, history=history)
        if parsed is None and any(f.get("population_hash") for f in frames):
            # Ask once more, but ONLY where giving up would cost something. This
            # failure is intermittent -- the same call, same prompt, usually
            # returns perfectly good JSON -- so one retry recovers most of them
            # for the price of one planning call. On a conversation with no
            # governed cube in it there is nothing for a fallback answer to
            # contradict, so the retry would be cost without a benefit.
            parsed = self._ask_planner(llm, prompt, schema_ctx, history=history)
        if parsed is None:
            return TurnPlan(path="aggregate", analysis="python", planner_failed=True,
                            rationale="the planner produced no usable plan")

        plan = self._resolve_plan(tenant_id, parsed, schema_ctx)
        if plan is None:
            return TurnPlan(path="aggregate", analysis="python", planner_failed=True,
                            rationale="the planner named a base view that does not exist")

        # The guard refused: feed the culprit back once. Do not silently drop a
        # dimension on the model's behalf -- the answer would then be to a
        # question nobody asked.
        if plan.cube_sql is not None and not plan.cube_sql.ok:
            retry = self._ask_planner(
                llm, prompt + "\n\n" + self._guard_feedback(plan.cube_sql), schema_ctx,
                history=history)
            retried = self._resolve_plan(tenant_id, retry, schema_ctx) if retry else None
            if retried is None or retried.cube_sql is None or not retried.cube_sql.ok:
                reason = plan.cube_sql.error
                return TurnPlan(path="aggregate", analysis=plan.analysis,
                                rationale=f"no cube could be composed: {reason}",
                                # Keep what _resolve_plan already found out -- a
                                # grain violation is the reason this turn fell
                                # through and the user has to be told which base.
                                caveats=list(plan.caveats) + [
                                    f"the requested breakdown could not be sized: {reason}"])
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
                dims = list(f.get("dimensions") or [])
                # Coverage matches a measure by the NAME the cube stores it
                # under, so a requirement that renames an existing measure --
                # session_count for a COUNT(*) already sitting there as
                # checkout_sessions -- reads as missing and re-queries the
                # warehouse. Show the names so they can be reused verbatim.
                measures = [c for c in (f.get("columns") or []) if c not in dims]
                lines.append(
                    f"- {f['label']}: {f.get('description', '')} | base_view="
                    f"{f.get('base_view') or 'none'} | dimensions={dims} "
                    f"| measures={measures} | slice={self._render_slice(f)} "
                    f"| rows={f.get('row_count')} | truncated={f.get('truncated')} "
                    f"| sample={f.get('sample')}")
            lines.append(
                "If a measure you need is already listed above, name it EXACTLY as "
                "that cube names it. A measure that means the same thing under a new "
                "name counts as missing and costs a fresh warehouse query.")
            lines.append(
                "`slice` is what that cube was actually filtered to. A follow-up that "
                "re-cuts it MUST repeat those filters and that window verbatim unless "
                "the question asks to change them -- dropping one silently answers a "
                "different question over different rows, while reading as a breakdown "
                "of the previous answer.")
            lines.append("")
        if self._is_causal_question(question):
            lines.extend([self.DIAGNOSTIC_MEASURE_PROMPT, ""])
        return "\n".join(lines)

    # How many earlier turns of the conversation the planner is shown. Input
    # tokens are cheap -- and cheaper still under prompt caching, which is why
    # the stable material goes in the system message and the volatile thread
    # after it -- but a context window is finite and a long-running conversation
    # is not, so this is bounded rather than unbounded.
    PLANNER_HISTORY_TURNS = 8

    def _conversation_thread(self, tenant_id: str,
                             conversation_id: str) -> List[Dict[str, str]]:
        """Earlier turns of this conversation, as chat messages for the planner.

        A follow-up is a rewrite of an earlier request, and until now the model
        was never shown the request it was rewriting -- only a one-line summary
        of the cube that came out of it. "Now split THAT by checkout type" cannot
        be resolved against a summary; it needs the turn it refers to.

        Each earlier turn becomes a user message (the question, verbatim) and an
        assistant message (what the planner decided, in the SAME JSON shape it is
        being asked to emit now, so the model reads its own prior output in the
        format it must answer in). Turns that decided no population say so in
        prose instead -- rendering them as a plan would invite the model to build
        on a decision nobody made. Clarifications stay in, because the user's
        next message is usually the answer to one.
        """
        if not conversation_id:
            return []
        conversation = self.get_conversation(tenant_id, conversation_id)
        if not conversation:
            return []
        out: List[Dict[str, str]] = []
        for message in (conversation.get("messages") or [])[-self.PLANNER_HISTORY_TURNS:]:
            question = (message.get("question") or "").strip()
            if not question:
                continue
            out.append({"role": "user", "content": question})
            out.append({"role": "assistant",
                        "content": self._render_prior_turn(message)})
        return out

    @staticmethod
    def _render_prior_turn(message: Dict[str, Any]) -> str:
        """What the planner decided on an earlier turn, for the assistant slot."""
        requirement = ((message.get("analysis") or {}).get("requirement")) or {}
        if requirement.get("base_view"):
            return dump_json({
                "base_view": requirement.get("base_view", ""),
                "cube": {
                    "dimensions": list(requirement.get("dimensions") or []),
                    "measures": [{"name": m.get("name", ""), "expr": m.get("expr", "")}
                                 for m in (requirement.get("measures") or [])
                                 if isinstance(m, dict)],
                    "filters": dict(requirement.get("filters") or {}),
                    "time_column": requirement.get("time_column", ""),
                    "time_start": _as_date_literal(requirement.get("time_start")),
                    "time_end": _as_date_literal(requirement.get("time_end")),
                },
            })
        if message.get("status") == "NEEDS_CLARIFICATION":
            # Kept as prose, and kept at all: the user's NEXT message is the reply
            # to this, and without it that reply reads as a non-sequitur.
            return (message.get("answer") or "").strip()
        return ("(no population was chosen for that turn -- it was answered with a "
                "one-off query, so there is no plan to build on)")

    # A question asking why something happened needs the columns that could
    # answer it. Matched on intent rather than on the literal word "why": "what
    # is driving the drop-off" is the same question.
    _CAUSAL_RE = re.compile(
        r"\bwhy\b|\bdriv(?:e|es|ing|er|ers)\b|\bcaus(?:e|es|ing|al)\b|"
        r"\broot\s*cause\b|\bdrop[\s-]?off\b|\bdrop[\s-]?out\b|\bchurn\b|"
        r"\bleak(?:age|ing)?\b|\bfriction\b|\bbottleneck\b|\bexplain\b|"
        r"\breason(?:s)?\b|\bdiagnos", re.IGNORECASE)

    DIAGNOSTIC_MEASURE_PROMPT = (
        "This question asks WHY something is happening, not just how much of it there "
        "is. Two endpoint counts can only size a drop -- they can never explain one. "
        "From the chosen base view's listed measure columns, ALSO include:\n"
        "  - any error or failure counts it carries, which are what separate a broken "
        "flow from an uninterested user;\n"
        "  - the intermediate steps between the two endpoints you are comparing, which "
        "are what show WHERE inside the drop people actually leave.\n"
        "Add these as MEASURES, NOT DIMENSIONS. Measures do not multiply the cube's "
        "cell count, so they are nearly free; an extra dimension is what gets a cube "
        "refused. If the base carries no such column, say so in rationale rather than "
        "inventing one."
    )

    @classmethod
    def _is_causal_question(cls, question: str) -> bool:
        return bool(cls._CAUSAL_RE.search(question or ""))

    @staticmethod
    def _render_slice(frame: Dict[str, Any]) -> str:
        """What a cached cube was filtered to, as the planner needs to repeat it.

        Said explicitly even when there is nothing, because silence is ambiguous:
        the planner cannot tell "this cube is unfiltered" from "its filters were
        not passed on", and that ambiguity is exactly what let a follow-up drop a
        country filter and answer over eight of them.
        """
        parts = [f"{column}={','.join(str(v) for v in values)}"
                 for column, values in sorted((frame.get("filters") or {}).items())
                 if values]
        # The requested window is preferred over the measured one: the measured
        # pair is only populated when the time column is among the cube's own
        # columns, so a cube filtered to July but grouped by country measures
        # nothing while still very much having a window.
        start = frame.get("requested_time_start") or frame.get("time_start")
        end = frame.get("requested_time_end") or frame.get("time_end")
        column = frame.get("time_column")
        if column and start and end:
            parts.append(f"{column}={start}..{end}")
        return "; ".join(parts) if parts else "no filters, no time window"

    def _ask_planner(self, llm: Any, prompt: str, schema_ctx: SchemaContext,
                     history: Optional[List[Dict[str, str]]] = None
                     ) -> Optional[Dict[str, Any]]:
        system = self.PLAN_SYSTEM_PROMPT
        if self._has_fanout(schema_ctx):
            system += "\n\n" + self.PROPOSAL_PROMPT
        system += "\n\n" + self._render_attribution_pattern(
            self._fanout_rules(schema_ctx))
        # The system message carries the stable material and comes first, so the
        # cacheable prefix stays valid across the turns of a conversation; the
        # volatile thread and the new question follow it.
        #
        # The system prompt goes INSIDE the list, not beside it: the gateway
        # ignores `system_prompt` entirely whenever `messages` is supplied, so
        # leaving it outside would silently drop the whole planner contract.
        messages = ([{"role": "system", "content": system}]
                    + list(history or [])
                    + [{"role": "user", "content": prompt}])
        try:
            res = llm.generate(prompt=prompt, system_prompt=system,
                               messages=messages, temperature=0.0)
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

        view = self._verify_grain(tenant_id, view, approved, caveats)

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
            timeframe_stated=bool(parsed.get("timeframe_stated", True)),
            rationale=str(parsed.get("rationale") or ""), caveats=caveats,
            # Attribution is a property of the population. On an existing base it
            # is already baked in and inherited; the planner may not override it.
            attributions=[])

    def _verify_grain(self, tenant_id: str, view: BaseView, approved: bool,
                      caveats: List[str]) -> BaseView:
        """Probe the base's grain once per population_hash, before composing.

        A tenant asking twenty questions against one approved base pays for this
        once, ever. It runs before compose_cube because after GROUP BY there is
        nothing left to see.
        """
        if not view.grain or not self.base_views.needs_grain_check(view):
            return view
        try:
            probe = self.base_views.compose_grain_probe(view)
        except ValueError:
            return view
        _, res = self._run_composed(tenant_id, probe, "base view grain verification")
        if res is None or not res.ok or res.data is None or not len(res.data):
            # Unverified is not the same as violated. Say so and let the cube
            # refusal (which fails closed) decide the turn.
            caveats.append(f"the grain of base view {view.name} could not be verified "
                           f"this turn, so no cube may be built over it yet")
            return view
        row = res.data.iloc[0]
        try:
            rows, keys = int(row["row_count"]), int(row["key_count"])
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("unreadable grain probe result for %s: %s", view.name, exc)
            return view
        try:
            null_keys = int(row["null_keys"] or 0)
        except (KeyError, TypeError, ValueError):
            null_keys = 0       # older stored probe shape; treat as "not measured"
        view = self.base_views.record_grain_check(tenant_id, view, rows, keys, null_keys)
        if not view.grain_verified:
            if null_keys:
                caveats.append(
                    f"base view {view.name} has {null_keys:,} rows with no "
                    f"{', '.join(view.grain)} at all -- they are not at the grain it "
                    f"claims and all of them collapse into one bucket. They are not "
                    f"duplicates; exclude them in the base or key it on something "
                    f"always present.")
            else:
                caveats.append(
                    f"base view {view.name} returns {rows:,} rows for {keys:,} distinct "
                    f"{', '.join(view.grain)} keys -- it is not at the grain it claims, "
                    f"and every measure over it would be multiplied")
            if approved:
                # A governance failure, not a modelling accident: a human approved
                # this definition. Surface it, never patch it.
                logger.error("APPROVED base view %s for tenant %s fails its own grain "
                             "claim (%s rows, %s distinct %s)", view.name, tenant_id,
                             rows, keys, view.grain)
        return view

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
            # Grain verification is a MEASUREMENT, never a declaration. The
            # proposal is filtered to the fields above, so it cannot set these --
            # but say so explicitly, because a base that could certify itself
            # would make the probe decorative.
            view.grain_verified, view.grain_checked_hash = False, ""
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
            time_start=_as_date_literal(raw.get("time_start")),
            time_end=_as_date_literal(raw.get("time_end")))

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

            return _extract_sql_block(text), (getattr(res, "tokens_in", 0), getattr(res, "tokens_out", 0))
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

        self._grain_violated = False
        # Every page's SQL, not just the survivor. The return signature carries
        # one string, so a 12-trip fetch used to be indistinguishable from a
        # 1-trip fetch in the audit trail -- you could not tell from the record
        # how much was pulled, or whether paging stopped early.
        self._sql_pages: List[str] = []
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
            self._sql_pages = list(sql_run)
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

        self._sql_pages = list(sql_run)
        if len(sql_run) > 1:
            warnings.append(
                f"this cube arrived in {len(sql_run)} keyset pages ({total} rows "
                f"total); every page's SQL is recorded")
        combined = pd.concat(pages, ignore_index=True) if pages else pd.DataFrame()
        if truncated and len(combined) > ceiling:
            combined = combined.head(ceiling)

        # The ID-grain path keeps a row-level check. On a base whose grain probe
        # passed this cannot fire; if it does, the base changed underneath the
        # stored check, so trust neither artifact -- say so rather than quietly
        # summing rows that are already multiplied.
        grain = list(plan.base_view.grain) if plan.base_view else []
        if (grain and page_keys == grain and len(combined)
                and all(k in combined.columns for k in grain)):
            if bool(combined.duplicated(subset=grain).any()):
                self._grain_violated = True
                warnings.append(
                    f"this extract claims one row per {', '.join(grain)} but came back "
                    f"with duplicate keys, so any total over it is double-counted; the "
                    f"base view changed since its grain was verified")
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

    # Both analysis paths read the same cubes, so they get the same rules. A cube
    # is not a row-per-event table, an averaged measure is stored decomposed, and
    # a non-additive measure cannot be rolled up at all -- get any of these wrong
    # and the number is wrong in a way that looks right.
    CUBE_READING_RULES = (
        "The DataFrame is a CUBE: one row per combination of {dimensions}, with "
        "pre-aggregated measures. To answer at fewer dimensions, SUM over the ones "
        "you are dropping -- never treat a row as a single event.\n"
        "An averaged measure is stored as `<name>_sum` and `<name>_count`. To read "
        "it, divide: SUM(x_sum) / NULLIF(SUM(x_count), 0). Never average `x_sum`, "
        "and never average an average.\n"
        "SELECT the measures your answer has to state, including any you ranked or "
        "filtered by. A query that orders by a share and then selects only the "
        "label returns a name with no number behind it, and the answer cannot "
        "report the share it just computed.\n"
        "Do not LIMIT away rows the answer needs to justify itself. A cube of a few "
        "hundred rows or fewer should come back whole and ordered; `LIMIT 1` is only "
        "right when the single row genuinely is the entire answer.\n"
        "{non_additive}")

    @classmethod
    def _cube_rules(cls, dimensions: List[str], non_additive: List[str]) -> str:
        if non_additive:
            na = (f"These measures are NON-ADDITIVE: {non_additive}. Do not SUM them "
                  f"and do not group them to fewer columns than the cube already "
                  f"carries -- the numbers would be wrong in a way that looks right.")
        else:
            na = "Every measure in this cube is additive."
        return cls.CUBE_READING_RULES.format(
            dimensions=dimensions or "the cube's own columns", non_additive=na)

    def _frame_cube_facts(self, desc: Dict[str, Any]) -> Tuple[List[str], List[str]]:
        return (list(desc.get("dimensions") or []),
                list(desc.get("non_additive") or []))

    # -- the workspace-SQL analysis path -------------------------------------
    def _workspace_prompt(self, question: str, frames: List[Dict[str, Any]],
                          prior_sql: str = "", prior_error: str = "") -> Tuple[str, str]:
        lines = [f"Question: {question}", "",
                 "These views are registered in the local workspace:"]
        dims: List[str] = []
        non_additive: List[str] = []
        for f in frames:
            dims.extend(f.get("dimensions") or [])
            non_additive.extend(f.get("non_additive") or [])
            lines.append(
                f"- {f['label']}: {f.get('description', '')}\n"
                f"    columns={f.get('columns')}\n"
                f"    dimensions={f.get('dimensions')} | non_additive="
                f"{f.get('non_additive')} | rows={f.get('row_count')}\n"
                f"    sample={f.get('sample')}")
        if prior_sql:
            lines.extend(["", f"Your previous query failed:\n{prior_sql}",
                          f"\nError:\n{prior_error}",
                          "\nWrite a corrected query that fixes this specific problem."])
        system = (
            "You are an expert data analyst. Answer the question with a single "
            "DuckDB SELECT over the views listed -- they are already registered, "
            "so do not create, attach, copy or install anything, and do not read "
            "from any file or table not listed. The dialect is DuckDB.\n\n"
            + self._cube_rules(sorted(set(dims)), sorted(set(non_additive)))
            + "\n\nReturn ONLY the SQL in a ```sql block. If the question cannot be "
              "answered from these views, output NOTHING.")
        return "\n".join(lines), system

    def _synthesize_and_execute_workspace_sql(
            self, llm: Any, tenant_id: str, conversation_id: str, question: str,
            labels: Optional[List[str]] = None,
            max_attempts: int = 3) -> Tuple[List[str], Any, Tuple[int, int]]:
        """Mirror of _synthesize_and_execute_python for local DuckDB.

        On repeated failure the caller falls back to the PYTHON path, not to a
        new warehouse query: the data is already on this disk, and a bad local
        query is not a reason to re-bill the warehouse.

        Returns (sql_attempts_run, WorkspaceResult_or_None, tokens).
        """
        frames = [f for f in self.data_cache.list_available(tenant_id, conversation_id)
                  if not labels or f["label"] in labels]
        if not frames:
            return [], None, (0, 0)
        for f in frames:
            self.workspace.register(tenant_id, conversation_id, f["label"])

        run: List[str] = []
        prior_sql, prior_error = "", ""
        t_in_total, t_out_total = 0, 0
        for attempt in range(1, max_attempts + 1):
            prompt, system = self._workspace_prompt(question, frames, prior_sql, prior_error)
            try:
                res = llm.generate(prompt=prompt, system_prompt=system, temperature=0.0)
                text = (res.text or "").strip() if res and hasattr(res, "text") else ""
                t_in_total += getattr(res, "tokens_in", 0)
                t_out_total += getattr(res, "tokens_out", 0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("workspace SQL synthesis failed: %s", exc, exc_info=True)
                return run, None, (t_in_total, t_out_total)
            sql = _extract_sql_block(text)
            if not sql:
                break                       # the model declined; retrying will not help
            out = self.workspace.query(tenant_id, conversation_id, sql)
            run.append(out.sql or sql)
            if out.ok:
                return run, out, (t_in_total, t_out_total)
            logger.warning("workspace SQL failed for tenant %s (attempt %d/%d): %s",
                           tenant_id, attempt, max_attempts, out.error)
            prior_sql, prior_error = sql, out.error
        return run, None, (t_in_total, t_out_total)

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
        dimensions, non_additive = self._frame_cube_facts(frame_desc)
        sys_prompt = (
            "You are an expert data analyst. Write pandas Python code that computes the "
            f"answer to the question using the DataFrame `{df_label}` (already in scope -- "
            "do not redefine it or read it from any file/database). Assign your final "
            "answer to a variable named `result` (a scalar, dict, list, or small DataFrame "
            "-- not the full raw DataFrame unmodified). Only `pandas` (as `pd`), `numpy`, "
            "`math`, `statistics`, `datetime`, `collections`, and `re` may be imported; no "
            "file, network, or system access is available and will be rejected.\n\n"
            + self._cube_rules(dimensions, non_additive) +
            "\n\nIf a chart would make the finding clearer, also assign `chart = "
            "{'kind': ..., 'x': ..., 'y': ..., 'series': ..., 'title': ...}` describing a "
            "chart over the rows in `result`. It is a SPEC, not a drawing: do not attempt "
            "to plot, render, or save an image.\n\n"
            "Return ONLY the Python code in a ```python block. If the question can't be "
            "answered from this DataFrame, output NOTHING."
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

            # Load from Parquet in the worker when the extract is on disk: a
            # cube can be large, and pickling it across the process boundary
            # costs a second full copy of it in this process.
            path = self.extract_store.parquet_paths(
                tenant_id, conversation_id).get(df_label, "")
            if path:
                exec_res = run_python_sandboxed(
                    decision.approved_code, dataframe_paths={df_label: path},
                    memory_mb=EXTRACT_MEMORY_MB, timeout_s=EXTRACT_TIMEOUT_S)
            else:
                exec_res = run_python_sandboxed(
                    decision.approved_code, {df_label: df},
                    memory_mb=EXTRACT_MEMORY_MB, timeout_s=EXTRACT_TIMEOUT_S)
            if exec_res.ok:
                return decision.approved_code, exec_res, (t_in_total, t_out_total)

            logger.warning("synthesized Python execution failed for tenant %s "
                           "(attempt %d/%d): %s", tenant_id, attempt, max_attempts, exec_res.error)
            prior_code, prior_error = decision.approved_code, exec_res.error

        return "", None, (t_in_total, t_out_total)

    # A cube small enough to show whole is shown whole. The analysis step
    # sometimes writes a query that ranks by a measure and then selects only the
    # label, leaving synthesis a name with no number behind it -- live, that
    # produced "I cannot determine which service line has the largest share"
    # from a cube that held every figure needed. Reading the cube again costs
    # nothing: it is already fetched, population-hashed and registered in the
    # in-process DuckDB workspace, so this is not another warehouse trip.
    FULL_CUBE_MAX_ROWS = 200

    def _full_cube(self, tenant_id: str, conversation_id: str,
                   label: str) -> Optional[List[Dict[str, Any]]]:
        if not label or not self.workspace:
            return None
        if not (0 < self._frame_rows(tenant_id, conversation_id, label)
                <= self.FULL_CUBE_MAX_ROWS):
            return None
        try:
            res = self.workspace.query(
                tenant_id, conversation_id,
                f"SELECT * FROM {label} LIMIT {self.FULL_CUBE_MAX_ROWS + 1}")
        except Exception as exc:  # noqa: BLE001 - context is best-effort
            logger.warning("could not read cube %s whole: %s", label, exc)
            return None
        df = getattr(res, "data", None)
        if df is None or not len(df) or len(df) > self.FULL_CUBE_MAX_ROWS:
            return None
        from .execution.dataframe_cache import _json_safe
        return [{k: _json_safe(v) for k, v in row.items()}
                for row in df.to_dict(orient="records")]

    def _frame_rows(self, tenant_id: str, conversation_id: str, label: str) -> int:
        """Rows in the cube a turn read, so synthesis can tell a whole cube from
        a slice of one. Unknown label -> 0, which degrades to the cautious
        wording rather than asserting completeness."""
        if not label:
            return 0
        try:
            for f in self.data_cache.list_available(tenant_id, conversation_id):
                if f.get("label") == label:
                    return int(f.get("row_count") or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not size frame %s: %s", label, exc)
        return 0

    # Synthesis sees the cube, not a sample of it. The pipeline goes to real
    # trouble to produce a population-hashed, reconcilable cube; narrating it
    # from an arbitrary handful of rows throws that away at the last step, and
    # the resulting sentence reads as complete whether or not it is. Bound the
    # prompt by characters rather than a fixed row count so a small cube always
    # arrives whole.
    SYNTHESIS_CONTEXT_CHARS = 12000

    @classmethod
    def _measure_key(cls, rows: Sequence[Dict[str, Any]]) -> str:
        """The column to rank by when the cube will not fit whole: the first one
        that is numeric in every row. Without a deterministic order, a truncated
        cube shows whichever rows the warehouse happened to emit first."""
        for key in (rows[0].keys() if rows and isinstance(rows[0], dict) else []):
            if all(isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)
                   for r in rows):
                return str(key)
        return ""

    @classmethod
    def _data_context(cls, rows: Optional[Sequence[Any]],
                      columns: Optional[Sequence[str]] = None,
                      frame_rows: int = 0,
                      full_cube: Optional[Sequence[Any]] = None,
                      diagnostic: Optional[Dict[str, Any]] = None) -> str:
        """`frame_rows` is how many rows the cube being read actually holds.

        Without it there is no way to tell a full cube read from an
        `ORDER BY ... LIMIT 1`, and the choice is between two wrong answers:
        call every result complete, and a top-1 re-cut becomes "the only
        category present, therefore 100% of sessions"; or hedge every result,
        and a genuinely complete distribution gets disclaimed into uselessness.
        Both happened live. With it, each result is described as what it is.
        """
        tail = cls._diagnostic_context(diagnostic)
        if not rows:
            return tail
        rows = list(rows)
        col_line = f"\nColumns: {list(columns)}" if columns else ""
        if frame_rows and len(rows) < frame_rows:
            if full_cube and len(full_cube) == frame_rows:
                # The query returned a slice, but the cube it came from is small
                # enough to show whole -- so show it, and the answer can quote
                # figures the slice dropped instead of declining for want of them.
                return (f"\nData context -- the analysis step returned {len(rows)} row(s): "
                        f"{rows}\nThat is a ranked or filtered slice. The COMPLETE cube it "
                        f"came from, all {frame_rows} row(s) over this population, is below "
                        f"-- use it for anything the slice cannot support, and account for "
                        f"every row when describing the whole: {list(full_cube)}{col_line}"
                        + tail)
            head = (f"\nData context -- a RANKED OR FILTERED SUBSET: {len(rows)} of the "
                    f"{frame_rows} rows in the cube. The rows not shown still exist, so "
                    f"do not say these are the only ones, and do not compute a share or "
                    f"a total from them")
        elif frame_rows:
            head = (f"\nData context -- the COMPLETE cube over this population, all "
                    f"{frame_rows} row(s). Account for every one of them; shares and "
                    f"totals are valid here")
        else:
            head = (f"\nData context -- the complete output of the query that ran, all "
                    f"{len(rows)} row(s). That query may itself have ranked or limited "
                    f"the population, so do not conclude no other rows exist")
        whole = f"{head}: {rows}{col_line}"
        if len(whole) <= cls.SYNTHESIS_CONTEXT_CHARS:
            return whole + tail

        ranked, key = rows, cls._measure_key(rows)
        if key:
            ranked = sorted(rows, key=lambda r: r.get(key) or 0, reverse=True)
        shown = ranked
        while shown and len(repr(shown)) > cls.SYNTHESIS_CONTEXT_CHARS:
            shown = shown[:-max(1, len(shown) // 10)]
        by = f" by {key}" if key else ""
        omitted = len(rows) - len(shown)
        return (f"\nData context -- PARTIAL. The result has {len(rows)} rows; the top "
                f"{len(shown)}{by} are shown and {omitted} are NOT. Totals and shares "
                f"cannot be computed from this, and it must not be described as the "
                f"whole distribution: {shown}{col_line}"
                + tail)

    # The prompt that writes every answer. What it asked for before was "a
    # cautious internal analytics assistant" that would "state what you know and
    # what data you would need", in "2-3 sentences" -- so the tool reliably
    # stopped at description and signed off by listing what it lacked. That read
    # as a refusal to analyse, because it is what was asked for. AGENTS.md Part 2
    # (the friction taxonomy) and Part 3 (descriptive -> diagnostic ->
    # prescriptive) had never reached a prompt at all.
    ANSWER_SYSTEM_PROMPT = (
        "You are an internal analytics assistant writing for a stakeholder who will "
        "have to defend this number in a meeting.\n\n"

        "FORMAT. Respond with a strict JSON object:\n"
        '{"answer": "<markdown>", "chart_config": {"type": '
        '"LineChart|BarChart|AreaChart|ScatterChart", "xKey": "col_name", '
        '"series": [{"key": "col_name"}]}}\n'
        "Omit chart_config if no chart applies. The `answer` value is markdown: open "
        "with AT MOST five '- ' bullets carrying the findings that would change a "
        "decision, then a blank line, then a detailed analysis under a '## Detailed analysis' "
        "heading. Fewer bullets is better -- do not pad to five.\n\n"

        "SEQUENCE. Work through all three layers in order, and NEVER jump straight to "
        "recommendations:\n"
        "1. DESCRIPTIVE -- establish the hard facts. What is happening, and how big is "
        "it. Size every claim.\n"
        "2. DIAGNOSTIC -- why is it happening? Form hypotheses from the data actually "
        "in front of you, and say which the data supports, which it contradicts, and "
        "which it cannot settle either way.\n"
        "3. PRESCRIPTIVE -- what to do about it, most valuable first, and only after "
        "1 and 2.\n\n"

        "DIAGNOSING A DROP-OFF OR BOTTLENECK. Categorise each one as exactly one of:\n"
        "  - MATCHING FRICTION: the wrong users arrived for this product.\n"
        "  - EDUCATIONAL FRICTION: users do not understand the value, or the next step.\n"
        "  - OPERATIONAL FRICTION: something is broken -- a bug, an error, latency, a "
        "flow that dead-ends.\n"
        "  - MOTIVATIONAL FRICTION: users understand it and simply lack a reason to "
        "continue.\n"
        "Name the type and say what in the data points to it. Error or failure counts "
        "CONCENTRATED in one segment are evidence of operational friction; a drop of "
        "similar size across every segment rarely is, and points to matching or "
        "motivational friction instead. If the data cannot separate them, say which "
        "single measurement would.\n\n"

        "EVIDENCE.\n"
        "- Never invent a figure. Every number must come from the data context.\n"
        "- The data context states whether it is the COMPLETE result or only part of "
        "one. When it is complete and small, account for every row -- do not describe "
        "a distribution while silently omitting categories. When it is partial, say "
        "so, and never present it as the whole picture.\n"
        "- A figure that contradicts another (a later funnel step exceeding an earlier "
        "one, a negative drop-off) is a finding about the DEFINITIONS, not a number to "
        "report as fact. Say which definition must be wrong.\n"
        "- Close by naming what you could not test and the one piece of data that "
        "would settle it. Keep it short, and put it last -- it is a closing note, and "
        "not as a substitute for analysing what you already have."
    )

    @staticmethod
    def _diagnostic_context(diagnostic: Optional[Dict[str, Any]]) -> str:
        """The follow-up cut, framed as what it is.

        This is the model's OWN hypothesis coming back to it alongside evidence.
        Presented as a finding it would be laundered into the answer as though it
        had been established, which is precisely the move the whole descriptive ->
        diagnostic sequence exists to prevent. So it is labelled a hypothesis, and
        the model is told to say which way the evidence actually falls.
        """
        if not diagnostic or not diagnostic.get("rows"):
            return ""
        friction = diagnostic.get("friction_type") or "unclassified"
        return (
            f"\n\nDIAGNOSTIC FOLLOW-UP. Before seeing the rows below you proposed this "
            f"HYPOTHESIS -- it is a guess, not a finding, and nothing has confirmed it "
            f"yet: \"{diagnostic.get('hypothesis', '')}\" (suspected {friction} "
            f"friction).\nTo test it, this second cut was taken over the SAME rows as "
            f"the result above -- same population, same filters, same window -- broken "
            f"down by {diagnostic.get('dimensions') or 'no dimension'}:\n"
            f"{diagnostic['rows']}\n"
            f"Say explicitly whether this evidence SUPPORTS, CONTRADICTS, or CANNOT "
            f"SETTLE the hypothesis, and if it contradicts it, say what it points to "
            f"instead. Do not repeat the hypothesis as though the test had confirmed it."
        )

    def _synthesize(self, llm: Any, question: str, category: str, data: Optional[Dict[str, Any]] = None) -> Tuple[str, Tuple[int, int], Optional[Dict[str, Any]]]:
        try:
            if data and isinstance(data, dict) and data.get("skill_steps"):
                return self._synthesize_text(
                    llm, question, category,
                    self._skill_context(data["skill_steps"]))
            rows = None
            columns: List[str] = []
            frame_rows = 0
            full_cube = None
            if data and isinstance(data, dict) and data.get("rows"):
                rows = data["rows"]
                columns = list(data.get("columns") or [])
                frame_rows = int(data.get("frame_rows") or 0)
                full_cube = data.get("full_cube")
            elif data and isinstance(data, list):
                rows = data
            data_context = self._data_context(
                rows, columns, frame_rows, full_cube,
                diagnostic=(data.get("diagnostic") if isinstance(data, dict) else None))
            return self._synthesize_text(llm, question, category, data_context)
        except Exception as e:  # noqa: BLE001 - LLM is optional
            return "Could not generate an answer: " + str(e), (0, 0), None

    def _skill_context(self, steps: Sequence[Dict[str, Any]]) -> str:
        """One labelled block per skill step.

        The old call site flattened every step's rows into a single nested list,
        so the model got [[{...}], [{...}]] with no idea which query produced
        what, how many rows each really had, or that anything had been cut. A
        skill's steps are usually SEPARATE analyses over the same population --
        segments in one, reasons in another -- and saying so is what lets the
        answer explain that they cannot be cross-cut.
        """
        blocks: List[str] = []
        for i, step in enumerate(steps or [], start=1):
            rows = list(step.get("preview") or [])
            total = int(step.get("row_count") or len(rows))
            name = step.get("step", f"step {i}")
            if rows and len(rows) < total:
                state = (f"showing {len(rows)} of {total} rows -- the rest are NOT "
                         f"shown, so do not total or take shares over them")
            elif rows:
                state = f"COMPLETE, all {total} row(s)"
            else:
                state = "returned no rows"
            blocks.append(f"\n--- Result {i} ({name}) -- {state} ---\n{rows}")
        if len(blocks) > 1:
            blocks.append(
                "\nThese results are separate queries. They may share a population "
                "but they are NOT joined, so a figure from one cannot be broken "
                "down by a dimension that only appears in another -- say so plainly "
                "rather than implying a cross-cut you cannot make.")
        return "".join(blocks)

    def _synthesize_text(self, llm: Any, question: str, category: str,
                         data_context: str) -> Tuple[str, Tuple[int, int], Optional[Dict[str, Any]]]:
        try:
            sys_prompt = self.ANSWER_SYSTEM_PROMPT
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
                conversation_id: str = "",
                extract_meta: Optional[Dict[str, Any]] = None,
                analysis: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
            "produced_df_label,conversation_id,extract_meta,analysis) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (answer_id, tenant_id, question, user_id, category, answer, mode.value, status,
             trace, now_iso(), dump_json(source_ids), dump_json(citations or []),
             dump_json(facts or []), dump_json(caveats or []), freshness,
             tokens_in, tokens_out, cost, int(escalated), dump_json(queries_run or []),
             dump_json(python_cells or []), produced_df_label, conversation_id,
             dump_json(extract_meta or {}), dump_json(analysis or {})))
        return {"answer_id": answer_id, "tenant_id": tenant_id, "question": question,
                "category": category, "answer": answer, "answer_mode": mode.value,
                "status": status, "escalated": escalated, "citations": citations or [],
                "caveats": caveats or [], "facts": facts or [], "freshness": freshness,
                "cost": cost, "trace_id": trace, "queries_run": queries_run or [],
                "python_cells": python_cells or [], "produced_df_label": produced_df_label,
                "conversation_id": conversation_id,
                "extract_meta": extract_meta or {}, "analysis": analysis or {}}

    # -- reconciliation --------------------------------------------------------
    # This is where the design stops being a claim and becomes a check.
    # Everything upstream exists so that two answers CAN be compared; nothing
    # until now actually compares them.

    def extract_frame(self, tenant_id: str, conversation_id: str,
                      label: str) -> Optional[Any]:
        """The materialised extract behind one answer, memory or disk.

        Ids are validated HERE rather than being left to whatever the cache does
        with a bad one: a malformed label is a bad request, and it must never
        become a 404 (which reads as "no such extract") or reach the filesystem.
        """
        for part in (tenant_id, conversation_id, label):
            if not SAFE_ID.match(part or ""):
                raise ValueError(f"unsafe identifier: {part!r}")
        return self.data_cache.get(tenant_id, conversation_id, label)

    def _answer_row(self, tenant_id: str, conversation_id: str,
                    answer_id: str) -> Optional[Dict[str, Any]]:
        return self.stores.for_tenant(tenant_id).query_one(
            "SELECT * FROM stakeholder_answers WHERE id=? AND tenant_id=? "
            "AND conversation_id=?", (answer_id, tenant_id, conversation_id))

    def reconcile_answers(self, tenant_id: str, conversation_id: str,
                          answer_a: str, answer_b: str,
                          measure: str = "") -> Optional[ReconcileResult]:
        """Do these two answers rest on the same rows, and do their numbers agree?

        Not "two totals, subtract". Two answers legitimately differ when their
        slices differ, so the comparison is made over the INTERSECTION of the two
        slices -- an answer filtered to Germany and an unfiltered one agree about
        Germany, and that is the only thing they can be asked to agree about.

        Returns None when either answer id is unknown (the caller 404s).
        """
        row_a = self._answer_row(tenant_id, conversation_id, answer_a)
        row_b = self._answer_row(tenant_id, conversation_id, answer_b)
        if row_a is None or row_b is None:
            return None
        meta_a = self._answer_population(tenant_id, conversation_id, row_a)
        meta_b = self._answer_population(tenant_id, conversation_id, row_b)
        hash_a = str(meta_a.get("population_hash") or "")
        hash_b = str(meta_b.get("population_hash") or "")
        measure = measure or self._only_measure(meta_a, meta_b)

        if not hash_a or not hash_b or hash_a != hash_b:
            # Refuse WITHOUT computing. Producing two numbers here would invite
            # someone to read their difference as meaningful, which is the exact
            # mistake this whole design exists to prevent.
            return reconcile(hash_a, 0.0, hash_b, 0.0, measure)

        slice_filters, problem = self._intersect_slices(meta_a, meta_b)
        if problem:
            return ReconcileResult(
                same_population=True, population_hash_a=hash_a, population_hash_b=hash_b,
                measure=measure, agrees=False, explanation=problem)

        values: List[float] = []
        for meta in (meta_a, meta_b):
            value, problem = self._measure_over_slice(
                tenant_id, conversation_id, meta, measure, slice_filters)
            if problem:
                return ReconcileResult(
                    same_population=True, population_hash_a=hash_a,
                    population_hash_b=hash_b, measure=measure, agrees=False,
                    explanation=problem)
            values.append(value)

        result = reconcile(hash_a, values[0], hash_b, values[1], measure)
        result.explanation = self._explain(result, meta_a, meta_b, slice_filters)
        return result

    def _answer_population(self, tenant_id: str, conversation_id: str,
                           row: Any) -> Dict[str, Any]:
        """What population an answer rests on, and which extract to read it from.

        A REUSE turn fetched nothing, so it has no extract_meta of its own -- but
        it absolutely has a population, and refusing to reconcile it would make
        the endpoint useless for exactly the turns this design exists to
        produce. Its slice comes from its own analysis; the frame it was computed
        over comes from the extract it reused.
        """
        meta = load_json(row["extract_meta"], {}) or {}
        if meta.get("population_hash") and meta.get("label"):
            return meta
        analysis = load_json(row["analysis"], {}) or {}
        labels = list(analysis.get("datasets_used") or [])
        label = labels[0] if labels else (row["produced_df_label"] or "")
        stored = self.extract_store.meta(tenant_id, conversation_id, label) if label else None
        base = asdict(stored) if stored is not None else {}
        base.update({
            "label": label,
            # What the FRAME is physically restricted to, which for a reuse turn
            # is not the same as the answer's own slice: the answer asked about
            # Germany, the cube on disk still holds every country.
            "frame_filters": dict(base.get("filters") or {}),
            "population_hash": analysis.get("population_hash") or base.get("population_hash", ""),
            "base_view": analysis.get("base_view") or base.get("base_view", ""),
            # The SLICE is this answer's own; the frame underneath may be wider.
            "filters": dict(analysis.get("slice_filters") or {}),
            "non_additive": list(analysis.get("non_additive")
                                 or base.get("non_additive") or []),
        })
        return base

    @staticmethod
    def _only_measure(meta_a: Dict[str, Any], meta_b: Dict[str, Any]) -> str:
        """When the caller named no measure, use the one both cubes carry."""
        dims_a = set(meta_a.get("dimensions") or [])
        dims_b = set(meta_b.get("dimensions") or [])
        shared = [c for c in (meta_a.get("columns") or [])
                  if c not in dims_a and c not in dims_b
                  and c in (meta_b.get("columns") or [])]
        return shared[0] if shared else ""

    @staticmethod
    def _intersect_slices(meta_a: Dict[str, Any],
                          meta_b: Dict[str, Any]) -> Tuple[Dict[str, List[str]], str]:
        """The narrowest slice both cubes can express, or why they cannot.

        A filter column that is neither already baked into a cube's own slice nor
        a dimension it carries makes the intersection inexpressible on that side.
        Comparing at a wider slice instead would report a disagreement that is an
        artifact of the question rather than of the data.
        """
        filters_a = {k: list(v) for k, v in (meta_a.get("filters") or {}).items()}
        filters_b = {k: list(v) for k, v in (meta_b.get("filters") or {}).items()}
        out: Dict[str, List[str]] = {}
        for column in sorted(set(filters_a) | set(filters_b)):
            if column in filters_a and column in filters_b:
                values = [v for v in filters_a[column] if v in set(filters_b[column])]
                if not values:
                    return {}, (f"cannot compare at this slice: the two answers filter "
                                f"`{column}` to values with nothing in common "
                                f"({filters_a[column]} and {filters_b[column]}), so there "
                                f"is no shared subset to compare.")
            else:
                values = list(filters_a.get(column) or filters_b.get(column) or [])
            for meta in (meta_a, meta_b):
                own = meta.get("filters") or {}
                if column in own or column in (meta.get("dimensions") or []):
                    continue
                return {}, (f"cannot compare at this slice: {meta.get('label')} does not "
                            f"carry `{column}`, so the {', '.join(values)} subset cannot "
                            f"be isolated from it.")
            out[column] = values
        return out, ""

    def _measure_over_slice(self, tenant_id: str, conversation_id: str,
                            meta: Dict[str, Any], measure: str,
                            slice_filters: Dict[str, List[str]]
                            ) -> Tuple[float, str]:
        """One `SELECT SUM(...) FROM <label> WHERE ...` through the workspace.

        Through AnalyticalWorkspace.query, not straight at the Parquet: that is
        where QueryPolicy and the result cap live, and no LLM comes near this.
        """
        label = str(meta.get("label") or "")
        columns = list(meta.get("columns") or [])
        if measure in (meta.get("non_additive") or []):
            return 0.0, (f"`{measure}` is non-additive in {label}, so it cannot be "
                         f"rolled up to this slice. Comparing it here would produce a "
                         f"number that looks right and is not.")
        if measure in columns:
            expr = f"SUM({_escape_ident(measure)})"
        elif f"{measure}_sum" in columns and f"{measure}_count" in columns:
            # An averaged measure is stored decomposed; read it by dividing.
            expr = (f"SUM({_escape_ident(measure + '_sum')}) / "
                    f"NULLIF(SUM({_escape_ident(measure + '_count')}), 0)")
        else:
            return 0.0, (f"{label} does not carry a measure called `{measure}` "
                         f"(it has {columns}).")

        # Only a filter the FRAME is physically restricted to may be skipped. A
        # reuse turn's own slice is a logical claim about the answer, not about
        # the rows on disk -- skipping those would silently compare a Germany
        # figure against a worldwide one.
        baked = meta.get("frame_filters")
        if baked is None:
            baked = meta.get("filters") or {}
        where = []
        for column, values in sorted(slice_filters.items()):
            if column in baked or not values:
                continue
            literals = ", ".join(_sql_literal(v) for v in values)
            where.append(f"{_escape_ident(column)} IN ({literals})")
        sql = f"SELECT {expr} AS _v FROM {_escape_ident(label)}"
        if where:
            sql += " WHERE " + " AND ".join(where)

        self.workspace.register(tenant_id, conversation_id, label)
        res = self.workspace.query(tenant_id, conversation_id, sql)
        if not res.ok or res.data is None or not len(res.data):
            return 0.0, (f"could not compute `{measure}` over {label}: "
                         f"{res.error or 'no rows'}")
        value = res.data.iloc[0, 0]
        return (0.0 if value is None else float(value)), ""

    @staticmethod
    def _explain(result: ReconcileResult, meta_a: Dict[str, Any],
                 meta_b: Dict[str, Any], slice_filters: Dict[str, List[str]]) -> str:
        """Written for a human -- this is the field the UI shows."""
        where = ("; ".join(f"{c} in {', '.join(v)}" for c, v in sorted(slice_filters.items()))
                 or "the whole population")
        label_a, label_b = meta_a.get("label", "A"), meta_b.get("label", "B")
        verdict = "they agree" if result.agrees else "they DISAGREE"
        text = (f"Both answers were computed over "
                f"{meta_a.get('base_view') or 'the same base view'} (population "
                f"{result.population_hash_a[:8]}…); over {where}, {result.measure} is "
                f"{result.value_a:,.2f} from {label_a} and {result.value_b:,.2f} from "
                f"{label_b} — {verdict}.")
        if result.agrees:
            return text
        # A disagreement over one population is a real finding, and it usually
        # has one of two causes. Name them rather than leaving the reader to
        # guess which artifact to distrust.
        causes = []
        for meta in (meta_a, meta_b):
            if meta.get("truncated"):
                causes.append(f"{meta.get('label')} is truncated at "
                              f"{meta.get('row_count')} rows, so its total is understated")
            for name in (meta.get("non_additive") or []):
                causes.append(f"{meta.get('label')} carries the non-additive measure "
                              f"`{name}`, which cannot be rolled up")
        if causes:
            text += " Likely cause: " + "; ".join(causes) + "."
        return text

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