"""Triage service tests — review inbox over the governed Brain."""
from __future__ import annotations

import unittest

from analytics_platform.domain import NodeKind, ReviewStatus
from analytics_platform.triage import ACTIONABLE, TriageService
from tests.helpers import make_ctx


def add_candidates(brain, kind, titles, status=ReviewStatus.CANDIDATE):
    for t in titles:
        brain.create(kind, t, summary=f"summary {t}", status=status)


class TestTriage(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.tid = self.ctx.tenants.create_tenant("ReviewCo").id
        self.brain = self.ctx.pipeline.brain(self.tid)
        self.svc = TriageService(self.ctx.store, self.ctx.obs)

    def tearDown(self):
        self.ctx.close()

    def test_summary_counts_and_actionable(self):
        add_candidates(self.brain, NodeKind.QUERY, ["a", "b"])
        add_candidates(self.brain, NodeKind.IDIOM, ["c"], status=ReviewStatus.APPROVED)
        s = self.svc.summary(self.tid)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["actionable"], 2)
        self.assertEqual(s["approved"], 1)
        self.assertEqual(s["by_kind"][NodeKind.QUERY.value], 2)

    def test_queue_filter_by_kind_and_search(self):
        add_candidates(self.brain, NodeKind.QUERY, ["revenue", "orders"])
        add_candidates(self.brain, NodeKind.IDIOM, ["row-number idiom"])
        q = self.svc.queue(self.tid, kind=NodeKind.QUERY)
        self.assertEqual(len(q), 2)
        q = self.svc.queue(self.tid, search="rev")
        self.assertEqual([n.title for n in q], ["revenue"])

    def test_approve_single_promotes_to_approved(self):
        add_candidates(self.brain, NodeKind.QUERY, ["revenue"])
        nid = self.brain.all(limit=10)[0].id
        res = self.svc.approve(self.tid, [nid], by="senior")
        self.assertEqual(res["approved"], [nid])
        node = self.brain.get(nid)
        self.assertEqual(node.status, ReviewStatus.APPROVED)
        self.assertEqual(node.confidence["review"], 1.0)
        self.assertEqual(node.reviewed_by, "senior")

    def test_approve_never_touches_non_actionable(self):
        add_candidates(self.brain, NodeKind.QUERY, ["approved"], status=ReviewStatus.APPROVED)
        nid = self.brain.all(limit=10)[0].id
        before = self.brain.get(nid).status
        res = self.svc.approve(self.tid, [nid])
        self.assertEqual(res["approved"], [])
        self.assertEqual(len(res["skipped"]), 1)
        self.assertEqual(self.brain.get(nid).status, before)

    def test_reject(self):
        add_candidates(self.brain, NodeKind.QUERY, ["junk"])
        nid = self.brain.all(limit=10)[0].id
        res = self.svc.reject(self.tid, [nid], notes="dup")
        self.assertEqual(res["rejected"], [nid])
        self.assertEqual(self.brain.get(nid).status, ReviewStatus.REJECTED)

    def test_bulk_approve_by_kind_only(self):
        add_candidates(self.brain, NodeKind.QUERY, ["q1", "q2"])
        add_candidates(self.brain, NodeKind.IDIOM, ["i1"])
        res = self.svc.bulk(self.tid, kind=NodeKind.QUERY, action="approve")
        self.assertEqual(len(res["approved"]), 2)
        statuses = {self.brain.get(n.id).status for n in self.brain.all(limit=10)}
        self.assertIn(ReviewStatus.APPROVED, statuses)
        # the idiom stayed CANDIDATE
        idiom = next(n for n in self.brain.all(limit=10) if n.kind == NodeKind.IDIOM)
        self.assertEqual(idiom.status, ReviewStatus.CANDIDATE)

    def test_bulk_reject(self):
        add_candidates(self.brain, NodeKind.IDIOM, ["i1", "i2"])
        res = self.svc.bulk(self.tid, kind=NodeKind.IDIOM, action="reject")
        self.assertEqual(len(res["rejected"]), 2)

    def test_conflicts_reported(self):
        add_candidates(self.brain, NodeKind.QUERY, ["same title"])
        add_candidates(self.brain, NodeKind.QUERY, ["same title"])
        self.assertEqual(len(self.svc.conflicts(self.tid)), 1)

    def test_conflicts_exclude_rejected_and_clear_on_reject(self):
        add_candidates(self.brain, NodeKind.QUERY, ["dup title"])
        add_candidates(self.brain, NodeKind.QUERY, ["dup title"])
        add_candidates(self.brain, NodeKind.QUERY, ["dup title"],
                       status=ReviewStatus.REJECTED)
        # the already-rejected ghost must not count: 2 kept -> 1 conflict
        groups = self.svc.conflicts(self.tid)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 2)
        # rejecting one of the two kept duplicates resolves the group
        kept = [n.id for n in self.brain.all(limit=10) if n.status in ACTIONABLE]
        res = self.svc.reject(self.tid, kept[:1], notes="dedupe")
        self.assertEqual(res["rejected"], kept[:1])
        self.assertEqual(self.svc.conflicts(self.tid), [])

    def test_dedupe_keeps_one_supersedes_approved_rejects_actionable(self):
        add_candidates(self.brain, NodeKind.BUSINESS_RULE, ["dup rule"],
                       status=ReviewStatus.APPROVED)
        add_candidates(self.brain, NodeKind.BUSINESS_RULE, ["dup rule"],
                       status=ReviewStatus.APPROVED)
        add_candidates(self.brain, NodeKind.BUSINESS_RULE, ["dup rule"])  # CANDIDATE
        self.assertEqual(len(self.svc.conflicts(self.tid)), 1)
        nodes = self.brain.all(limit=10)
        approved = [n.id for n in nodes if n.status == ReviewStatus.APPROVED]
        candidate = next(n.id for n in nodes if n.status == ReviewStatus.CANDIDATE)
        keep = approved[0]
        res = self.svc.dedupe(self.tid, keep, drop=approved[1:] + [candidate],
                              by="senior")
        # the second approved was superseded; the candidate was rejected
        self.assertEqual(res["superseded"], approved[1:])
        self.assertEqual(res["rejected"], [candidate])
        # the kept node is the only non-discarded one with that title
        self.assertEqual(self.svc.conflicts(self.tid), [])
        kept_node = self.brain.get(keep)
        self.assertEqual(kept_node.status, ReviewStatus.APPROVED)

    def test_actionable_contains_candidate_and_under_review(self):
        self.assertIn(ReviewStatus.CANDIDATE, ACTIONABLE)
        self.assertIn(ReviewStatus.UNDER_REVIEW, ACTIONABLE)
        self.assertNotIn(ReviewStatus.APPROVED, ACTIONABLE)


if __name__ == "__main__":
    unittest.main()