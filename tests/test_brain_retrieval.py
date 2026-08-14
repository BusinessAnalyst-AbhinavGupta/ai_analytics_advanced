"""End-to-end Brain retrieval: the behaviour that was broken before the rebuild."""
from __future__ import annotations

import unittest

from analytics_platform.brain.embedding import NullEmbedder
from analytics_platform.brain.index import BrainIndex
from analytics_platform.brain.store import CompanyBrain
from analytics_platform.domain import NodeKind, ReviewStatus
from tests.helpers import make_ctx


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.index = BrainIndex(self.ctx.store, embedder=NullEmbedder("test"))
        self.brain = CompanyBrain(self.ctx.store, "t1", index=self.index)
        self.other = CompanyBrain(self.ctx.store, "t2", index=self.index)

        self.approved = self._approved(
            self.brain, NodeKind.QUERY, "Checkout conversion rate",
            "Share of sessions that reach the payment page")
        self.definition = self._approved(
            self.brain, NodeKind.DEFINITION, "Conversion",
            "A session that reaches the payment page")
        self.candidate = self.brain.create(
            NodeKind.QUERY, "Checkout conversion by city",
            summary="Conversion split by city")   # stays CANDIDATE
        self.foreign = self._approved(
            self.other, NodeKind.QUERY, "Checkout conversion rate",
            "Another tenant's node")

    def tearDown(self):
        self.ctx.close()

    @staticmethod
    def _approved(brain, kind, title, summary):
        node = brain.create(kind, title, summary=summary)
        brain.submit(node.id, by="junior")
        return brain.approve(node.id, by="senior")

    def test_natural_language_question_finds_the_node(self):
        """The core regression: this returned [] before the rebuild."""
        hits = self.brain.search("why did our checkout conversion drop last week?",
                                 kind=NodeKind.QUERY)
        self.assertIn(self.approved.id, [n.id for n in hits])

    def test_unapproved_nodes_are_never_returned(self):
        hits = self.brain.search("checkout conversion by city", kind=NodeKind.QUERY)
        self.assertNotIn(self.candidate.id, [n.id for n in hits])

    def test_other_tenants_are_never_returned(self):
        hits = self.brain.search("checkout conversion rate", kind=NodeKind.QUERY)
        self.assertNotIn(self.foreign.id, [n.id for n in hits])

    def test_kind_filter_is_honoured(self):
        hits = self.brain.search("conversion", kind=NodeKind.DEFINITION)
        ids = [n.id for n in hits]
        self.assertIn(self.definition.id, ids)
        self.assertNotIn(self.approved.id, ids)

    def test_usable_only_false_includes_candidates(self):
        hits = self.brain.search("checkout conversion by city", kind=NodeKind.QUERY,
                                 usable_only=False)
        self.assertIn(self.candidate.id, [n.id for n in hits])

    def test_empty_query_returns_recent_nodes(self):
        hits = self.brain.search("", kind=NodeKind.QUERY)
        self.assertIn(self.approved.id, [n.id for n in hits])

    def test_unmatchable_query_returns_empty(self):
        self.assertEqual(self.brain.search("zzzz-nonexistent-token",
                                           kind=NodeKind.QUERY), [])

    def test_limit_is_respected(self):
        self.assertLessEqual(len(self.brain.search("conversion", limit=1)), 1)

    def test_results_are_knowledge_nodes(self):
        hits = self.brain.search("conversion", kind=NodeKind.QUERY)
        self.assertTrue(all(hasattr(n, "id") and hasattr(n, "title") for n in hits))

    def test_search_without_an_index_returns_nothing_for_a_real_query(self):
        """No index -> [] for a query, never unrelated nodes presented as matches.

        `self.approved` genuinely exists in this tenant's table and would match —
        proving this isn't just "empty database, nothing to find." An indexless
        brain that returned it (or any other recent node) here would be answering
        a real question with unrelated content, which is worse than the original
        bug this plan fixes (that one at least returned nothing).
        """
        bare = CompanyBrain(self.ctx.store, "t1")
        self.assertEqual(bare.search("checkout conversion rate", kind=NodeKind.QUERY), [])

    def test_search_without_an_index_and_no_query_still_browses_recent_nodes(self):
        """No query is a browsing request, not a relevance claim -- unaffected."""
        bare = CompanyBrain(self.ctx.store, "t1")
        hits = bare.search("", kind=NodeKind.QUERY)
        self.assertIn(self.approved.id, [n.id for n in hits])


class IndexSyncTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.index = BrainIndex(self.ctx.store, embedder=NullEmbedder("test"))
        self.brain = CompanyBrain(self.ctx.store, "t1", index=self.index)

    def tearDown(self):
        self.ctx.close()

    def test_create_indexes_the_node(self):
        node = self.brain.create(NodeKind.METRIC, "Gross margin",
                                 summary="Revenue minus cost of goods")
        rows = self.ctx.store.query_all(
            "SELECT node_id FROM knowledge_fts WHERE node_id = ?", (node.id,))
        self.assertEqual(len(rows), 1)

    def test_transition_reindexes_without_duplicating(self):
        node = self.brain.create(NodeKind.METRIC, "Gross margin", summary="x")
        self.brain.submit(node.id, by="junior")
        self.brain.approve(node.id, by="senior")
        rows = self.ctx.store.query_all(
            "SELECT node_id FROM knowledge_fts WHERE node_id = ?", (node.id,))
        self.assertEqual(len(rows), 1)


class ContextWiringTest(unittest.TestCase):
    """Regression guard for the defect that made the Brain look empty.

    Every service that reads the Brain must receive an index. This test fails if
    any of them is constructed without one.
    """

    def setUp(self):
        import tempfile
        from analytics_platform.api import make_context
        from analytics_platform.config import Settings
        self._tmp = tempfile.TemporaryDirectory()
        self.ctx = make_context(Settings(data_dir=self._tmp.name,
                                         embedding_enabled=False))
        self.ctx.tenants.create("t1", name="T1")

    def tearDown(self):
        self.ctx.stores.close_all()
        self._tmp.cleanup()

    def test_every_brain_reader_has_an_index(self):
        for name in ("stakeholder", "junior", "onboarding", "research", "triage"):
            service = getattr(self.ctx, name, None)
            if service is None:
                continue
            brain = service.brain("t1")
            self.assertIsNotNone(
                brain.index, f"{name}.brain() has no BrainIndex — searches "
                             f"will silently return recency order")

    def test_each_brain_index_uses_its_own_tenants_store(self):
        """An index bound to the wrong store would read another company's data."""
        self.ctx.tenants.create("t2", name="T2")
        a = self.ctx.stakeholder.brain("t1").index
        b = self.ctx.stakeholder.brain("t2").index
        self.assertNotEqual(a.store.db_path, b.store.db_path)

    def test_the_embedder_is_shared_across_tenants(self):
        """Loading a model per tenant would cost seconds and hundreds of MB each."""
        self.ctx.tenants.create("t2", name="T2")
        self.assertIs(self.ctx.stakeholder.brain("t1").index.embedder,
                      self.ctx.stakeholder.brain("t2").index.embedder)

    def test_stakeholder_retrieves_an_approved_query_from_a_paraphrase(self):
        brain = self.ctx.stakeholder.brain("t1")
        node = brain.create(NodeKind.QUERY, "Checkout conversion rate",
                            payload={"sql": "SELECT 1", "dialect": "duckdb"},
                            summary="Share of sessions reaching payment")
        brain.submit(node.id, by="junior")
        brain.approve(node.id, by="senior")

        q, d = self.ctx.stakeholder._retrieve(
            "t1", "how is our checkout conversion doing?")
        self.assertIn(node.id, [n.id for n in q])


if __name__ == "__main__":
    unittest.main()
