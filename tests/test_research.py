"""P7 — External research tests (allow/block, cite, external flag, senior-only promote)."""
from __future__ import annotations

import unittest

from analytics_platform.api import PromoteIn2, ResearchBatchIn, create_app
from analytics_platform.domain import NodeKind, ReviewStatus
from tests.test_api import app_ctx, call

OK_CLAIM = {
    "source_name": "Official documentation",
    "title": "Churn benchmark for retail SaaS",
    "url": "https://docs.example.com/churn",
    "kind": "official",
    "snippet": "Median retail-SaaS monthly churn is ~5%.",
}
FORUM_CLAIM = {
    "source_name": "Community forum",
    "title": "Random churn advice",
    "url": "https://forum.example.com/x",
    "kind": "forum",
    "snippet": "Trust me, churn is low.",
}


class TestResearch(unittest.TestCase):
    def setUp(self):
        self.ctx, self.base = app_ctx()
        self.tid = self.ctx.tenants.create_tenant("ResearchCo").id
        self.app = create_app(self.ctx)
        self.sources = self.ctx.research.seed_sources(self.tid)

    def tearDown(self):
        self.base.close()

    def test_sources_seeded_and_allowed(self):
        self.assertEqual(len(self.sources), 5)
        allowed = self.ctx.research.allowed_sources(self.tid)
        names = {s["name"] for s in allowed}
        self.assertIn("Official documentation", names)
        self.assertNotIn("Community forum", names)  # block-listed by default

    def test_search_cites_and_flags_external(self):
        res = self.ctx.research.search(self.tid, "retail churn benchmark",
                                       results=[OK_CLAIM, FORUM_CLAIM])
        self.assertEqual(len(res), 1)  # forum is blocked
        claim = res[0]
        self.assertEqual(claim["origin"], "external")
        self.assertEqual(claim["credibility"], "official")
        self.assertEqual(claim["url"], OK_CLAIM["url"])
        self.assertTrue(claim["confidence"] > 0.9)

    def test_blocked_source_dropped_by_policy(self):
        forum = next(s for s in self.sources if s["name"] == "Community forum")
        res = self.ctx.research.search(self.tid, "x", results=[FORUM_CLAIM])
        self.assertEqual(res, [])

    def test_promote_never_auto_approves(self):
        docs = self.ctx.research.capture(self.tid, "retail churn", [OK_CLAIM])
        node = self.ctx.research.promote(self.tid, docs[0]["id"], by="analyst")
        self.assertEqual(node["kind"], NodeKind.EXTERNAL.value)
        self.assertEqual(node["status"], ReviewStatus.CANDIDATE.value)  # NOT approved
        self.assertEqual(node["payload"]["origin"], "external")

        # only the senior triage gate can make it company knowledge
        brain = self.ctx.pipeline.brain(self.tid)
        n = brain.get(node["id"])
        brain.submit(node["id"], by="analyst")
        brain.approve(node["id"], by="senior", notes="verified against internal data")
        self.assertEqual(brain.get(node["id"]).status, ReviewStatus.APPROVED)

    def test_overview_counts(self):
        self.ctx.research.capture(self.tid, "retail churn", [OK_CLAIM])
        ov = self.ctx.research.overview(self.tid)
        self.assertEqual(ov["captured_docs"], 1)
        self.assertEqual(ov["internal_vs_external"]["external"], 1)

    def test_routes(self):
        call(self.app, "POST", "/research/{tenant_id}/sources/seed", self.tid)
        docs = call(self.app, "POST", "/research/{tenant_id}/capture", self.tid,
                    ResearchBatchIn(query="retail churn", results=[OK_CLAIM]))
        self.assertEqual(len(docs), 1)
        got = call(self.app, "GET", "/research/{tenant_id}/docs", self.tid)
        self.assertEqual(len(got), 1)
        prom = call(self.app, "POST", "/research/{tenant_id}/promote", self.tid,
                    PromoteIn2(doc_id=docs[0]["id"]))
        self.assertEqual(prom["status"], ReviewStatus.CANDIDATE.value)


if __name__ == "__main__":
    unittest.main()