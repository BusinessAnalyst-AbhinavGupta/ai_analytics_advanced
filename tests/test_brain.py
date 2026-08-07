"""Company Brain lifecycle + isolation tests."""
from __future__ import annotations

import unittest

from analytics_platform.brain.store import BrainConflict
from analytics_platform.domain import NodeKind, ReviewStatus
from tests.helpers import make_ctx


class TestBrain(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.tid = self.ctx.tenants.create_tenant("Acme").id

    def tearDown(self):
        self.ctx.close()

    def test_lifecycle_to_approved(self):
        brain = self.ctx.pipeline.brain(self.tid)
        n = brain.create(NodeKind.QUERY, "order completion", payload={"sql": "SELECT 1"})
        self.assertEqual(n.status, ReviewStatus.CANDIDATE)
        brain.submit(n.id, by="junior")
        self.assertEqual(brain.get(n.id).status, ReviewStatus.UNDER_REVIEW)
        brain.approve(n.id, by="senior")
        node = brain.get(n.id)
        self.assertEqual(node.status, ReviewStatus.APPROVED)
        self.assertEqual(node.confidence["review"], 1.0)

    def test_illegal_transition_raises(self):
        brain = self.ctx.pipeline.brain(self.tid)
        n = brain.create(NodeKind.DEFINITION, "def")
        with self.assertRaises(BrainConflict):
            brain.approve(n.id, by="senior")  # CANDIDATE -> APPROVED is illegal (must go via UNDER_REVIEW)

    def test_tenant_isolation(self):
        tid_a = self.ctx.tenants.create_tenant("A").id
        tid_b = self.ctx.tenants.create_tenant("B").id
        brain_a = self.ctx.pipeline.brain(tid_a)
        n = brain_a.create(NodeKind.METRIC, "churn", summary="secret churn metric")
        brain_a.submit(n.id, by="junior")
        brain_a.approve(n.id, by="senior")
        brain_b = self.ctx.pipeline.brain(tid_b)
        self.assertEqual(brain_b.search("churn"), [])
        self.assertEqual(brain_b.get(n.id), None)

    def test_search_filters_by_usable_status(self):
        brain = self.ctx.pipeline.brain(self.tid)
        ok = brain.create(NodeKind.QUERY, "revenue query", summary="approved-ish")
        brain.submit(ok.id)
        brain.approve(ok.id)
        bad = brain.create(NodeKind.QUERY, "draft query", summary="draft")
        self.assertEqual([x.id for x in brain.search("query")], [ok.id])
        all_nodes = {x.id for x in brain.all(kind=NodeKind.QUERY)}
        self.assertIn(bad.id, all_nodes)

    def test_mark_stale(self):
        brain = self.ctx.pipeline.brain(self.tid)
        n = brain.create(NodeKind.BUSINESS_RULE, "rule")
        brain.submit(n.id)
        brain.approve(n.id)
        brain.mark_stale(n.id)
        self.assertEqual(brain.get(n.id).status, ReviewStatus.STALE)
        self.assertEqual(brain.search("rule"), [])


if __name__ == "__main__":
    unittest.main()