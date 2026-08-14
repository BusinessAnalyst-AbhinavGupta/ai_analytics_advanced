"""Hybrid retrieval index: schema, lexical leg, vector leg, tenant isolation."""
from __future__ import annotations

import unittest
from unittest import mock

from analytics_platform.brain.embedding import NullEmbedder
from analytics_platform.brain.index import BrainIndex
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


class LexicalSearchTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.index = BrainIndex(self.ctx.store, embedder=NullEmbedder("test"))
        self.index.upsert("kn_1", "t1", "Checkout conversion rate",
                          "Share of sessions that reach the payment page")
        self.index.upsert("kn_2", "t1", "BC2D attach rate",
                          "Bundled care attach on disbursed loans")
        self.index.upsert("kn_3", "t2", "Checkout conversion rate",
                          "Other tenant, same title")

    def tearDown(self):
        self.ctx.close()

    def test_finds_node_by_content_word(self):
        self.assertEqual(
            self.index.lexical_search("conversion", "t1", None, 10), ["kn_1"])

    def test_finds_node_from_a_full_natural_language_question(self):
        # The defect this replaces: LIKE '%<whole question>%' matched nothing.
        hits = self.index.lexical_search(
            "why did our checkout conversion drop last week?", "t1", None, 10)
        self.assertIn("kn_1", hits)

    def test_matches_internal_acronyms(self):
        self.assertEqual(self.index.lexical_search("BC2D", "t1", None, 10), ["kn_2"])

    def test_other_tenants_are_never_returned(self):
        self.assertNotIn("kn_3", self.index.lexical_search("conversion", "t1", None, 10))

    def test_candidate_ids_restrict_results(self):
        self.assertEqual(
            self.index.lexical_search("conversion", "t1", ["kn_2"], 10), [])

    def test_empty_candidate_list_returns_nothing(self):
        self.assertEqual(self.index.lexical_search("conversion", "t1", [], 10), [])

    def test_unmatchable_query_returns_empty_not_error(self):
        self.assertEqual(self.index.lexical_search("what is the", "t1", None, 10), [])

    def test_punctuation_heavy_query_does_not_raise(self):
        self.assertEqual(self.index.lexical_search('"; DROP TABLE --', "t1", None, 10), [])

    def test_upsert_replaces_rather_than_duplicates(self):
        self.index.upsert("kn_1", "t1", "Checkout conversion rate", "Updated summary")
        rows = self.ctx.store.query_all(
            "SELECT node_id FROM knowledge_fts WHERE node_id = ?", ("kn_1",))
        self.assertEqual(len(rows), 1)

    def test_delete_removes_from_index(self):
        self.index.delete("kn_1")
        self.assertEqual(self.index.lexical_search("conversion", "t1", None, 10), [])

    def test_ranking_puts_the_better_match_first(self):
        self.index.upsert("kn_4", "t1", "Conversion", "conversion conversion conversion")
        hits = self.index.lexical_search("conversion", "t1", None, 10)
        self.assertEqual(hits[0], "kn_4")


class ChunkedLexicalSearchTest(unittest.TestCase):
    """Regression for candidate sets larger than SQLite's ~900-param limit.

    Patches `_MAX_SQL_PARAMS` down to 2 so a 5-id candidate set forces
    `lexical_search` to issue 3 chunked MATCH queries and merge the results,
    instead of dropping the SQL restriction and silently ranking the whole
    tenant (the bug being fixed here).
    """

    def setUp(self):
        self.ctx = make_ctx()
        self.index = BrainIndex(self.ctx.store, embedder=NullEmbedder("test"))
        # Increasing term frequency of "conversion" produces a strict, known
        # bm25 ranking: kn_5 best match, kn_1 worst.
        for i in range(1, 6):
            self.index.upsert(
                f"kn_{i}", "t1", f"Node {i}", " ".join(["conversion"] * i))
        # A node outside the candidate set that would win tenant-wide ranking
        # if the restriction were ever dropped instead of chunked.
        self.index.upsert(
            "kn_decoy", "t1", "Decoy", " ".join(["conversion"] * 50))

    def tearDown(self):
        self.ctx.close()

    def test_chunking_matches_unchunked_result_exactly(self):
        candidate_ids = ["kn_1", "kn_2", "kn_3", "kn_4", "kn_5"]

        # Ground truth: default _MAX_SQL_PARAMS (900) means all 5 ids fit in
        # one query, so this is the real, unchunked answer.
        expected = self.index.lexical_search("conversion", "t1", candidate_ids, 3)
        self.assertEqual(expected, ["kn_5", "kn_4", "kn_3"])
        self.assertNotIn("kn_decoy", expected)

        # Force 3 chunks of at most 2 ids each: [kn_1,kn_2], [kn_3,kn_4], [kn_5].
        with mock.patch("analytics_platform.brain.index._MAX_SQL_PARAMS", 2):
            chunked = self.index.lexical_search("conversion", "t1", candidate_ids, 3)

        self.assertEqual(chunked, expected)
        self.assertNotIn("kn_decoy", chunked)

    def test_chunking_respects_full_candidate_set_not_just_first_chunk(self):
        # All 5 candidates, but ask for everything back (limit=10) so a
        # regression that only queried the first chunk would be caught by a
        # shorter, wrong result rather than just a reordering.
        candidate_ids = ["kn_1", "kn_2", "kn_3", "kn_4", "kn_5"]
        with mock.patch("analytics_platform.brain.index._MAX_SQL_PARAMS", 2):
            chunked = self.index.lexical_search("conversion", "t1", candidate_ids, 10)
        self.assertEqual(chunked, ["kn_5", "kn_4", "kn_3", "kn_2", "kn_1"])

    def test_candidate_ids_none_is_unaffected_by_chunking(self):
        # candidate_ids=None must always take the single-chunk path,
        # regardless of how small _MAX_SQL_PARAMS is patched to.
        with mock.patch("analytics_platform.brain.index._MAX_SQL_PARAMS", 2):
            hits = self.index.lexical_search("conversion", "t1", None, 10)
        self.assertEqual(hits[0], "kn_decoy")
        self.assertIn("kn_5", hits)
        self.assertIn("kn_1", hits)


if __name__ == "__main__":
    unittest.main()
