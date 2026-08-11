"""Junior maturity-stage engine tests (offline, retail warehouse)."""
from __future__ import annotations

import unittest

from unittest.mock import patch

from analytics_platform.domain import DataSourceKind, NodeKind, ReviewStatus
from analytics_platform.fixtures import WEEKLY_ORDER_SQL, build_retail_warehouse
from analytics_platform.junior import JuniorEngine
from tests.helpers import make_ctx



def approve_query(brain, sql, title="approved metric"):
    n = brain.create(NodeKind.QUERY, title, payload={"sql": sql, "dialect": "athena"})
    brain.submit(n.id, by="senior")
    brain.approve(n.id, by="senior")
    return n


class _FakeLLM:
    """A live-looking LLM client (name != 'null') that returns canned text."""

    name = "gateway"

    def __init__(self, text):
        self.text = text

    def generate(self, prompt, system_prompt="", **kw):
        from analytics_platform.llm.client import LLMResponse
        return LLMResponse(text=self.text, tokens_out=9)


class TestJunior(unittest.TestCase):
    def _ctx_with_warehouse(self):
        ctx = make_ctx(warehouse=build_retail_warehouse())
        return ctx

    def setUp(self):
        self.ctx = self._ctx_with_warehouse()
        self.tid = self.ctx.tenants.create_tenant("JuniorCo").id
        self.engine = JuniorEngine(self.ctx.store, executor=self.ctx.executor,
                                   tenants=self.ctx.tenants)

    def tearDown(self):
        self.ctx.close()

    def test_stage0_without_approved_knowledge(self):
        s = self.engine.stage(self.tid)
        self.assertEqual(s["stage"], 0)
        self.assertEqual(s["approved_queries"], 0)

    def test_reproduce_metrics_runs_approved_query(self):
        approve_query(self.ctx.pipeline.brain(self.tid), WEEKLY_ORDER_SQL)
        repro = self.engine.reproduce_metrics(self.tid)
        self.assertEqual(repro["attempted"], 1)
        self.assertEqual(repro["reproduced"], 1)
        self.assertEqual(repro["failed"], [])

    def test_stage2_with_approved_query_still_uses_warehouse(self):
        approve_query(self.ctx.pipeline.brain(self.tid), WEEKLY_ORDER_SQL)
        s = self.engine.stage(self.tid)
        self.assertEqual(s["approved_queries"], 1)
        self.assertGreaterEqual(s["stage"], 2)

    def test_ignore_unapproved_and_non_query_nodes(self):
        brain = self.ctx.pipeline.brain(self.tid)
        # a CANDIDATE query must NOT be reproduced
        brain.create(NodeKind.QUERY, "draft", payload={"sql": WEEKLY_ORDER_SQL,
                                                       "dialect": "athena"})
        approve_query(brain, WEEKLY_ORDER_SQL, title="real")
        repro = self.engine.reproduce_metrics(self.tid)
        self.assertEqual(repro["attempted"], 1)
        self.assertEqual(repro["reproduced"], 1)

    def test_catalog_describes_registered_table(self):
        self.ctx.tenants.add_datasource(self.tid, "Events", DataSourceKind.DIRECT_DB,
                                        dialect="athena", tables=["events"])
        c = self.engine.catalog(self.tid)
        self.assertEqual(c["tables_known"], 1)
        self.assertEqual(c["tables_described"], 1)
        cols = c["tables"][0]["columns"]
        self.assertIn("action", cols)
        self.assertIn("revenue", cols)

    def test_catalog_unknown_table_reports_error(self):
        self.ctx.tenants.add_datasource(self.tid, "Missing", DataSourceKind.DIRECT_DB,
                                        tables=["nope_missing"])
        c = self.engine.catalog(self.tid)
        self.assertEqual(c["tables_known"], 1)
        self.assertEqual(c["tables_described"], 0)
        self.assertTrue(c["tables"][0]["error"])

    def test_suggest_questions_from_targets(self):
        self.ctx.tenants.set_company_profile(self.tid, {
            "name": "X", "industry": "ecommerce", "description": "d",
            "customers": "c", "value_creation": "v", "revenue_model": "r",
            "targets": [{"name": "Raise funnel conversion", "category": "funnel",
                         "priority": 1, "metric_refs": ["view_to_order_pct"]},
                        {"name": "Grow orders", "category": "growth", "priority": 2}]})
        s = self.engine.suggest_questions(self.tid)
        self.assertGreaterEqual(s["count"], 1)
        self.assertTrue(any("Raise funnel conversion" in x["question"]
                            for x in s["suggestions"]))

    def test_suggest_questions_fallback_without_targets(self):
        approve_query(self.ctx.pipeline.brain(self.tid), WEEKLY_ORDER_SQL,
                      title="Order completion by month")
        s = self.engine.suggest_questions(self.tid)
        self.assertGreaterEqual(s["count"], 1)
        self.assertTrue(any("reproduce" in x["question"] for x in s["suggestions"]))

    def test_suggest_questions_ignores_llm_when_null(self):
        # self.engine uses the default NullClient -> no "llm"-source suggestions
        s = self.engine.suggest_questions(self.tid)
        self.assertFalse(any(x["source"] == "llm" for x in s["suggestions"]))

    @patch("analytics_platform.junior.make_role_client")
    def test_suggest_questions_llm_enrichment(self, mock_make_role_client):
        mock_make_role_client.return_value = _FakeLLM("1. What drives the view_to_order drop?\n"
                                                       "2. Segment completion rate by region")
        eng = JuniorEngine(self.ctx.store, executor=self.ctx.executor,
                           tenants=self.ctx.tenants)
        s = eng.suggest_questions(self.tid)
        self.assertTrue(any(x["source"] == "llm" for x in s["suggestions"]))
        self.assertGreaterEqual(s["count"], 1)
        texts = {x["question"] for x in s["suggestions"]}
        self.assertTrue(any("view_to_order" in t or "completion" in t for t in texts))

    @patch("analytics_platform.junior.make_role_client")
    def test_suggest_questions_llm_failure_is_non_fatal(self, mock_make_role_client):
        class _Boom(_FakeLLM):
            def generate(self, prompt, system_prompt="", **kw):
                raise RuntimeError("boom")
        mock_make_role_client.return_value = _Boom("")
        eng = JuniorEngine(self.ctx.store, executor=self.ctx.executor,
                           tenants=self.ctx.tenants)
        s = eng.suggest_questions(self.tid)  # must not raise
        self.assertFalse(any(x["source"] == "llm" for x in s["suggestions"]))

    def test_runs_over_browser_executor_seam_offline(self):
        """JuniorEngine drives the live BrowserSessionExecutor (injectable runner).

        Proves the stage-3 reproduction / catalog path works against the same executor
        used for real Metabase — host-guarded, cookie stays in the browser — without
        needing Chrome here (stub runner simulates the JS roundtrip).
        """
        from analytics_platform.execution.browser_session import BrowserSessionExecutor
        from tests.test_browser_session import exec_payload, make_runner, probe_payload

        # a mapped table + an approved query the engine should reproduce
        self.ctx.tenants.add_datasource(self.tid, "Events", DataSourceKind.DIRECT_DB,
                                        dialect="athena", tables=["events"])
        approve_query(self.ctx.pipeline.brain(self.tid), WEEKLY_ORDER_SQL)

        live = BrowserSessionExecutor(
            database_id=59, expected_host="metabase.om.yo-digital.com",
            runner=make_runner(probe_payload(host="metabase.om.yo-digital.com"),
                               exec_payload(rows=[[1, "a"]], cols=["month", "orders"])))
        eng = JuniorEngine(self.ctx.store, executor=live, tenants=self.ctx.tenants)

        # stage () reproduces the approved query through the browser executor
        st = eng.stage(self.tid)
        self.assertGreaterEqual(st["reproduction"]["attempted"], 1)
        self.assertGreaterEqual(st["reproduction"]["reproduced"], 1)
        self.assertEqual(st["reproduction"]["failed"], [])

        # catalog() describes the mapped table through the browser executor
        cat = eng.catalog(self.tid)
        self.assertEqual(cat["tables_known"], 1)
        self.assertEqual(cat["tables_described"], 1)
        self.assertIn("month", cat["tables"][0]["columns"])


if __name__ == "__main__":
    unittest.main()