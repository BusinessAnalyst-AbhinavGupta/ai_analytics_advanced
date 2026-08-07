"""Senior-analyst service: the human plays the senior role through the tool when
the senior analyst's AI is turned off (and as an override when it is on).

Design (config panel):
- Each analyst (junior / senior / stakeholder) has a toggle + model, stored per
  tenant (`analyst_configs`), versioned in `analyst_config_history`.
- A "senior" approves / rejects / promotes analyses produced by the junior.
  When `senior.enabled` is False the workload falls to a human, who performs the
  senior role through the exact same `SeniorService` + API surface — there is no
  separate AI-only path, so the human and the AI are interchangeable (human-on-top).

Invariants: read-only policy is preserved (only status transitions, never DML);
every action emits an observability event; no credentials logged.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import ReviewStatus, RunStatus
from .observability import Observability


class SeniorService:
    def __init__(self, store, pipeline, tenants, observability=None):
        self.store = store
        self.pipeline = pipeline
        self.tenants = tenants
        self.obs = observability or Observability(store)

    # -- who is playing senior -----------------------------------------------
    def status(self, tenant_id: str) -> Dict[str, Any]:
        """Where the senior workload currently sits (AI vs human, from config)."""
        cfg = self.tenants.get_analyst_config(tenant_id)
        s = cfg.senior
        return {
            "tenant_id": tenant_id,
            "role": "senior",
            "mode": "ai" if s.enabled else "human",
            "enabled": s.enabled,
            "provider": s.provider,
            "model": s.model,
            "human_override": True,  # human-on-top always available
        }

    # -- senior review inbox -------------------------------------------------
    def queue(self, tenant_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Analyses awaiting senior sign-off (completed runs not yet approved)."""
        cfg = self.tenants.get_analyst_config(tenant_id)
        rows = self.store.query_all(
            "SELECT * FROM analysis_runs WHERE tenant_id=? "
            "AND status IN (?,?) ORDER BY generated_at DESC LIMIT ?",
            (tenant_id, RunStatus.COMPLETED.value, RunStatus.EXECUTED.value, limit))
        out = []
        for r in self.store.rows_to_dicts(rows):
            rv = r.get("review_status") or ReviewStatus.CANDIDATE.value
            if rv in (ReviewStatus.REJECTED.value, ReviewStatus.SUPERSEDED.value,
                      ReviewStatus.ARCHIVED.value):
                continue
            out.append({
                "run_id": r["id"], "question": r.get("question_text", ""),
                "sql": r.get("sql", ""), "status": r.get("status"),
                "review_status": rv, "generated_at": r.get("generated_at", ""),
                "row_count": r.get("row_count", 0),
                "answer": r.get("answer", ""),
                "reviewer": "human" if not cfg.senior.enabled else "ai",
                "mode": "human" if not cfg.senior.enabled else "ai",
            })
        return out

    def _set_review_status(self, run_id: str, tenant_id: str, status: str) -> None:
        self.store.execute(
            "UPDATE analysis_runs SET review_status=? WHERE id=? AND tenant_id=?",
            (status, run_id, tenant_id))

    def review(self, tenant_id: str, run_id: str, action: str = "approve",
               by: str = "human", notes: str = "") -> Dict[str, Any]:
        """Senior decision on a junior's analysis. 'approve' promotes to a
        governed FINDING; 'reject' marks the run rejected; 'revise' requests it
        back. Works identically for human or AI reviewer (human-on-top)."""
        run = self.pipeline.get_run(tenant_id, run_id)
        if run is None:
            return {"ok": False, "error": "run not found"}
        if run.status != RunStatus.COMPLETED:
            return {"ok": False, "error": "run not completed / not reviewable"}

        action = (action or "approve").lower()
        if action == "approve":
            # promote to a governed FINDING (no-op if run not completable)
            node = self.pipeline.promote_finding(tenant_id, run_id, by=by, notes=notes)
            self._set_review_status(run_id, tenant_id, ReviewStatus.APPROVED.value)
            result = {"ok": True, "action": "approved", "node_id": node.id if node else None}
        elif action == "reject":
            self._set_review_status(run_id, tenant_id, ReviewStatus.REJECTED.value)
            result = {"ok": True, "action": "rejected", "node_id": None}
        elif action == "revise":
            self._set_review_status(run_id, tenant_id, ReviewStatus.REVISION_REQUIRED.value)
            result = {"ok": True, "action": "revised", "node_id": None}
        else:
            return {"ok": False, "error": f"unknown action {action}"}
        self.obs.event(tenant_id=tenant_id, stage=f"senior.{action}", actor=by,
                       resource=run_id, status="OK",
                       meta={"question": run.question_text[:120]})
        return {**result, "run_id": run_id, "by": by}