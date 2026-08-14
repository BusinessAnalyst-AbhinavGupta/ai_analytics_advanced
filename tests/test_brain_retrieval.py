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

    def test_search_without_an_index_still_works(self):
        """No index injected -> lexical-free fallback must not raise."""
        bare = CompanyBrain(self.ctx.store, "t1")
        self.assertIsInstance(bare.search("conversion", kind=NodeKind.QUERY), list)


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


if __name__ == "__main__":
    unittest.main()
