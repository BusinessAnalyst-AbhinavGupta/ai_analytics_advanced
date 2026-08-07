"""CP-14 tests: the senior review queue drains and the junior stops repeating.

Covers the fixes for "the review inbox keeps growing":
  * approved AND rejected analyses leave the senior inbox (`SeniorService.queue`),
    and an approved analysis becomes an approved FINDING in the knowledge graph,
  * the junior never re-asks a question it already answered (`pick_problem_statement`
    skips any question in `analysis_runs`), and
  * the junior pauses once `review_backlog_max` completed analyses await review,
    so the inbox cannot grow unbounded (`force` bypasses only for tests).
"""
from __future__ import annotations

import tempfile
import unittest

import pandas as pd

from analytics_platform.domain import NodeKind, ReviewStatus
from analytics_platform.junior import JuniorEngine
from analytics_platform.junior_worker import JuniorWorker
from analytics_platform.llm.client import NullClient
from analytics_platform.senior import SeniorService
from tests.helpers import make_ctx

_WAREHOUSE = {"things": pd.DataFrame({"col": [1, 2, 3, 4]})}


def _worker(ctx, tid: str, review_backlog_max: int = 3, **kw):
    eng = JuniorEngine(ctx.store, executor=ctx.executor, tenants=ctx.tenants,
                       observability=ctx.obs, llm=NullClient())
    return JuniorWorker(ctx.store, eng, tenant_id=tid,
                        observability=ctx.obs, reviews_dir=kw.get("reviews_dir", "data/reviews"),
                        work_start=kw.get("work_start", "10:00"),
                        work_end=kw.get("work_end", "19:00"),
                        min_interval_minutes=kw.get("min_interval_minutes", 60),
                        daily_cap=kw.get("daily_cap", 3),
                        review_backlog_max=review_backlog_max)


class TestSeniorReviewQueue(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx(_WAREHOUSE)
        self.reviews = tempfile.mkdtemp()
        self.tid = self.ctx.tenants.create_tenant("ReviewCo").id
        self.ctx.tenants.set_company_profile(self.tid, {
            "name": "ReviewCo",
            "targets": [{"name": "Grow orders", "category": "growth", "priority": 1}]})
        self.eng = JuniorEngine(self.ctx.store, executor=self.ctx.executor,
                                tenants=self.ctx.tenants, observability=self.ctx.obs,
                                llm=NullClient())
        self.worker = _worker(self.ctx, self.tid, reviews_dir=self.reviews)
        self.senior = SeniorService(self.ctx.store, self.ctx.pipeline, self.ctx.tenants,
                                    observability=self.ctx.obs, reviews_dir=self.reviews)
        # one approved, reproducible query in the Brain for the junior to explore
        brain = self.eng.brain(self.tid)
        node = brain.create(NodeKind.QUERY, title="What is funnel conversion?",
                            payload={"sql": "SELECT col FROM things LIMIT 1000"},
                            created_by="admin")
        brain.submit(node.id, by="admin")
        brain.approve(node.id, by="admin")

    def tearDown(self):
        self.ctx.close()

    def _run_one(self):
        return self.worker.run_cycle(self.tid, force=True)

    def test_approved_leaves_queue_and_enriches_knowledge_graph(self):
        res = self._run_one()
        run_id = res["run_id"]
        self.assertIn(run_id, {r["run_id"] for r in self.senior.queue(self.tid)})
        out = self.senior.review(self.tid, run_id, action="approve", by="human")
        self.assertTrue(out["ok"])
        self.assertNotIn(run_id, {r["run_id"] for r in self.senior.queue(self.tid)})
        findings = [n for n in self.eng.brain(self.tid).all(kind=NodeKind.FINDING)
                    if n.status == ReviewStatus.APPROVED]
        self.assertEqual(len(findings), 1)

    def test_rejected_leaves_queue_and_does_not_create_finding(self):
        a = self._run_one()
        b = self._run_one()
        run_id = b["run_id"]
        self.senior.review(self.tid, a["run_id"], action="approve", by="human")
        out = self.senior.review(self.tid, run_id, action="reject", by="human")
        self.assertTrue(out["ok"])
        self.assertNotIn(run_id, {r["run_id"] for r in self.senior.queue(self.tid)})
        findings = [n for n in self.eng.brain(self.tid).all(kind=NodeKind.FINDING)
                    if n.status == ReviewStatus.APPROVED]
        self.assertEqual(len(findings), 1)  # only the approved one, not the rejected

    def test_junior_never_reasks_an_answered_question(self):
        res = self._run_one()
        self.assertEqual(res["question"], "What is funnel conversion?")
        self.assertIn("What is funnel conversion?", self.worker._answered_questions())
        nxt = self.worker.pick_problem_statement()
        self.assertNotEqual(nxt.get("question"), "What is funnel conversion?")


class TestReviewBacklogGate(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx(_WAREHOUSE)
        self.reviews = tempfile.mkdtemp()
        self.tid = self.ctx.tenants.create_tenant("BacklogCo").id
        self.ctx.tenants.set_company_profile(self.tid, {
            "name": "BacklogCo",
            "targets": [{"name": "Grow", "category": "growth", "priority": 1}]})
        self.worker = _worker(self.ctx, self.tid, review_backlog_max=2,
                              reviews_dir=self.reviews, work_start="00:00",
                              work_end="23:59", min_interval_minutes=0, daily_cap=100)

    def tearDown(self):
        self.ctx.close()

    def test_pauses_once_pending_reviews_reach_max(self):
        self.assertTrue(self.worker.run_cycle(self.tid, force=True)["ran"])
        self.assertTrue(self.worker.run_cycle(self.tid, force=True)["ran"])
        self.assertEqual(self.worker._pending_review_count(self.tid), 2)
        res = self.worker.run_cycle(self.tid)
        self.assertFalse(res["ran"])
        self.assertEqual(res["reason"], "review_backlog")
        self.assertTrue(self.worker.run_cycle(self.tid, force=True)["ran"])


if __name__ == "__main__":
    unittest.main()