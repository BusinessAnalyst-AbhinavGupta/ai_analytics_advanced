"""Company Brain v2 migration.

Ports the prototype's knowledge-graph snapshot
(`extracted_data/knowledge_graph_snapshot.json`) into governed `KnowledgeNode`s
in the platform's Company Brain.

- `mapper.py` — pure/deterministic: snapshot dict -> list of `NodeSpec` drafts.

(CP1) The `loader` (persist drafts as CANDIDATE nodes, idempotently) lands next.
"""
from __future__ import annotations

from .mapper import NodeSpec, load_snapshot, plan_from_snapshot

__all__ = [
    "NodeSpec",
    "load_snapshot",
    "plan_from_snapshot",
]