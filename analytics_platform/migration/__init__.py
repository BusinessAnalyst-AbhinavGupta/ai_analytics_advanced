"""Company Brain v2 migration.

Ports the prototype's knowledge-graph snapshot
(`extracted_data/knowledge_graph_snapshot.json`) into governed `KnowledgeNode`s
in the platform's Company Brain.

- `mapper.py` — pure/deterministic: snapshot dict -> list of `NodeSpec` drafts.
- `loader.py` — persists drafts as CANDIDATE nodes, idempotently (via `source_ref`),
  with provenance + confidence; `migrate_from_snapshot` / `migrate_specs`.
"""
from __future__ import annotations

from .loader import migrate_from_snapshot, migrate_specs
from .mapper import NodeSpec, load_snapshot, plan_from_snapshot

__all__ = [
    "NodeSpec",
    "load_snapshot",
    "migrate_from_snapshot",
    "migrate_specs",
    "plan_from_snapshot",
]