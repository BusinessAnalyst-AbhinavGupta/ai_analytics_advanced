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

import os
import tempfile
import unittest

import pandas as pd

from analytics_platform.brain.store import CompanyBrain
from analytics_platform.domain import DataSourceKind, NodeKind, ReviewStatus
from analytics_platform.junior import JuniorEngine
from analytics_platform.junior_worker import JuniorWorker
from analytics_platform.senior import SeniorService
from tests.helpers import make_ctx

_WAREHOUSE = {"things": pd.DataFrame({"col": [1, 2, 3, 4]})}


def _worker(ctx, tid: str, review_backlog_max: int = 3, **kw):
    eng = JuniorEngine(ctx.stores, executor=ctx.executor, tenants=ctx.tenants,
                       observability=ctx.obs)
    return JuniorWorker(ctx.stores, eng, tenant_id=tid,
                        observability=ctx.obs, reviews_dir=kw.get("reviews_dir", "data/reviews"),
                        work_start=kw.get("work_start", "10:00"),
                        work_end=kw.get("work_end", "19:00"),
                        min_interval_minutes=kw.get("min_interval_minutes", 60),
                        daily_cap=kw.get("daily_cap", 3),
                        review_backlog_max=review_backlog_max,
                        autopromote_cap=kw.get("autopromote_cap", 500),
                        supporting_cap=kw.get("supporting_cap", 5))


