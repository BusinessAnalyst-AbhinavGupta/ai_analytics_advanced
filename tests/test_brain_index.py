"""Hybrid retrieval index: schema, lexical leg, vector leg, tenant isolation."""
from __future__ import annotations

import unittest
from unittest import mock

from analytics_platform.brain.embedding import NullEmbedder, SentenceTransformerEmbedder
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


class VectorSearchTest(unittest.TestCase):
    """Replaces the ChromaDB test that used to live in tests/test_vector_search.py."""

    @classmethod
    def setUpClass(cls):
        cls.embedder = SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5")
        if not cls.embedder.available:
            raise unittest.SkipTest("bge-small-en-v1.5 not available offline")

    def setUp(self):
        self.ctx = make_ctx()
        self.index = BrainIndex(self.ctx.store, embedder=self.embedder)
        self.index.upsert("kn_churn", "t1", "Q3 European churn",
                          "High user churn observed in Q3 for the European market.")
        # The unrelated doc must be topically unambiguous once title+summary are
        # embedded together (what upsert() actually does). A "server latency" doc
        # titled "Latency regression" was tried first and lost — "regression" reads
        # close enough to "churn regression model" that it out-scored the genuinely
        # on-topic doc for the query "customer attrition" (0.650 vs 0.617). This
        # pairing has a wide, verified margin (~0.19) instead of a coin flip.
        self.index.upsert("kn_palette", "t1", "New color palette",
                          "The design team shipped a refreshed color palette for the mobile app icon.")
        self.index.upsert("kn_other", "t2", "Q3 European churn",
                          "High user churn observed in Q3 for the European market.")

    def tearDown(self):
        self.ctx.close()

    def test_matches_on_meaning_not_keywords(self):
        # "customer attrition" shares no token with "user churn".
        hits = self.index.vector_search("customer attrition", "t1", None, 5)
        self.assertEqual(hits[0], "kn_churn")

    def test_lexical_leg_cannot_do_this(self):
        # Demonstrates why both legs exist.
        self.assertEqual(self.index.lexical_search("customer attrition", "t1", None, 5), [])

    def test_other_tenants_are_never_returned(self):
        hits = self.index.vector_search("customer attrition", "t1", None, 5)
        self.assertNotIn("kn_other", hits)

    def test_candidate_ids_restrict_results(self):
        hits = self.index.vector_search("customer attrition", "t1", ["kn_palette"], 5)
        self.assertEqual(hits, ["kn_palette"])

    def test_empty_candidate_list_returns_nothing(self):
        self.assertEqual(self.index.vector_search("customer attrition", "t1", [], 5), [])

    def test_delete_removes_the_vector(self):
        self.index.delete("kn_churn")
        hits = self.index.vector_search("customer attrition", "t1", None, 5)
        self.assertNotIn("kn_churn", hits)


class VectorSearchDegradationTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.index = BrainIndex(self.ctx.store, embedder=NullEmbedder("test"))

    def tearDown(self):
        self.ctx.close()

    def test_returns_empty_when_embeddings_unavailable(self):
        self.index.upsert("kn_1", "t1", "Churn", "User churn in Q3")
        self.assertEqual(self.index.vector_search("attrition", "t1", None, 5), [])

    def test_no_vector_row_is_written_without_an_embedder(self):
        self.index.upsert("kn_1", "t1", "Churn", "User churn in Q3")
        rows = self.ctx.store.query_all("SELECT node_id FROM knowledge_vectors")
        self.assertEqual(rows, [])

    def test_embedding_available_is_false(self):
        self.assertFalse(self.index.embedding_available)


class _FakeAvailableEmbedder:
    """An embedder that reports `available=True` but never actually wrote any
    vectors under this model name -- the "wrong model / never backfilled"
    scenario from the whole-branch review."""

    available = True
    model_name = "some/other-model"
    dim = 3

    def encode_documents(self, texts):
        return None

    def encode_query(self, text):
        return [0.1, 0.2, 0.3]


class VectorSearchMissingModelTest(unittest.TestCase):
    """Regression: dense leg returning [] because no rows match `model` must
    log a WARNING, not fail silently -- "degrade loudly" (see index.py
    `_load_vectors`)."""

    def setUp(self):
        self.ctx = make_ctx()
        self.index = BrainIndex(self.ctx.store, embedder=_FakeAvailableEmbedder())

    def tearDown(self):
        self.ctx.close()

    def test_warns_when_embeddings_available_but_no_vectors_for_this_model(self):
        with self.assertLogs("analytics_platform.brain.index", level="WARNING") as cm:
            hits = self.index.vector_search("attrition", "t1", ["kn_1"], 5)
        self.assertEqual(hits, [])
        self.assertTrue(
            any("t1" in msg and "some/other-model" in msg for msg in cm.output),
            cm.output)

    def test_no_warning_when_candidate_set_is_empty(self):
        # An empty candidate set means there was nothing to search in the
        # first place -- not a missing-backfill situation, so no warning.
        with self.assertRaises(AssertionError):
            with self.assertLogs("analytics_platform.brain.index", level="WARNING"):
                self.index.vector_search("attrition", "t1", [], 5)


class ReindexTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.index = BrainIndex(self.ctx.store, embedder=NullEmbedder("test"))
        # Nodes written before the index existed: rows with no FTS entries.
        for i, (title, summary) in enumerate([
                ("Checkout conversion", "Sessions reaching payment"),
                ("Refund rate", "Share of orders refunded")], start=1):
            self.ctx.store.execute(
                "INSERT INTO knowledge_nodes (id,tenant_id,kind,status,version,title,"
                "summary,payload,confidence,evidence_ref,source_ref,created_at,"
                "updated_at,created_by,reviewed_by,review_notes,supersedes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"kn_{i}", "t1", "METRIC", "APPROVED", 1, title, summary,
                 "{}", "{}", "", "", "2026-01-01", "2026-01-01", "seed", "", "", ""))

    def tearDown(self):
        self.ctx.close()

    def test_nodes_are_invisible_before_reindex(self):
        self.assertEqual(self.index.lexical_search("conversion", "t1", None, 10), [])

    def test_reindex_returns_the_node_count(self):
        self.assertEqual(self.index.reindex_tenant("t1"), 2)

    def test_nodes_are_searchable_after_reindex(self):
        self.index.reindex_tenant("t1")
        self.assertEqual(self.index.lexical_search("conversion", "t1", None, 10), ["kn_1"])

    def test_reindex_is_idempotent(self):
        self.index.reindex_tenant("t1")
        self.index.reindex_tenant("t1")
        rows = self.ctx.store.query_all(
            "SELECT node_id FROM knowledge_fts WHERE node_id = ?", ("kn_1",))
        self.assertEqual(len(rows), 1)

    def test_reindex_does_not_touch_other_tenants(self):
        self.assertEqual(self.index.reindex_tenant("t2"), 0)


if __name__ == "__main__":
    unittest.main()
