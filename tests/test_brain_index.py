"""Hybrid retrieval index: schema, lexical leg, vector leg, tenant isolation."""
from __future__ import annotations

import unittest

from tests.helpers import make_ctx


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()

    def tearDown(self):
        self.ctx.close()

    def _tables(self):
        rows = self.ctx.store.query_all(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")
        return {r["name"] for r in rows}

    def test_fts_table_exists(self):
        self.assertIn("knowledge_fts", self._tables())

    def test_vectors_table_exists(self):
        self.assertIn("knowledge_vectors", self._tables())

    def test_vectors_table_columns(self):
        rows = self.ctx.store.query_all("PRAGMA table_info(knowledge_vectors)")
        self.assertEqual(
            {r["name"] for r in rows},
            {"node_id", "tenant_id", "model", "dim", "vector", "updated_at"})

    def test_fts_accepts_match_query(self):
        self.ctx.store.execute(
            "INSERT INTO knowledge_fts (node_id, tenant_id, title, summary) VALUES (?,?,?,?)",
            ("kn_1", "t1", "Checkout conversion", "Share of sessions reaching payment"))
        rows = self.ctx.store.query_all(
            "SELECT node_id FROM knowledge_fts WHERE knowledge_fts MATCH ? AND tenant_id = ?",
            ('"conversion"', "t1"))
        self.assertEqual([r["node_id"] for r in rows], ["kn_1"])


if __name__ == "__main__":
    unittest.main()
