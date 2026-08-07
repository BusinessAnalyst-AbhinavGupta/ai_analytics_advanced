"""End-to-end pipeline test: question -> plan -> policy -> execute -> analyze.

Uses the synthetic retail warehouse + a golden query. Exercised twice:
  - source dialect 'athena' (golden query uses date_format -> transpiled to duckdb)
  - direct persisted SQL
"""
from __future__ import annotations

import unittest

from analytics_platform.domain import AnswerMode, DataSourceKind, NodeKind, ReviewStatus, RunStatus
from analytics_platform.fixtures import GOLDEN_QUERIES, build_retail_warehouse
from tests.helpers import make_ctx

NOVEL_SQL = (
    "SELECT region, AVG(revenue) AS avg_revenue FROM events "
    "WHERE action='order' AND status='completed' GROUP BY 1 ORDER BY 2 DESC LIMIT 20"
)


class TestPipelineE2E(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx(warehouse=build_retail_warehouse(rows=1500, seed=7))
        self.tid = self.ctx.tenants.create_tenant("Acme", region="DE").id
        self.ctx.tenants.add_datasource(self.tid, "warehouse", DataSourceKind.DIRECT_DB, tables=["events"])
        # golden query authored in athena (date_format) -> executor must transpile
        self.ctx.pipeline.settings.source_dialect = "athena"
        for g in GOLDEN_QUERIES:
            self.ctx.pipeline.register_approved_query(self.tid, g["sql"], g["title"],
                                                      g["summary"], by="admin")

    def tearDown(self):
        self.ctx.close()

    def test_approved_query_reuse_runs_and_completes(self):
        run = self.ctx.pipeline.run(self.tid, "Order completion rate by month")
        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertEqual(run.answer_mode, AnswerMode.DIRECT_FROM_APPROVED_KNOWLEDGE)
        self.assertGreater(run.row_count, 0)
        self.assertTrue(run.facts, "expected facts")
        self.assertEqual(run.source_node_ids[0][:3], "kn_")

    def test_novel_analysis_requires_review(self):
        run = self.ctx.pipeline.run(self.tid, "avg revenue per region",
                                     persisted_sql=NOVEL_SQL)
        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertIn(run.answer_mode,
                      (AnswerMode.NEW_LOW_RISK_ANALYSIS, AnswerMode.REQUIRES_SENIOR_REVIEW))
        self.assertEqual(run.row_count, 3)

    def test_dml_blocked_by_policy(self):
        run = self.ctx.pipeline.run(self.tid, "delete stuff", persisted_sql="DELETE FROM events WHERE 1=1")
        self.assertEqual(run.status, RunStatus.POLICY_REJECTED)
        self.assertTrue(any("read-only" in r for r in run.policy_reasons))

    def test_promote_finding_approves(self):
        run = self.ctx.pipeline.run(self.tid, "avg revenue per region", persisted_sql=NOVEL_SQL)
        node = self.ctx.pipeline.promote_finding(self.tid, run.id, by="senior")
        self.assertIsNotNone(node)
        self.assertEqual(node.status, ReviewStatus.APPROVED)
        self.assertEqual(node.kind, NodeKind.FINDING)

    def test_telemetry_recorded(self):
        self.ctx.pipeline.run(self.tid, "Order completion rate by month")
        m = self.ctx.obs.metrics(self.tid)
        self.assertGreater(m["total_spans"], 0)
        self.assertIn("planning", [s["stage"] for s in m["by_stage"]])
        self.assertIn("query.execution_completed", [s["stage"] for s in m["by_stage"]])


if __name__ == "__main__":
    unittest.main()