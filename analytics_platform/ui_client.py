"""Thin, dependency-light HTTP client for the standalone platform API.

Used by the Streamlit UI (and anything else) as the *only* way it talks to the
platform — per the plan's "Streamlit as a thin API client" (React/Next later,
§5). Read-only typed helpers; mutations (approve/bulk) are explicit.
"""
from __future__ import annotations

from typing import Any, Dict, List

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

    def triage_approve(self, tenant: str, ids: List[str], by: str = "senior") -> Dict[str, Any]:
        return self._req("POST", f"/triage/{tenant}/approve", json={"ids": ids, "by": by})

    def triage_reject(self, tenant: str, ids: List[str], by: str = "senior") -> Dict[str, Any]:
        return self._req("POST", f"/triage/{tenant}/reject", json={"ids": ids, "by": by})

    def triage_bulk(self, tenant: str, kind: str = "", action: str = "approve",
                    by: str = "senior", limit: int = 200) -> Dict[str, Any]:
        return self._req("POST", f"/triage/{tenant}/bulk",
                         json={"kind": kind or None, "action": action, "by": by, "limit": limit})


__all__ = ["APIClient"]