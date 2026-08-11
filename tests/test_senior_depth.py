"""Senior-depth tests (R1-R4): human-controlled junior depth, hypothesis
scaling, human-signoff mandate, AI-vs-human enforcement, and markdown render.
All offline and deterministic (no HTTP, no Chrome, no real LLM).
"""
from __future__ import annotations

import os
import tempfile
import unittest

from unittest.mock import MagicMock, patch

from analytics_platform.config import Settings
from analytics_platform.domain import (DataSourceKind, RunStatus,
                                      clamp_junior_depth)
from analytics_platform.markdown import render_analysis_md, write_analysis_md
from analytics_platform.senior import SeniorReviewer, SeniorService
from analytics_platform.fixtures import build_retail_warehouse
from tests.helpers import make_ctx



class TestJuniorDepth(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx(warehouse=build_retail_warehouse())
        self.tid = self.ctx.tenants.create_tenant("DepthCo").id
        self.ctx.tenants.set_company_profile(self.tid, {
            "name": "DepthCo",
            "targets": [{"name": "Grow orders", "category": "growth", "priority": 1}]})
        self.ctx.tenants.add_datasource(self.tid, "warehouse", DataSourceKind.DIRECT_DB,
                                        tables=["events"])
        self.ctx.pipeline.register_approved_query(
            self.tid, "SELECT * FROM events LIMIT 5", "Baseline events", by="admin")

    def tearDown(self):
        self.ctx.close()

    def test_clamp(self):
        self.assertEqual(clamp_junior_depth(-1), 0)
        self.assertEqual(clamp_junior_depth(5), 2)
        self.assertEqual(clamp_junior_depth("bogus"), 1)
        self.assertEqual(clamp_junior_depth(2), 2)

    def test_depth_round_trip_and_label(self):
        self.ctx.tenants.set_analyst_config(self.tid, {"junior_depth": 2,
                                                       "human_signoff_days": 3})
        cfg = self.ctx.tenants.get_analyst_config(self.tid)
        self.assertEqual(cfg.junior_depth, 2)
        self.assertEqual(cfg.depth_label, "advanced")
        self.assertEqual(cfg.human_signoff_days, 3)

    def test_suggest_questions_depth(self):
        from analytics_platform.junior import JuniorEngine
        eng = JuniorEngine(self.ctx.store, executor=self.ctx.executor,
                           tenants=self.ctx.tenants, observability=self.ctx.obs)
        self.ctx.tenants.set_analyst_config(self.tid, {"junior_depth": 0})
        basic = eng.suggest_questions(self.tid)
        self.ctx.tenants.set_analyst_config(self.tid, {"junior_depth": 2})
        adv = eng.suggest_questions(self.tid)
        self.assertEqual(basic["depth"], 0)
        self.assertEqual(adv["depth"], 2)
        self.assertIn("deep", {s["source"] for s in adv["suggestions"]})

    def test_suggest_hypotheses_scale(self):
        from analytics_platform.junior import JuniorEngine
        eng = JuniorEngine(self.ctx.store, executor=self.ctx.executor,
                           tenants=self.ctx.tenants, observability=self.ctx.obs)
        self.ctx.tenants.set_analyst_config(self.tid, {"junior_depth": 0})
        self.assertEqual(eng.suggest_hypotheses(self.tid)["hypotheses"], [])
        self.ctx.tenants.set_analyst_config(self.tid, {"junior_depth": 1})
        self.assertTrue(eng.suggest_hypotheses(self.tid)["hypotheses"])


def _completed_run(ctx, tid, question="Order completion rate by month",
                   sql="SELECT * FROM events LIMIT 5"):
    ctx.pipeline.register_approved_query(tid, sql, "Baseline events", by="admin")
    run = ctx.pipeline.run(tid, question)
    if run.status != RunStatus.COMPLETED:
        ctx.store.execute("UPDATE analysis_runs SET status=? WHERE id=? AND tenant_id=?",
                          (RunStatus.COMPLETED.value, run.id, tid))
        run = ctx.pipeline.get_run(tid, run.id)
    return run


class TestSeniorControl(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx(warehouse=build_retail_warehouse())
        self.tid = self.ctx.tenants.create_tenant("SenCo").id
        self.senior = SeniorService(self.ctx.store, self.ctx.pipeline,
                                    self.ctx.tenants, observability=self.ctx.obs)
        self.ctx.tenants.add_datasource(self.tid, "warehouse", DataSourceKind.DIRECT_DB,
                                        tables=["events"])
        self.run = _completed_run(self.ctx, self.tid)

    def tearDown(self):
        self.ctx.close()

    def test_promote_downgrade_clamped(self):
        self.assertEqual(self.senior.set_junior_depth(self.tid, "up")["depth"], 2)
        self.assertEqual(self.senior.set_junior_depth(self.tid, "up")["depth"], 2)
        self.assertEqual(self.senior.set_junior_depth(self.tid, "down")["depth"], 1)
        self.assertEqual(self.senior.set_junior_depth(self.tid, "set",
                                                       level=0)["depth"], 0)
        self.assertEqual(self.senior.set_junior_depth(self.tid, "down")["depth"], 0)

    def test_requires_human_signoff_fresh_tenant(self):
        self.assertTrue(self.senior.requires_human_signoff(self.tid))
        self.ctx.tenants.set_analyst_config(self.tid, {"human_signoff_days": 0})
        self.assertFalse(self.senior.requires_human_signoff(self.tid))

    def test_review_human_approves(self):
        r = self.senior.review(self.tid, self.run.id, action="approve", by="human")
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"], "approved")

    def test_review_ai_denied_when_senior_ai_off(self):
        self.ctx.tenants.set_analyst_config(self.tid, {"senior": {"enabled": False}})
        r = self.senior.review(self.tid, self.run.id, action="approve", by="ai")
        self.assertFalse(r["ok"])
        self.assertIn("human", r["error"].lower())

    def test_review_ai_denied_in_signoff_window(self):
        self.ctx.tenants.set_analyst_config(self.tid,
                                            {"human_signoff_days": 7,
                                             "senior": {"enabled": True}})
        r = self.senior.review(self.tid, self.run.id, action="approve", by="ai")
        self.assertFalse(r["ok"])
        self.assertIn("human", r["error"].lower())

    def test_analysis_md_renders_and_writes(self):
        out = self.senior.analysis_md(self.tid, self.run.id)
        self.assertTrue(out["ok"])
        self.assertIn("## SQL", out["md"])
        self.assertIn(self.run.question_text, out["md"])
        self.assertTrue(os.path.exists(out["path"]))

    def test_senior_reviewer_alias_and_settings(self):
        s = Settings(llm_provider="ollama", llm_model="llama3")
        reviewer = SeniorReviewer(self.ctx.store, self.ctx.pipeline,
                                  self.ctx.tenants, observability=self.ctx.obs,
                                  settings=s)
        self.assertIs(reviewer.settings, s)
        self.assertIsInstance(reviewer, SeniorService)

    def test_run_senior_review_disabled_returns_none(self):
        self.ctx.tenants.set_analyst_config(self.tid, {"senior": {"enabled": False}})
        run_doc = {"id": self.run.id, "question_text": "Q", "sql": "SELECT 1", "answer": "A"}
        res = self.senior.run_senior_review(self.tid, run_doc)
        self.assertIsNone(res)

    def test_run_senior_review_signoff_mandate(self):
        self.ctx.tenants.set_analyst_config(self.tid, {"human_signoff_days": 7, "senior": {"enabled": True}})
        run_doc = {"id": self.run.id, "question_text": "Q", "sql": "SELECT 1", "answer": "A"}
        res = self.senior.run_senior_review(self.tid, run_doc)
        self.assertFalse(res["ok"])
        self.assertIn("human", res["error"].lower())

    @patch("analytics_platform.senior.make_role_client")
    def test_run_senior_review_dynamic_llm_resolution(self, mock_make_role_client):
        mock_llm = MagicMock()
        mock_llm.generate.return_value.text = "Senior review notes: Looks solid."
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "human_signoff_days": 0,
            "junior_depth": 2,
            "senior": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-sonnet"}
        })
        run_doc = {"id": self.run.id, "question_text": "Q", "sql": "SELECT 1", "answer": "A"}

        res = self.senior.run_senior_review(self.tid, run_doc)
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "approved")
        self.assertEqual(res["by"], "ai")

        mock_make_role_client.assert_called_once()
        args, _ = mock_make_role_client.call_args
        self.assertIs(args[0], self.senior.settings)
        self.assertEqual(args[1].provider, "openrouter")
        self.assertEqual(args[1].model, "anthropic/claude-3-sonnet")

    @patch("analytics_platform.senior.make_role_client")
    def test_run_senior_review_handles_none_res_text(self, mock_make_role_client):
        mock_llm = MagicMock()
        mock_llm.generate.return_value.text = None
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "human_signoff_days": 0,
            "junior_depth": 2,
            "senior": {"enabled": True}
        })
        run_doc = {"id": self.run.id, "question_text": "Q", "sql": "SELECT 1", "answer": "A"}

        res = self.senior.run_senior_review(self.tid, run_doc)
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "approved")


if __name__ == "__main__":
    unittest.main()