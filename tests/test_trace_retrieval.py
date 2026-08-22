"""Retrieval, made legible.

The failure this exists to expose: a 2-4 word rewrite is the sole key for both
searches, and when it surfaces the wrong nodes nothing records why. Putting the
query string next to the ids each leg returned is the whole point.
"""
from __future__ import annotations

import json
import tempfile
import unittest

from analytics_platform import tracing
from analytics_platform.brain.index import BrainIndex
from analytics_platform.brain.store import CompanyBrain
from analytics_platform.database import TENANT_SCHEMA, Store
from analytics_platform.domain import NodeKind, ReviewStatus


class RetrievalTraceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(f"{self._tmp.name}/t.db", schema=TENANT_SCHEMA)
        self.brain = CompanyBrain(self.store, "tnt_x", index=BrainIndex(self.store))
        node = self.brain.create(NodeKind.DEFINITION, title="Checkout consent page",
                                 summary="the consent step of checkout",
                                 status=ReviewStatus.APPROVED)
        self.node_id = node.id
        self.sink = tracing.TraceSink(self.store, "tnt_x", "trace-1")
        tracing.use_sink(self.sink)

    def tearDown(self):
        tracing.clear_turn()
        self._tmp.cleanup()

    def retrieval_payloads(self):
        return [json.loads(r["payload"]) for r in self.store.query_all(
            "SELECT payload FROM llm_traces WHERE kind='retrieval' ORDER BY seq")]

    def test_a_search_records_the_query_and_what_came_back(self):
        self.brain.search("consent", kind=NodeKind.DEFINITION, limit=3)
        p = self.retrieval_payloads()[0]
        self.assertEqual(p["query"], "consent")
        self.assertEqual(p["node_kind"], "DEFINITION")
        self.assertEqual(p["limit"], 3)
        self.assertIn(self.node_id, p["lexical_ids"])
        self.assertIn(self.node_id, p["returned_ids"])

    def test_a_search_records_the_candidate_count(self):
        self.brain.search("consent", kind=NodeKind.DEFINITION, limit=3)
        self.assertEqual(self.retrieval_payloads()[0]["candidate_count"], 1)
        self.assertFalse(self.retrieval_payloads()[0]["candidate_cap_hit"])

    def test_embeddings_off_is_recorded_not_hidden(self):
        self.brain.search("consent", kind=NodeKind.DEFINITION, limit=3)
        p = self.retrieval_payloads()[0]
        self.assertFalse(p["embedding_available"])
        self.assertEqual(p["dense_ids"], [])

    def test_a_search_that_finds_nothing_still_records(self):
        self.brain.search("zzzznotathing", kind=NodeKind.DEFINITION, limit=3)
        p = self.retrieval_payloads()[0]
        self.assertEqual(p["returned_ids"], [])
        self.assertEqual(p["query"], "zzzznotathing")

    def test_browsing_mode_is_not_traced_as_a_relevance_claim(self):
        self.brain.search("", kind=NodeKind.DEFINITION, limit=3)
        self.assertEqual(self.retrieval_payloads(), [])

    def test_search_still_returns_its_results(self):
        got = self.brain.search("consent", kind=NodeKind.DEFINITION, limit=3)
        self.assertEqual([n.id for n in got], [self.node_id])
