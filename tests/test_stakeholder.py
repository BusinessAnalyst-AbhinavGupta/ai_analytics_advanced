"""P6 — Stakeholder analyst tests (reuse approved, refresh, escalate, feedback)."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from analytics_platform.api import FeedbackIn, StakeholderIn, create_app
from analytics_platform.domain import AnswerMode, DataSourceKind, NodeKind
from analytics_platform.fixtures import WEEKLY_ORDER_SQL, build_retail_warehouse
from tests.test_api import app_ctx, call


class TestStakeholder(unittest.TestCase):
    def setUp(self):
        self.ctx, self.base = app_ctx(warehouse=build_retail_warehouse())
        self.tid = self.ctx.tenants.create_tenant("StakeCo", retention_days=90).id
        self.app = create_app(self.ctx)
        self.ctx.tenants.add_datasource(self.tid, "Events", DataSourceKind.DIRECT_DB,
                                        dialect="athena", tables=["events"])
        self.ctx.pipeline.register_approved_query(
            self.tid, WEEKLY_ORDER_SQL, "monthly retail orders",
            "how many retail orders per month", by="admin")

    def tearDown(self):
        self.base.close()

    def test_reuse_approved_query_with_citation(self):
        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        self.assertEqual(res["answer_mode"], AnswerMode.REFRESHED_APPROVED_QUERY.value)
        self.assertEqual(res["status"], "ANSWERED")
        self.assertFalse(res["escalated"])
        self.assertEqual(len(res["citations"]), 1)
        self.assertEqual(res["citations"][0]["title"], "monthly retail orders")
        self.assertIn("monthly retail orders", res["answer"])

    def test_approved_definition_falls_through(self):
        brain = self.ctx.pipeline.brain(self.tid)
        d = brain.create(NodeKind.DEFINITION, "gross margin",
                         summary="gross margin is revenue minus cost of goods sold")
        brain.submit(d.id, by="junior")
        brain.approve(d.id, by="senior")
        res = self.ctx.stakeholder.answer(self.tid, "gross margin")
        self.assertEqual(res["answer_mode"], AnswerMode.DIRECT_FROM_APPROVED_KNOWLEDGE.value)
        self.assertIn("gross margin", res["answer"])

    def test_high_risk_escalates(self):
        res = self.ctx.stakeholder.answer(self.tid, "list the personally identifiable info we store")
        self.assertTrue(res["escalated"])
        self.assertEqual(res["answer_mode"], AnswerMode.REQUIRES_SENIOR_REVIEW.value)

    def test_no_approved_knowledge_cannot_answer(self):
        res = self.ctx.stakeholder.answer(self.tid, "explain our warehouse picking policy in "
                                                    "minute detail")
        self.assertEqual(res["answer_mode"], AnswerMode.CANNOT_ANSWER.value)

    def test_feedback_and_quality(self):
        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        fb = self.ctx.stakeholder.record_feedback(self.tid, res["answer_id"], "sarah", "up")
        self.assertEqual(fb["rating"], "up")
        q = self.ctx.stakeholder.quality(self.tid)
        self.assertEqual(q["total_questions"], 1)
        self.assertEqual(q["feedback_count"], 1)
        self.assertEqual(q["acceptance_rate"], 1.0)
        self.assertEqual(q["reuse_count"], 1)

    def test_routes(self):
        res = call(self.app, "POST", "/stakeholder/{tenant_id}/answer", self.tid,
                   StakeholderIn(question="how many retail orders per month"))
        self.assertEqual(res["answer_mode"], AnswerMode.REFRESHED_APPROVED_QUERY.value)
        fb = call(self.app, "POST", "/stakeholder/{tenant_id}/feedback", self.tid,
                  FeedbackIn(answer_id=res["answer_id"], rating="up"))
        self.assertEqual(fb["rating"], "up")
        q = call(self.app, "GET", "/stakeholder/{tenant_id}/quality", self.tid)
        self.assertEqual(q["feedback_count"], 1)

    def test_disabled_stakeholder_returns_graceful_message(self):
        self.ctx.tenants.set_analyst_config(self.tid, {"stakeholder": {"enabled": False}})
        res = self.ctx.stakeholder.answer(self.tid, "explain our warehouse picking policy")
        self.assertEqual(res["answer_mode"], AnswerMode.CANNOT_ANSWER.value)
        self.assertIn("disabled", res["answer"].lower())

    @patch("analytics_platform.stakeholder.make_role_client")
    def test_dynamic_llm_resolution(self, mock_make_role_client):
        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.return_value.text = '{"answer": "Synthesized stakeholder answer text."}'
        mock_llm.generate.return_value.tokens_in = 100
        mock_llm.generate.return_value.tokens_out = 50
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "stakeholder": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-haiku"}
        })

        res = self.ctx.stakeholder.answer(self.tid, "explain our warehouse picking policy")
        self.assertEqual(res["answer_mode"], AnswerMode.NEW_LOW_RISK_ANALYSIS.value)
        self.assertEqual(res["answer"], "Synthesized stakeholder answer text.")

        mock_make_role_client.assert_called_once()
        args, _ = mock_make_role_client.call_args
        self.assertIs(args[0], self.ctx.stakeholder.settings)
        self.assertEqual(args[1].provider, "openrouter")
        self.assertEqual(args[1].model, "anthropic/claude-3-haiku")

    @patch("analytics_platform.stakeholder.make_role_client")
    def test_approved_query_chart_synthesis_token_accounting(self, mock_make_role_client):
        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.return_value.text = '{"answer": "Reused answer", "chart_config": {"type": "BarChart"}}'
        mock_llm.generate.return_value.tokens_in = 200
        mock_llm.generate.return_value.tokens_out = 80
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "stakeholder": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-haiku"}
        })

        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        self.assertEqual(res["answer_mode"], AnswerMode.REFRESHED_APPROVED_QUERY.value)
        self.assertIsNotNone(res["chart_config"])
        self.assertEqual(res["chart_config"]["type"], "BarChart")
        self.assertTrue(len(res["chart_data"]) > 0)
        self.assertGreater(res["cost"], 0.0)


if __name__ == "__main__":
    unittest.main()