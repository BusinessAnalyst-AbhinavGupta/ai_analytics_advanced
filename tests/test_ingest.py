"""Legacy-query ingestion (sqlglot AST extraction) tests."""
from __future__ import annotations

import unittest

from analytics_platform.brain.ingest import column_business_definitions, extract, ingest_sql
from analytics_platform.domain import NodeKind, ReviewStatus
from tests.helpers import make_ctx

SAMPLE_SQL = """
SELECT date_format(CAST(created_at AS TIMESTAMP), '%Y-%m') AS month,
       COUNT_IF(action = 'apptCompleted') AS completed
  FROM events
 WHERE action = 'apptBooked'
   AND status = 'PENDING'
 GROUP BY 1
"""


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.tid = self.ctx.tenants.create_tenant("Acme").id
        self.brain = self.ctx.pipeline.brain(self.tid)

    def tearDown(self):
        self.ctx.close()

    def test_extract_structure(self):
        info = extract(SAMPLE_SQL)
        self.assertIn("events", info["tables"])
        self.assertTrue(info["read_only"])
        self.assertTrue(info["aggregate"])
        # action appears both as a WHERE filter and inside COUNT_IF
        self.assertIn("apptBooked", info["filters"].get("action"))
        self.assertIn("apptCompleted", info["filters"].get("action"))
        self.assertIn("status", info["filters"])

    def test_is_read_only_flags_dml(self):
        from analytics_platform.brain.ingest import is_read_only
        self.assertTrue(is_read_only("SELECT * FROM t"))
        self.assertFalse(is_read_only("DELETE FROM t"))
        self.assertFalse(is_read_only("UPDATE t SET a=1"))

    def test_ingest_creates_candidate_query_and_definitions(self):
        nodes = ingest_sql(self.brain, SAMPLE_SQL, source_ref="legacy_card_123")
        kinds = {n.kind for n in nodes}
        self.assertTrue(NodeKind.QUERY in kinds)
        self.assertTrue(NodeKind.DEFINITION in kinds)
        # imported queries are never auto-approved
        self.assertTrue(all(n.status == ReviewStatus.CANDIDATE for n in nodes))

    def test_column_business_definitions_only_from_approved(self):
        nodes = ingest_sql(self.brain, SAMPLE_SQL, source_ref="legacy_card")
        # approve the definition nodes
        for n in nodes:
            if n.kind == NodeKind.DEFINITION:
                self.brain.submit(n.id)
                self.brain.approve(n.id, by="senior")
        defs = column_business_definitions(self.brain)
        self.assertIn("action", defs)
        self.assertIn("apptBooked", defs["action"])


if __name__ == "__main__":
    unittest.main()