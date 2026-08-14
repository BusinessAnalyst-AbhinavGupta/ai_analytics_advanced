"""Reciprocal Rank Fusion and confidence re-ranking."""
from __future__ import annotations

import unittest

from analytics_platform.brain.fusion import (confidence_boost, rank_nodes,
                                             rrf_fuse)


class RrfFuseTest(unittest.TestCase):
    def test_single_ranking_preserves_order(self):
        fused = rrf_fuse([["a", "b", "c"]])
        self.assertEqual(sorted(fused, key=lambda i: -fused[i]), ["a", "b", "c"])

    def test_agreement_between_legs_wins(self):
        # "b" is 2nd in both legs; "a" and "c" are 1st in one and absent from the other.
        fused = rrf_fuse([["a", "b"], ["c", "b"]])
        self.assertEqual(max(fused, key=lambda i: fused[i]), "b")

    def test_score_matches_the_rrf_formula(self):
        fused = rrf_fuse([["a"]], k=60)
        self.assertAlmostEqual(fused["a"], 1.0 / 61.0)

    def test_ids_from_either_leg_are_included(self):
        self.assertEqual(set(rrf_fuse([["a"], ["b"]])), {"a", "b"})

    def test_empty_input_yields_empty(self):
        self.assertEqual(rrf_fuse([]), {})

    def test_empty_rankings_are_skipped(self):
        self.assertEqual(rrf_fuse([[], ["a"]]), {"a": 1.0 / 61.0})


class ConfidenceBoostTest(unittest.TestCase):
    def test_full_confidence_gives_the_maximum_boost(self):
        self.assertAlmostEqual(
            confidence_boost({"review": 1.0, "freshness": 1.0}, weight=0.3), 1.3)

    def test_zero_confidence_is_neutral_not_zero(self):
        self.assertAlmostEqual(
            confidence_boost({"review": 0.0, "freshness": 0.0}, weight=0.3), 1.0)

    def test_missing_dimensions_are_treated_as_zero(self):
        self.assertAlmostEqual(confidence_boost({}, weight=0.3), 1.0)

    def test_out_of_range_values_are_clamped(self):
        self.assertAlmostEqual(
            confidence_boost({"review": 5.0, "freshness": 5.0}, weight=0.3), 1.3)


class RankNodesTest(unittest.TestCase):
    def test_confidence_breaks_a_relevance_tie(self):
        fused = {"a": 0.5, "b": 0.5}
        conf = {"a": {"review": 0.0, "freshness": 0.0},
                "b": {"review": 1.0, "freshness": 1.0}}
        self.assertEqual(rank_nodes(fused, conf), ["b", "a"])

    def test_relevance_still_dominates_confidence(self):
        fused = {"a": 1.0, "b": 0.5}
        conf = {"a": {"review": 0.0, "freshness": 0.0},
                "b": {"review": 1.0, "freshness": 1.0}}
        self.assertEqual(rank_nodes(fused, conf), ["a", "b"])

    def test_nodes_without_confidence_still_rank(self):
        self.assertEqual(rank_nodes({"a": 1.0, "b": 0.5}, {}), ["a", "b"])

    def test_empty_input_yields_empty(self):
        self.assertEqual(rank_nodes({}, {}), [])


if __name__ == "__main__":
    unittest.main()