class TestSeniorReviewQueue(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx(_WAREHOUSE)
        self.reviews = tempfile.mkdtemp()
        self.tid = self.ctx.tenants.create_tenant("ReviewCo").id
        self.ctx.tenants.set_company_profile(self.tid, {
            "name": "ReviewCo",
            "targets": [{"name": "Grow orders", "category": "growth", "priority": 1}]})
        # depth 2 => high-level (human-governed) work, so runs land in the inbox
        self.ctx.tenants.set_analyst_config(self.tid, {"junior_depth": 2}, changed_by="human")
        self.eng = JuniorEngine(self.ctx.stores, executor=self.ctx.executor,
                                tenants=self.ctx.tenants, observability=self.ctx.obs)
        self.worker = _worker(self.ctx, self.tid, reviews_dir=self.reviews, supporting_cap=0)
        self.senior = SeniorService(self.ctx.stores, self.ctx.pipeline, self.ctx.tenants,
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
        self.assertEqual(res["level"], "high")
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
        q1 = res["question"]
        self.assertIn(q1, self.worker._answered_questions())
        nxt = self.worker.pick_problem_statement()
        self.assertNotEqual(nxt.get("question"), q1)


class TestReviewBacklogGate(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx(_WAREHOUSE)
        self.reviews = tempfile.mkdtemp()
        self.tid = self.ctx.tenants.create_tenant("BacklogCo").id
        self.ctx.tenants.set_company_profile(self.tid, {
            "name": "BacklogCo",
            "targets": [{"name": "Grow", "category": "growth", "priority": 1}]})
        # depth 2 => high-level (human-governed) runs, which DO backlog the inbox
        self.ctx.tenants.set_analyst_config(self.tid, {"junior_depth": 2}, changed_by="human")
        self.worker = _worker(self.ctx, self.tid, review_backlog_max=2,
                              reviews_dir=self.reviews, work_start="00:00",
                              work_end="23:59", min_interval_minutes=0, daily_cap=100,
                              supporting_cap=0)

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


class TestTwoTierJunior(unittest.TestCase):
    """CP-15: promotion/demotion controls the level of work — low-level auto-folds to
    FINDINGs (under cap, never in the human inbox); depth-2 high-level is human-governed
    and may spawn exempt supporting workpapers."""

    _WH = {"things": pd.DataFrame({
        "col": [1, 2, 3, 4], "channel": ["a", "b", "a", "b"],
        "created_at": ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]})}

    def setUp(self):
        self.ctx = make_ctx(self._WH)
        self.reviews = tempfile.mkdtemp()
        self.tid = self.ctx.tenants.create_tenant("TwoTier").id
        self.ctx.tenants.set_company_profile(self.tid, {
            "name": "TwoTier",
            "targets": [{"name": "Funnel conversion", "category": "growth",
                         "metric_refs": ["col"], "priority": 1}]})
        # catalog must see the sample table so the low-level taxonomy populates
        self.ctx.tenants.add_datasource(self.tid, "warehouse", DataSourceKind.DIRECT_DB,
                                        dialect="duckdb", tables=["things"], connected=True)
        self.senior = SeniorService(self.ctx.stores, self.ctx.pipeline, self.ctx.tenants,
                                    observability=self.ctx.obs, reviews_dir=self.reviews)

    def tearDown(self):
        self.ctx.close()

    def _set_depth(self, d):
        self.ctx.tenants.set_analyst_config(self.tid, {"junior_depth": d}, changed_by="human")

    def _findings(self):
        return [n for n in CompanyBrain(self.ctx.stores.for_tenant(self.tid), self.tid).all(kind=NodeKind.FINDING)
                if n.status == ReviewStatus.APPROVED]

    def test_demoted_junior_runs_low_level_and_auto_folds(self):
        self._set_depth(0)
        worker = _worker(self.ctx, self.tid, reviews_dir=self.reviews, autopromote_cap=10)
        res = worker.run_cycle(self.tid, force=True)
        self.assertTrue(res["ran"])
        self.assertEqual(res["level"], "low")
        self.assertTrue(res["promoted"])
        # low-level, produced data -> auto-accepted, promoted, NOT in the human inbox
        self.assertNotIn(res["run_id"], {r["run_id"] for r in self.senior.queue(self.tid)})
        self.assertEqual(len(self._findings()), 1)

    def test_autopromote_cap_stops_promotion_but_run_saved(self):
        self._set_depth(0)
        worker = _worker(self.ctx, self.tid, reviews_dir=self.reviews, autopromote_cap=0)
        res = worker.run_cycle(self.tid, force=True)
        self.assertTrue(res["ran"])
        self.assertFalse(res["promoted"])
        self.assertEqual(len(self._findings()), 0)
        self.assertIsNotNone(self.ctx.pipeline.get_run(self.tid, res["run_id"]))
        self.assertTrue(os.path.exists(
            os.path.join(self.reviews, self.tid, f"{res['run_id']}.md")))

    def test_promoted_junior_only_runs_high_level_and_spawns_exempt_workpapers(self):
        self._set_depth(2)
        worker = _worker(self.ctx, self.tid, reviews_dir=self.reviews,
                         autopromote_cap=100, supporting_cap=2)
        res = worker.run_cycle(self.tid, force=True)
        self.assertTrue(res["ran"])
        self.assertEqual(res["level"], "high")
        self.assertIn(res["category"], ("rca", "hypothesis"))
        self.assertFalse(res["promoted"])  # high-level is never auto-folded
        self.assertIn(res["run_id"], {r["run_id"] for r in self.senior.queue(self.tid)})
        self.assertGreaterEqual(len(res["supporting"]), 1)
        for sid in res["supporting"]:
            run = self.ctx.pipeline.get_run(self.tid, sid)
            self.assertEqual(run.level, "low")
            self.assertEqual(run.supportive_of, res["run_id"])
            self.assertEqual(run.review_status, ReviewStatus.APPROVED)
            self.assertNotIn(sid, {r["run_id"] for r in self.senior.queue(self.tid)})
            self.assertTrue(os.path.exists(
                os.path.join(self.reviews, self.tid, f"{res['run_id']}__{sid}.md")))
        # supporting runs are EXEMPT from the daily cap: exactly 1 standalone consumed,
        # and the backlog gate only sees the high-level run (not the workpapers).
        self.assertEqual(worker._runs_today(worker._clock()), 1)
        self.assertEqual(worker._pending_review_count(self.tid), 1)


if __name__ == "__main__":
    unittest.main()