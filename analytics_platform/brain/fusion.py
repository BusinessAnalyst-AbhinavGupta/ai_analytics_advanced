"""Combining the two recall legs into one ranking.

bm25 scores are unbounded and negative; cosine similarities are 0..1. They cannot
be added. Reciprocal Rank Fusion combines by *position* instead, which needs no
score normalisation and no per-corpus tuning — the standard, boring choice.

Confidence then acts as a tie-breaker, not a driver: a reviewed, fresh node should
win against a stale one of equal relevance, but should never outrank a clearly
better match.
"""
from __future__ import annotations

from typing import Dict, List, Sequence

# The conventional RRF constant. Larger k flattens the contribution of top ranks,
# which keeps a single confident leg from dominating the fusion.
DEFAULT_K = 60

# Dimensions that describe how much a node can be trusted *now*. `evidence`,
# `definition`, `reproducibility` and `source` describe how it was produced and
# are deliberately excluded from ranking.
_RANKING_DIMENSIONS = ("review", "freshness")


def rrf_fuse(rankings: Sequence[Sequence[str]], k: int = DEFAULT_K) -> Dict[str, float]:
    """Fuse ranked id lists into {node_id: score}. Higher is better."""
    scores: Dict[str, float] = {}
    for ranking in rankings:
        for position, node_id in enumerate(ranking, start=1):
            scores[node_id] = scores.get(node_id, 0.0) + 1.0 / (k + position)
    return scores


def confidence_boost(confidence: Dict[str, float], weight: float = 0.3) -> float:
    """Multiplier in [1.0, 1.0 + weight] from the ranking-relevant dimensions."""
    if not confidence:
        return 1.0
    values = [min(1.0, max(0.0, float(confidence.get(d, 0.0) or 0.0)))
              for d in _RANKING_DIMENSIONS]
    return 1.0 + weight * (sum(values) / len(values))


def rank_nodes(fused: Dict[str, float],
               confidence_by_id: Dict[str, Dict[str, float]],
               weight: float = 0.3) -> List[str]:
    """Final ordering: fused relevance first, confidence only breaks ties.

    RRF's own dynamic range and the confidence multiplier's range overlap, so
    multiplying them together (the previous implementation) let confidence
    outrank a clearly more relevant match — exactly the outcome this module's
    docstring says must never happen. Confidence must instead be a true
    secondary sort key: it can only decide order between nodes whose fused
    relevance score is already equal.
    """
    def confidence(node_id: str) -> float:
        return confidence_boost(confidence_by_id.get(node_id, {}), weight)

    # Secondary sort on id keeps the order deterministic for equal scores.
    return sorted(fused, key=lambda n: (-fused[n], -confidence(n), n))
