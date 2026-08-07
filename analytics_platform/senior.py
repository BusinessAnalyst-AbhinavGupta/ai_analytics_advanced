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

from .domain import ReviewStatus, RunStatus, clamp_junior_depth
from .observability import Observability


class SeniorService:
    def __init__(self, store, pipeline, tenants, observability=None,
                 reviews_dir: str = "data/reviews"):
        self.store = store
        self.pipeline = pipeline
        self.tenants = tenants
        self.obs = observability or Observability(store)
        self.reviews_dir = reviews_dir

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
            "junior_depth": clamp_junior_depth(cfg.junior_depth),
            "junior_depth_label": cfg.depth_label,
            "human_signoff_days": int(cfg.human_signoff_days),
        }

    # -- junior depth (human controls the quality/depth the junior learns) ------
    def junior_depth(self, tenant_id: str) -> Dict[str, Any]:
        cfg = self.tenants.get_analyst_config(tenant_id)
        return {"tenant_id": tenant_id, "depth": clamp_junior_depth(cfg.junior_depth),
                "label": cfg.depth_label}

    def set_junior_depth(self, tenant_id: str, action: str = "up",
                         level: Optional[int] = None, by: str = "human") -> Dict[str, Any]:
        """Human promote/downgrade of the junior question depth (0..2)."""
        cfg = self.tenants.get_analyst_config(tenant_id)
        cur = clamp_junior_depth(cfg.junior_depth)
        if action == "set" and level is not None:
            nxt = clamp_junior_depth(level)
        elif action == "up":
            nxt = min(2, cur + 1)
        elif action == "down":
            nxt = max(0, cur - 1)
        else:
            return {"ok": False, "error": f"unknown action {action}"}
        cfg = self.tenants.set_analyst_config(tenant_id, {"junior_depth": nxt}, changed_by=by)
        self.obs.event(tenant_id=tenant_id, stage="senior.junior_depth", actor=by,
                       status="OK", meta={"from": cur, "to": nxt, "action": action})
        return {"ok": True, "tenant_id": tenant_id, "depth": nxt,
                "label": cfg.depth_label, "action": action, "by": by}

    # -- human-signoff mandate ------------------------------------------------
    def _tenant_age_days(self, tenant_id: str) -> Optional[float]:
        try:
            t = self.tenants.get_tenant(tenant_id)
            created = (t or {}).get("created_at") if isinstance(t, dict) else getattr(t, "created_at", "")
            if not created:
                return None
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
        except Exception:  # noqa: BLE001 - best-effort
            return None

    def requires_human_signoff(self, tenant_id: str, run_id: Optional[str] = None) -> bool:
        """True when an analysis *must* go through a human, even with the senior AI on.

        First `human_signoff_days` (default 7) after the tenant is created, every
        junior analysis requires an explicit human evaluation (initial humanoversight
        window). Also gated on junior depth == 0 (still learning).
        """
        cfg = self.tenants.get_analyst_config(tenant_id)
        days = int(getattr(cfg, "human_signoff_days", 7))
        if days > 0:
            age = self._tenant_age_days(tenant_id)
            if age is None or age < days:
                return True
        return clamp_junior_depth(cfg.junior_depth) == 0

    # -- markdown rendering (each analysis is an .md for human review) ---------
    def analysis_md(self, tenant_id: str, run_id: str) -> Dict[str, Any]:
        """Render an analysis as markdown and persist the .md file (R3)."""
        from .markdown import render_analysis_md, write_analysis_md
        run = self.pipeline.get_run(tenant_id, run_id)
        if run is None:
            return {"ok": False, "error": "run not found"}
        text = render_analysis_md(run)
        path = write_analysis_md(run, self.reviews_dir)
        return {"ok": True, "tenant_id": tenant_id, "run_id": run_id,
                "md": text, "path": path}

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
        back. Works identically for human or AI reviewer (human-on-top), with
        hard gates: an AI senior can't approve when the senior AI is off, and
        can't auto-approve an analysis that requires human sign-off (initial
        human-signoff window / junior still at basic depth)."""
        run = self.pipeline.get_run(tenant_id, run_id)
        if run is None:
            return {"ok": False, "error": "run not found"}
        if run.status != RunStatus.COMPLETED:
            return {"ok": False, "error": "run not completed / not reviewable"}

        by = (by or "human").lower()
        cfg = self.tenants.get_analyst_config(tenant_id)
        if not cfg.senior.enabled and by == "ai":
            return {"ok": False, "action": None, "error": (
                "senior AI is disabled; this review must be done by a human... "
                "human-on-top through the same surface")}
        if by == "ai" and self.requires_human_signoff(tenant_id, run_id):
            return {"ok": False, "action": None, "error": (
                "analysis is in the initial human-signoff window (or junior is at "
                "basic depth); an AI senior cannot auto-approve — a human must review")}

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
        self.analysis_md(tenant_id, run_id)  # persist the .md for human review (R3)
        self.obs.event(tenant_id=tenant_id, stage=f"senior.{action}", actor=by,
                       resource=run_id, status="OK",
                       meta={"question": run.question_text[:120], "by": by})
        return {**result, "run_id": run_id, "by": by}