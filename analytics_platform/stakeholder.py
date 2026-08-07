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
from .database import Store, dump_json
from .domain import AnswerMode, NodeKind, new_id, now_iso
from .execution.base import ExecutionContext
from .observability import Observability, new_trace
from .tenancy import TenantService

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
                 llm: Optional[Any] = None,
                 cost_per_1k_input: float = 0.30,
                 cost_per_1k_output: float = 1.20):
        from .execution.sampler import SamplerExecutor
        from .llm.client import NullClient
        self.store = store
        self.tenants = tenants or TenantService(store)
        self.executor = executor or SamplerExecutor()
        self.obs = observability or Observability(store)
        self.llm = llm or NullClient()
        self.cost_per_1k_input = cost_per_1k_input
        self.cost_per_1k_output = cost_per_1k_output

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
    def _retrieve(self, tenant_id: str, question: str) -> Tuple[Optional[Any], Optional[Any]]:
        """Approved knowledge first: a reusable QUERY, else a DEFINITION."""
        brain = self.brain(tenant_id)
        q = brain.search(question, kind=NodeKind.QUERY, usable_only=True, limit=3)
        d = brain.search(question, kind=NodeKind.DEFINITION, usable_only=True, limit=3)
        return (q[0] if q else None), (d[0] if d else None)

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
        query_node, defn_node = self._retrieve(tenant_id, question)

        if self.is_high_risk(question, category):
            source_ids = [n.id for n in (query_node, defn_node) if n]
            if source_ids:
                self.brain(tenant_id).submit(source_ids[0], by="stakeholder")
            out = self._record(tenant_id, question, user_id, category, trace, "",
                               AnswerMode.REQUIRES_SENIOR_REVIEW, "ESCALATED", True,
                               source_ids, caveats=["high-risk question matched escalation rules"])
            self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.escalate",
                           actor="stakeholder", resource=out["answer_id"], status="OK",
                           meta={"category": category})
            return out

        if query_node is not None:
            refreshed = self._refresh(tenant_id, query_node, question)
            if refreshed["ok"]:
                answer = ("Reused approved query '" + query_node.title + "' ("
                          + str(refreshed["row_count"]) + " rows).")
                mode = AnswerMode.REFRESHED_APPROVED_QUERY
                caveats = ["value from the approved query at review time"]
            else:
                answer = ("Matched approved query '" + query_node.title + "' but it failed "
                          "to run: " + str(refreshed["error"]))
                mode = AnswerMode.CANNOT_ANSWER
                caveats = [str(refreshed["error"])]
            freshness = query_node.confidence.get("freshness", 0.0)
            out = self._record(tenant_id, question, user_id, category, trace, answer, mode,
                               "ANSWERED", False, [query_node.id],
                               [{"node_id": query_node.id, "title": query_node.title,
                                 "evidence_ref": query_node.evidence_ref,
                                 "freshness": freshness}],
                               facts=["reused approved query: " + query_node.title] if refreshed["ok"] else [],
                               caveats=caveats)
            out["_detail"] = refreshed
            self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.answer",
                           actor="stakeholder", resource=out["answer_id"], status="OK",
                           meta={"category": category, "mode": mode.value})
            return out

        if defn_node is not None:
            answer = ("Definition: " + defn_node.title + ". " + (defn_node.summary or "")).strip()
            out = self._record(tenant_id, question, user_id, category, trace, answer,
                               AnswerMode.DIRECT_FROM_APPROVED_KNOWLEDGE, "ANSWERED", False,
                               [defn_node.id],
                               [{"node_id": defn_node.id, "title": defn_node.title,
                                 "evidence_ref": defn_node.evidence_ref,
                                 "freshness": defn_node.confidence.get("freshness", 0.0)}],
                               facts=[defn_node.summary] if defn_node.summary else [],
                               caveats=["from an approved definition at review time"])
            self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.answer",
                           actor="stakeholder", resource=out["answer_id"], status="OK",
                           meta={"category": category, "mode": AnswerMode.DIRECT_FROM_APPROVED_KNOWLEDGE.value})
            return out

        if self._llm_live():
            answer, toks = self._synthesize(question, category)
            out = self._record(tenant_id, question, user_id, category, trace, answer,
                               AnswerMode.NEW_LOW_RISK_ANALYSIS, "ANSWERED", False, [],
                               caveats=["no approved knowledge in the Brain; generated answer"],
                               tokens_in=toks[0], tokens_out=toks[1])
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
    def _llm_live(self) -> bool:
        return getattr(self.llm, "name", "null") != "null"

    def _synthesize(self, question: str, category: str) -> Tuple[str, Tuple[int, int]]:
        try:
            res = self.llm.generate(
                prompt="Answer in 2-3 sentences: " + question,
                system_prompt=("You are a cautious internal analytics assistant. State what "
                               "you know and what data you would need. Do not invent figures."),
                temperature=0.2)
            text = (res.text or "").strip()
            if not text:
                return "No answer generated.", (res.tokens_in, res.tokens_out)
            return text, (res.tokens_in, res.tokens_out)
        except Exception as e:  # noqa: BLE001 - LLM is optional
            return "Could not generate an answer: " + str(e), (0, 0)

    def _record(self, tenant_id: str, question: str, user_id: str, category: str,
                trace: str, answer: str, mode: AnswerMode, status: str,
                escalated: bool, source_ids: List[str],
                citations: Optional[List[Dict[str, Any]]] = None,
                facts: Optional[List[str]] = None,
                caveats: Optional[List[str]] = None,
                tokens_in: int = 0, tokens_out: int = 0) -> Dict[str, Any]:
        answer_id = new_id("ans")
        cost = round((tokens_in / 1000.0) * self.cost_per_1k_input
                     + (tokens_out / 1000.0) * self.cost_per_1k_output, 6)
        freshness = 0.0
        for c in citations or []:
            freshness = max(freshness, float(c.get("freshness", 0.0)))
        self.store.execute(
            "INSERT INTO stakeholder_answers (id,tenant_id,question,user_id,category,answer,"
            "answer_mode,status,trace_id,created_at,source_node_ids,citations,facts,caveats,"
            "freshness,tokens_in,tokens_out,cost,escalated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (answer_id, tenant_id, question, user_id, category, answer, mode.value, status,
             trace, now_iso(), dump_json(source_ids), dump_json(citations or []),
             dump_json(facts or []), dump_json(caveats or []), freshness,
             tokens_in, tokens_out, cost, int(escalated)))
        return {"answer_id": answer_id, "tenant_id": tenant_id, "question": question,
                "category": category, "answer": answer, "answer_mode": mode.value,
                "status": status, "escalated": escalated, "citations": citations or [],
                "caveats": caveats or [], "facts": facts or [], "freshness": freshness,
                "cost": cost, "trace_id": trace}

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