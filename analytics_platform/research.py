"""P7 — External research: cited competitor / best-practice claims.

Implements the plan's external-research phase: approved search providers, a
source allow/block list, source-credibility classification, and competitor /
best-practice workflows. Exit criteria it enforces:

- external claims are **cited** (provider + url + credibility) and clearly
  flagged origin="external" (distinguished from internal company facts);
- a search result can never silently become company knowledge: promotion writes
  a `NodeKind.EXTERNAL` node that always starts CANDIDATE and only the senior
  triage path can ever turn it into an approved fact.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .brain.store import CompanyBrain
from .database import Store, dump_json, load_json
from .domain import NodeKind, ReviewStatus, new_id, now_iso
from .observability import Observability
from .stores import TenantStoreProvider

CREDIBILITY_SCORE = {"official": 1.0, "government": 0.95, "industry_analyst": 0.85,
                     "vendor": 0.6, "blog": 0.4, "forum": 0.3}

DEFAULT_SOURCES = [
    {"name": "Official documentation", "url": "https://docs.example.com", "kind": "official",
     "credibility": "official", "policy": "allow"},
    {"name": "Government statistics", "url": "https://gov.example.org", "kind": "government",
     "credibility": "government", "policy": "allow"},
    {"name": "Industry analyst", "url": "https://analyst.example.com", "kind": "industry_analyst",
     "credibility": "industry_analyst", "policy": "allow"},
    {"name": "Vendor blog", "url": "https://blog.example.com", "kind": "vendor",
     "credibility": "vendor", "policy": "allow"},
    {"name": "Community forum", "url": "https://forum.example.com", "kind": "forum",
     "credibility": "forum", "policy": "block"},
]


class ResearchService:
    def __init__(self, stores: TenantStoreProvider, observability: Optional[Observability] = None,
                 sources: Optional[List[Dict[str, Any]]] = None):
        self.stores = stores
        self.obs = observability or Observability(stores)
        self.default_sources = sources or DEFAULT_SOURCES

    # -- sources (allow/block) --------------------------------------------- #
    def seed_sources(self, tenant_id: str) -> List[Dict[str, Any]]:
        store = self.stores.for_tenant(tenant_id)
        for s in self.default_sources:
            store.execute(
                "INSERT INTO research_sources (id,tenant_id,name,url,kind,credibility,policy,created_at) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (new_id("src"), tenant_id, s["name"], s["url"], s["kind"],
                 s["credibility"], s["policy"], now_iso()))
        return self.list_sources(tenant_id)

    def list_sources(self, tenant_id: str) -> List[Dict[str, Any]]:
        store = self.stores.for_tenant(tenant_id)
        rows = store.query_all(
            "SELECT * FROM research_sources WHERE tenant_id=? ORDER BY credibility", (tenant_id,))
        return store.rows_to_dicts(rows)

    def allowed_sources(self, tenant_id: str) -> List[Dict[str, Any]]:
        return [s for s in self.list_sources(tenant_id) if s["policy"] == "allow"]

    def set_source_policy(self, tenant_id: str, source_id: str, policy: str) -> Dict[str, Any]:
        if policy not in ("allow", "block"):
            return {"error": "policy must be allow|block"}
        self.stores.for_tenant(tenant_id).execute(
            "UPDATE research_sources SET policy=? WHERE id=? AND tenant_id=?",
            (policy, source_id, tenant_id))
        for s in self.list_sources(tenant_id):
            if s["id"] == source_id:
                return {"source_id": source_id, "policy": policy, "ok": True}
        return {"error": "source not found"}

    def classify_credibility(self, kind: str) -> Dict[str, Any]:
        cred = kind if kind in CREDIBILITY_SCORE else "blog"
        return {"credibility": cred, "score": CREDIBILITY_SCORE[cred]}

    # -- search ------------------------------------------------------------ #
    def search(self, tenant_id: str, query: str,
               results: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        """Return cited, source-flagged claims. `results` is what an approved
        search provider would return; offline it is empty (never fabricated)."""
        allowed = {s["name"] for s in self.allowed_sources(tenant_id)}
        out: List[Dict[str, Any]] = []
        for r in results or []:
            if r.get("source_name") not in allowed:
                continue  # block-listed provider never contributes
            cred = self.classify_credibility(r.get("kind", "blog"))
            out.append({
                "query": query,
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "source_name": r.get("source_name", ""),
                "snippet": r.get("snippet", ""),
                "origin": "external",                      # always distinguished
                "credibility": cred["credibility"],
                "confidence": cred["score"],
            })
        self.obs.event(tenant_id=tenant_id, stage="research.search", actor="research",
                       status="OK", meta={"query": query, "results": len(out)})
        return out

    # -- persist cited docs ------------------------------------------------- #
    def capture(self, tenant_id: str, query: str,
                claims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        saved = []
        store = self.stores.for_tenant(tenant_id)
        for c in claims:
            did = new_id("rd")
            store.execute(
                "INSERT INTO research_docs (id,tenant_id,query,url,title,source_id,credibility,"
                "snippet,claims,origin,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (did, tenant_id, query, c.get("url", ""), c.get("title", ""),
                 c.get("source_id", ""), c.get("credibility", "blog"),
                 c.get("snippet", ""), dump_json([c]), c.get("origin", "external"),
                 "CAPTURED", now_iso()))
            saved.append({"id": did, "title": c.get("title", ""), "url": c.get("url", ""),
                          "credibility": c.get("credibility", "blog"),
                          "origin": c.get("origin", "external"), "query": query})
        self.obs.event(tenant_id=tenant_id, stage="research.capture", actor="research",
                       status="OK", meta={"docs": len(saved)})
        return saved

    def list_docs(self, tenant_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        store = self.stores.for_tenant(tenant_id)
        rows = store.query_all(
            "SELECT * FROM research_docs WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
            (tenant_id, limit))
        out = []
        for r in store.rows_to_dicts(rows):
            r["claims"] = load_json(r["claims"], [])
            out.append(r)
        return out

    # -- promote a cited claim to the Brain (never auto-approved) ----------- #
    def promote(self, tenant_id: str, doc_id: str, by: str = "analyst",
                note: str = "") -> Optional[Dict[str, Any]]:
        store = self.stores.for_tenant(tenant_id)
        row = store.query_one(
            "SELECT * FROM research_docs WHERE id=? AND tenant_id=?", (doc_id, tenant_id))
        if not row:
            return None
        claims = load_json(row["claims"], [])
        claim = claims[0] if claims else {}
        brain = CompanyBrain(store, tenant_id)
        node = brain.create(
            NodeKind.EXTERNAL,
            title=claim.get("title") or row["title"] or ("External claim: " + row["query"]),
            summary=claim.get("snippet") or "",
            payload={"origin": "external", "url": row["url"], "credibility": row["credibility"],
                     "claims": claims, "query": row["query"]},
            confidence={"source": CREDIBILITY_SCORE.get(row["credibility"], 0.4),
                        "definition": 0.0, "review": 0.0, "evidence": 0.0,
                        "freshness": 1.0, "reproducibility": 0.0},
            source_ref=row["url"], evidence_ref=doc_id, created_by=by,
            status=ReviewStatus.CANDIDATE)  # hard gate: CANDIDATE, senior-only promotion
        self.obs.event(tenant_id=tenant_id, stage="research.promote", actor=by,
                       resource=node.id, status="OK",
                       meta={"doc": doc_id, "note": note})
        return node.to_dict()

    def overview(self, tenant_id: str) -> Dict[str, Any]:
        docs = self.list_docs(tenant_id)
        return {
            "tenant_id": tenant_id,
            "sources": len(self.list_sources(tenant_id)),
            "allowed_sources": len(self.allowed_sources(tenant_id)),
            "captured_docs": len(docs),
            "external_claims": len(docs),
            "internal_vs_external": {"internal": 0, "external": len(docs)},
            "by_credibility": {k: sum(1 for d in docs if d["credibility"] == k)
                               for k in CREDIBILITY_SCORE},
        }


__all__ = ["ResearchService", "CREDIBILITY_SCORE", "DEFAULT_SOURCES"]