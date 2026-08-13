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

from typing import Any, Dict, List, Optional, Tuple

from .brain.store import CompanyBrain
from .config import Settings
from .database import Store, dump_json
from .domain import AnswerMode, NodeKind, new_id, now_iso
from .execution.base import ExecutionContext
from .llm.client import make_role_client
from .observability import Observability, new_trace
from .tenancy import TenantService
from .skills import SkillRegistry, SkillEngine

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
    def __init__(self, store: Store, tenants: Optional[TenantService] = None,
                 executor: Optional[Any] = None,
                 observability: Optional[Observability] = None,
                 cost_per_1k_input: float = 0.30,
                 cost_per_1k_output: float = 1.20,
                 settings: Optional[Settings] = None):
        from .execution.sampler import SamplerExecutor
        self.store = store
        self.tenants = tenants or TenantService(store)
        self.executor = executor or SamplerExecutor()
        self.obs = observability or Observability(store)
        self.settings = settings or Settings()
        self.cost_per_1k_input = cost_per_1k_input
        self.cost_per_1k_output = cost_per_1k_output
        self.skill_registry = SkillRegistry()
        self.skill_registry.load_skills()
        self.skill_engine = SkillEngine()

    def brain(self, tenant_id: str) -> CompanyBrain:
        return CompanyBrain(self.store, tenant_id)

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

    # -- retrieve ----------------------------------------------------------
    def _retrieve(self, tenant_id: str, question: str) -> Tuple[List[Any], List[Any]]:
        """Approved knowledge first: reusable QUERY nodes, else DEFINITION nodes."""
        brain = self.brain(tenant_id)
        q = brain.search(question, kind=NodeKind.QUERY, usable_only=True, limit=3)
        d = brain.search(question, kind=NodeKind.DEFINITION, usable_only=True, limit=3)
        return (q or []), (d or [])

    def _refresh(self, tenant_id: str, node: Any, question: str) -> Dict[str, Any]:
        ec = ExecutionContext(tenant_id=tenant_id, question=question,
                              dialect=node.payload.get("dialect", "athena"))
        result = self.executor.execute(node.payload.get("sql", ""), ec)
        if not result.ok:
            return {"ok": False, "error": result.error, "row_count": 0,
                    "execution_ms": result.execution_ms}
        preview = []
        rows = result.data
        if rows is not None:
            try:
                preview = rows.head(3).to_dict(orient="records")
            except Exception:  # noqa: BLE001 - non-DataFrame result
                preview = list(rows)[:3]
        return {"ok": True, "row_count": result.row_count,
                "execution_ms": result.execution_ms, "preview": preview}

    # -- answer ------------------------------------------------------------
    def answer(self, tenant_id: str, question: str, user_id: str = "") -> Dict[str, Any]:
        self.tenants.require_tenant(tenant_id)
        trace = new_trace()
        category = self.classify(question)

        cfg = self.tenants.get_analyst_config(tenant_id)
        if not cfg.stakeholder.enabled:
            answer = "AI Stakeholder analyst is disabled for this tenant."
            out = self._record(tenant_id, question, user_id, category, trace, answer,
                               AnswerMode.CANNOT_ANSWER, "CANNOT_ANSWER", False, [],
                               caveats=["stakeholder analyst AI disabled in tenant configuration"])
            self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.answer",
                           actor="stakeholder", resource=out["answer_id"], status="DISABLED",
                           meta={"category": category, "mode": AnswerMode.CANNOT_ANSWER.value})
            return out

        llm = make_role_client(self.settings, cfg.stakeholder)
        query_nodes, defn_nodes = self._retrieve(tenant_id, question)

        if self.is_high_risk(question, category):
            source_ids = [n.id for n in (query_nodes + defn_nodes)]
            if source_ids:
                self.brain(tenant_id).submit(source_ids[0], by="stakeholder")
            out = self._record(tenant_id, question, user_id, category, trace, "",
                               AnswerMode.REQUIRES_SENIOR_REVIEW, "ESCALATED", True,
                               source_ids, caveats=["high-risk question matched escalation rules"],
                               queries_run=[n.payload.get("sql", "") for n in query_nodes])
            self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.escalate",
                           actor="stakeholder", resource=out["answer_id"], status="OK",
                           meta={"category": category})
            return out

        if query_nodes:
            all_details = []
            queries_run = []
            citations = []
            facts = []
            caveats = ["values from approved queries at review time"]
            any_failed = False
            last_err = ""
            for q_node in query_nodes:
                sql = q_node.payload.get("sql", "")
                if sql:
                    queries_run.append(sql)
                refreshed = self._refresh(tenant_id, q_node, question)
                all_details.append(refreshed)
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
            else:
                answer = f"Matched {len(query_nodes)} approved queries, but execution failed: {last_err}"
                mode = AnswerMode.CANNOT_ANSWER
                caveats = [str(last_err)]
                
            chart_config = None
            t_in, t_out = 0, 0
            if not any_failed and self._llm_live(llm) and len(all_details) > 0:
                data_arg = all_details[0].get("preview", [])
                _, (t_in, t_out), chart_config = self._synthesize(llm, question, category, data_arg)

            out = self._record(tenant_id, question, user_id, category, trace, answer, mode,
                               "ANSWERED", False, [n.id for n in query_nodes],
                               citations, facts=facts, caveats=caveats,
                               tokens_in=t_in, tokens_out=t_out, queries_run=queries_run)
            out["_detail"] = all_details
            out["chart_config"] = chart_config
            out["chart_data"] = all_details[0].get("preview", []) if all_details else []
            self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.answer",
                           actor="stakeholder", resource=out["answer_id"], status="OK",
                           meta={"category": category, "mode": mode.value})
            return out
        if defn_nodes:
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
                               caveats=["from approved definitions at review time"])
            self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.answer",
                           actor="stakeholder", resource=out["answer_id"], status="OK",
                           meta={"category": category, "mode": AnswerMode.DIRECT_FROM_APPROVED_KNOWLEDGE.value})
            return out

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
                                           caveats=["missing required parameters for skill: " + skill.meta.name])
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
                                           caveats=["skill execution error"])
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
                                       tokens_in=toks[0], tokens_out=toks[1])
                    out["chart_config"] = chart_config
                    out["chart_data"] = exec_res.data_previews[-1]["preview"] if exec_res.data_previews else []
                    self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.answer",
                                   actor="stakeholder", resource=out["answer_id"], status="OK",
                                   meta={"category": category, "mode": out["answer_mode"]})
                    return out

            # Fallback to direct LLM synthesis if no skill matched
            answer, toks, chart_config = self._synthesize(llm, question, category)
            out = self._record(tenant_id, question, user_id, category, trace, answer,
                               AnswerMode.NEW_LOW_RISK_ANALYSIS, "ANSWERED", False, [],
                               caveats=["no approved knowledge in the Brain; generated answer"],
                               tokens_in=toks[0], tokens_out=toks[1])
            out["chart_config"] = chart_config
            out["chart_data"] = []
        else:
            answer = ("I don't have an approved query or definition matching this question yet. "
                      "Rephrase, or ask the senior analyst.")
            out = self._record(tenant_id, question, user_id, category, trace, answer,
                               AnswerMode.CANNOT_ANSWER, "CANNOT_ANSWER", False, [],
                               caveats=["no approved knowledge matched"])
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
                queries_run: Optional[List[str]] = None) -> Dict[str, Any]:
        answer_id = new_id("ans")
        cost = round((tokens_in / 1000.0) * self.cost_per_1k_input
                     + (tokens_out / 1000.0) * self.cost_per_1k_output, 6)
        freshness = 0.0
        for c in citations or []:
            freshness = max(freshness, float(c.get("freshness", 0.0)))
        self.store.execute(
            "INSERT INTO stakeholder_answers (id,tenant_id,question,user_id,category,answer,"
            "answer_mode,status,trace_id,created_at,source_node_ids,citations,facts,caveats,"
            "freshness,tokens_in,tokens_out,cost,escalated,queries_run) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (answer_id, tenant_id, question, user_id, category, answer, mode.value, status,
             trace, now_iso(), dump_json(source_ids), dump_json(citations or []),
             dump_json(facts or []), dump_json(caveats or []), freshness,
             tokens_in, tokens_out, cost, int(escalated), dump_json(queries_run or [])))
        return {"answer_id": answer_id, "tenant_id": tenant_id, "question": question,
                "category": category, "answer": answer, "answer_mode": mode.value,
                "status": status, "escalated": escalated, "citations": citations or [],
                "caveats": caveats or [], "facts": facts or [], "freshness": freshness,
                "cost": cost, "trace_id": trace, "queries_run": queries_run or []}

    # -- feedback + quality -------------------------------------------------
    def record_feedback(self, tenant_id: str, answer_id: str, user_id: str,
                        rating: str, comment: str = "") -> Dict[str, Any]:
        row = self.store.query_one(
            "SELECT * FROM stakeholder_answers WHERE id=? AND tenant_id=?", (answer_id, tenant_id))
        if not row:
            return {"error": "answer not found"}
        fid = new_id("fb")
        self.store.execute(
            "INSERT INTO stakeholder_feedback (id,tenant_id,answer_id,user_id,rating,comment,"
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (fid, tenant_id, answer_id, user_id, rating, comment, now_iso()))
        self.obs.event(tenant_id=tenant_id, stage="stakeholder.feedback", actor=user_id or "unknown",
                       resource=answer_id, meta={"rating": rating})
        return {"feedback_id": fid, "answer_id": answer_id, "rating": rating}

    def quality(self, tenant_id: str) -> Dict[str, Any]:
        answers = self.store.query_all(
            "SELECT * FROM stakeholder_answers WHERE tenant_id=?", (tenant_id,))
        feedbacks = self.store.query_all(
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