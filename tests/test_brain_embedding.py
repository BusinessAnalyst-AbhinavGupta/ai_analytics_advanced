"""Embedding provider: configurable model, normalised output, loud null fallback."""
from __future__ import annotations

import logging
import unittest

import numpy as np

from analytics_platform.brain.embedding import (NullEmbedder,
                                                SentenceTransformerEmbedder,
                                                get_embedder)
from analytics_platform.config import Settings


class NullEmbedderTest(unittest.TestCase):
    def test_reports_unavailable(self):
        self.assertFalse(NullEmbedder("disabled by config").available)

    def test_encode_returns_none(self):
        emb = NullEmbedder("disabled by config")
        self.assertIsNone(emb.encode_documents(["anything"]))
        self.assertIsNone(emb.encode_query("anything"))

    def test_dim_is_zero(self):
        self.assertEqual(NullEmbedder("disabled by config").dim, 0)


class FactoryTest(unittest.TestCase):
    def test_disabled_setting_yields_null_embedder(self):
        emb = get_embedder(Settings(embedding_enabled=False))
        self.assertIsInstance(emb, NullEmbedder)
        self.assertFalse(emb.available)

    def test_unloadable_model_degrades_to_null_and_logs(self):
        settings = Settings(embedding_model="definitely/not-a-real-model-xyz")
        with self.assertLogs("analytics_platform.brain.embedding", level=logging.WARNING) as cap:
            emb = get_embedder(settings)
        self.assertFalse(emb.available)
        self.assertTrue(any("definitely/not-a-real-model-xyz" in m for m in cap.output))

    def test_repeated_calls_are_cached(self):
        s = Settings(embedding_enabled=False)
        self.assertIs(get_embedder(s), get_embedder(s))


class SentenceTransformerEmbedderTest(unittest.TestCase):
    """Loads a real model. Skipped when the model is not cached locally."""

    @classmethod
    def setUpClass(cls):
        cls.emb = SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5")
        if not cls.emb.available:
            raise unittest.SkipTest("bge-small-en-v1.5 not available offline")

    def test_document_vectors_are_normalised_float32(self):
        vecs = self.emb.encode_documents(["checkout conversion rate"])
        self.assertEqual(vecs.dtype, np.float32)
        self.assertAlmostEqual(float(np.linalg.norm(vecs[0])), 1.0, places=4)

    def test_query_vector_shape_matches_dim(self):
        vec = self.emb.encode_query("how many people converted")
        self.assertEqual(vec.shape, (self.emb.dim,))

    def test_semantics_beat_keywords(self):
        # The unrelated doc must be topically unambiguous. An earlier draft used a
        # "server latency" doc, which scored within 0.0015 of the correct answer —
        # "regression" apparently reads close to "churn regression model" to this
        # model, a near coin-flip margin, not a robust semantic-match assertion.
        docs = self.emb.encode_documents([
            "High user churn observed in Q3 for the European market.",
            "The design team shipped a refreshed color palette for the mobile app icon.",
        ])
        q = self.emb.encode_query("customer attrition")
        sims = docs @ q
        self.assertGreater(float(sims[0]), float(sims[1]))


if __name__ == "__main__":
    unittest.main()
