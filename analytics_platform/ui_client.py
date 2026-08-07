"""Thin, dependency-light HTTP client for the standalone platform API.

Used by the Streamlit UI (and anything else) as the *only* way it talks to the
platform — per the plan's "Streamlit as a thin API client" (React/Next later,
§5). Read-only typed helpers; mutations (approve/bulk) are explicit.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests


class APIClient:
    def __init__(self, base_url: str = "http://localhost:8000", timeout: float = 15.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _req(self, method: str, path: str, **kw: Any) -> Any:
        r = requests.request(method, f"{self.base}{path}", timeout=self.timeout, **kw)
        r.raise_for_status()
        return r.json()

    # -- tenants -----------------------------------------------------------
    def list_tenants(self) -> List[Dict[str, Any]]:
        return self._req("GET", "/tenants")

    def create_tenant(self, name: str) -> Dict[str, Any]:
        return self._req("POST", "/tenants", json={"name": name})

    def get_tenant(self, tenant: str) -> Dict[str, Any]:
        """Tenant row + its company profile (the analysts' business context)."""
        return self._req("GET", f"/tenants/{tenant}")

    def set_profile(self, tenant: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        """Persist company/business context (description, OKRs/targets, metrics)."""
        return self._req("PUT", f"/tenants/{tenant}/company-profile", json=profile)

    def profile_history(self, tenant: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Versioned history of business-context changes (config panel)."""
        return self._req("GET", f"/tenants/{tenant}/company-profile/history",
                         params={"limit": limit})

    def list_datasources(self, tenant: str) -> List[Dict[str, Any]]:
        return self._req("GET", f"/tenants/{tenant}/datasources")

    def add_datasource(self, tenant: str, name: str, kind: str = "direct_db",
                       dialect: str = "athena", tables: Optional[List[str]] = None,
                       connected: bool = True) -> Dict[str, Any]:
        return self._req("POST", f"/tenants/{tenant}/datasources",
                         json={"name": name, "kind": kind, "dialect": dialect,
                               "tables": tables or [], "connected": connected})

    # -- junior (read-only) ------------------------------------------------
    def junior_stage(self, tenant: str, limit: int = 200) -> Dict[str, Any]:
        return self._req("GET", f"/junior/{tenant}/stage", params={"limit": limit})

    def junior_catalog(self, tenant: str) -> Dict[str, Any]:
        return self._req("GET", f"/junior/{tenant}/catalog")

    def junior_questions(self, tenant: str) -> Dict[str, Any]:
        return self._req("GET", f"/junior/{tenant}/questions")

    def junior_datasets(self, tenant: str) -> List[str]:
        return self._req("GET", f"/junior/{tenant}/datasets")

    # -- triage ------------------------------------------------------------
    def triage_summary(self, tenant: str) -> Dict[str, Any]:
        return self._req("GET", f"/triage/{tenant}/summary")

    def triage_queue(self, tenant: str, kind: str = "", search: str = "",
                     limit: int = 100) -> List[Dict[str, Any]]:
        return self._req("GET", f"/triage/{tenant}/queue",
                         params={"kind": kind, "search": search, "limit": limit})

    def triage_conflicts(self, tenant: str, limit: int = 100) -> List[Dict[str, Any]]:
        return self._req("GET", f"/triage/{tenant}/conflicts", params={"limit": limit})

    def triage_approve(self, tenant: str, ids: List[str], by: str = "senior",
                       notes: str = "") -> Dict[str, Any]:
        return self._req("POST", f"/triage/{tenant}/approve",
                         json={"ids": ids, "by": by, "notes": notes})

    def triage_reject(self, tenant: str, ids: List[str], by: str = "senior",
                      notes: str = "") -> Dict[str, Any]:
        return self._req("POST", f"/triage/{tenant}/reject",
                         json={"ids": ids, "by": by, "notes": notes})

    def triage_bulk(self, tenant: str, kind: str = "", action: str = "approve",
                    by: str = "senior", limit: int = 200,
                    notes: str = "") -> Dict[str, Any]:
        return self._req("POST", f"/triage/{tenant}/bulk",
                         json={"kind": kind or None, "action": action, "by": by,
                               "limit": limit, "notes": notes})

    def triage_dedupe(self, tenant: str, keep: str, drop: List[str],
                      by: str = "senior", notes: str = "") -> Dict[str, Any]:
        """Keep one conflict node; reject/supersede the rest of the group."""
        return self._req("POST", f"/triage/{tenant}/dedupe",
                         json={"keep": keep, "drop": drop, "by": by, "notes": notes})

    # -- P6 stakeholder ----------------------------------------------------
    def stakeholder_answer(self, tenant: str, question: str,
                           user_id: str = "") -> Dict[str, Any]:
        return self._req("POST", f"/stakeholder/{tenant}/answer",
                         json={"question": question, "user_id": user_id})

    def stakeholder_feedback(self, tenant: str, answer_id: str, rating: str = "up",
                             user_id: str = "", comment: str = "") -> Dict[str, Any]:
        return self._req("POST", f"/stakeholder/{tenant}/feedback",
                         json={"answer_id": answer_id, "rating": rating,
                               "user_id": user_id, "comment": comment})

    def stakeholder_quality(self, tenant: str) -> Dict[str, Any]:
        return self._req("GET", f"/stakeholder/{tenant}/quality")

    # -- P7 research -------------------------------------------------------
    def research_seed(self, tenant: str) -> List[Dict[str, Any]]:
        return self._req("POST", f"/research/{tenant}/sources/seed")

    def research_sources(self, tenant: str) -> List[Dict[str, Any]]:
        return self._req("GET", f"/research/{tenant}/sources")

    def research_search(self, tenant: str, query: str,
                        results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return self._req("POST", f"/research/{tenant}/search",
                         json={"query": query, "results": results})

    def research_capture(self, tenant: str, query: str,
                         results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return self._req("POST", f"/research/{tenant}/capture",
                         json={"query": query, "results": results})

    def research_docs(self, tenant: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self._req("GET", f"/research/{tenant}/docs", params={"limit": limit})

    def research_promote(self, tenant: str, doc_id: str) -> Dict[str, Any]:
        return self._req("POST", f"/research/{tenant}/promote", json={"doc_id": doc_id})

    def research_overview(self, tenant: str) -> Dict[str, Any]:
        return self._req("GET", f"/research/{tenant}/overview")

    # -- P8 governance -----------------------------------------------------
    def billing_usage(self, tenant: str) -> Dict[str, Any]:
        return self._req("GET", f"/billing/{tenant}/usage")

    # -- Phase 9 observability (owner-facing) -----------------------------
    def observability_status(self) -> Dict[str, Any]:
        return self._req("GET", "/observability/status")

    def observability_logs(self, tenant: str = "", limit: int = 200) -> Dict[str, Any]:
        return self._req("GET", "/observability/logs",
                         params={"tenant_id": tenant, "limit": limit})

    def observability_purge(self) -> Dict[str, Any]:
        return self._req("POST", "/observability/purge")

    def observability_junior_run(self, tenant: str = "") -> Dict[str, Any]:
        return self._req("POST", "/observability/junior/run",
                         params={"tenant_id": tenant})


__all__ = ["APIClient"]