"""Onboarding wizard tests: provision, ingest, review, readiness staging."""
from __future__ import annotations

import unittest

from analytics_platform.domain import NodeKind, ReviewStatus
from analytics_platform.onboarding import OnboardingService
from tests.helpers import make_ctx

PROFILE = {
    "name": "Beta Corp", "industry": "saas", "region": "US",
    "description": "subscription analytics", "customers": "SMBs",
    "product": "dashboard", "value_creation": "insights", "revenue_model": "subscription",
    "targets": [{"name": "Grow ARR", "category": "growth", "priority": 1}],
}
LEGACY_SQL = "SELECT date_format(CAST(created_at AS TIMESTAMP), '%Y-%m') AS month, COUNT(*) AS n FROM events WHERE action='signup' GROUP BY 1"


class TestOnboarding(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.svc = OnboardingService(self.ctx.store, tenants=self.ctx.tenants,
                                     pipeline=self.ctx.pipeline,
                                     observability=self.ctx.obs)
        self.tid = self.svc.provision_company(PROFILE,
                                              datasource={"tables": ["events", "users"]},
                                              region="US").id

    def tearDown(self):
        self.ctx.close()

    def test_provision_creates_tenant_profile_and_tables(self):
        self.assertIsNotNone(self.ctx.tenants.get_tenant(self.tid))
        p = self.ctx.tenants.get_company_profile(self.tid)
        self.assertEqual(p.industry, "saas")
        self.assertEqual(len(p.targets), 1)
        srcs = self.ctx.tenants.list_datasources(self.tid)
        self.assertEqual(srcs[0]["tables"], ["events", "users"])
        r = self.svc.readiness(self.tid)
        self.assertTrue(r["criteria"]["company_profile"])
        self.assertTrue(r["criteria"]["main_tables_mapped"])

    def test_ingest_legacy_creates_candidates(self):
        nodes = self.svc.ingest_legacy(self.tid,
                                       [{"sql": LEGACY_SQL, "source_ref": "card_1",
                                         "title": "signup trend"}])
        self.assertTrue(nodes)
        self.assertTrue(all(n.status == ReviewStatus.CANDIDATE for n in nodes))
        kinds = {n.kind for n in nodes}
        self.assertTrue(NodeKind.QUERY in kinds)

    def test_review_approves_and_rejects(self):
        nodes = self.svc.ingest_legacy(self.tid, [{"sql": LEGACY_SQL, "source_ref": "card_1"}])
        q = next(n for n in nodes if n.kind == NodeKind.QUERY)
        d = next(n for n in nodes if n.kind == NodeKind.DEFINITION)
        res = self.svc.review(self.tid, approve_ids=[q.id], reject_ids=[d.id], by="senior")
        self.assertIn(q.id, res["approved"])
        self.assertIn(d.id, res["rejected"])
        self.assertTrue(self.svc.brain(self.tid).get(q.id).status.is_usable())
        self.assertEqual(self.svc.brain(self.tid).get(d.id).status, ReviewStatus.REJECTED)

    def test_readiness_stage_progresses_to_findings(self):
        self.svc.ingest_legacy(self.tid, [{"sql": LEGACY_SQL, "source_ref": "card_1"}])
        # approve all candidates -> defined_metrics + approved_queries true
        cands = self.svc.candidates(self.tid)
        self.svc.review(self.tid, approve_ids=[n.id for n in cands], by="senior")
        r = self.svc.readiness(self.tid)
        self.assertTrue(r["criteria"]["defined_metrics"])
        self.assertTrue(r["criteria"]["approved_queries"])
        self.assertGreaterEqual(r["stage"], 2)
        self.assertEqual(self.svc.maturity_label(r["stage"]),
                         ["provisioning", "data_discovery", "metric_understanding",
                          "process_analysis"][min(r["stage"], 3)])

    def test_digest_contains_profile_and_readiness(self):
        d = self.svc.digest(self.tid)
        self.assertEqual(d["profile"]["industry"], "saas")
        self.assertIn("readiness", d)


if __name__ == "__main__":
    unittest.main()