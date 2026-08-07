"""Junior maturity-stage engine tests (offline, retail warehouse)."""
from __future__ import annotations

import unittest

from analytics_platform.domain import NodeKind, ReviewStatus
from analytics_platform.fixtures import WEEKLY_ORDER_SQL, build_retail_warehouse
from analytics_platform.junior import JuniorEngine
from tests.helpers import make_ctx


def approve_query(brain, sql, title="approved metric"):
    n = brain.create(NodeKind.QUERY, title, payload={"sql": sql, "dialect": "athena"})
    brain.submit(n.id, by="senior")
    brain.approve(n.id, by="senior")
    return n


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


if __name__ == "__main__":
    unittest.main()