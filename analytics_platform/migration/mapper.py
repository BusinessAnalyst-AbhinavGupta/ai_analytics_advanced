"""Map the prototype knowledge-graph snapshot onto governed `NodeSpec` drafts.

This module is **pure and deterministic**: given a parsed snapshot dict it decides
*what* should become a knowledge node and with what provenance/confidence. No
database or file writes happen here — transport belongs to `loader.py`.

Invariants preserved:
- every spec defaults to `CANDIDATE` (approval is the hard gate elsewhere);
- `source_dialect="athena"` is kept verbatim on each query payload (no transpile);
- SQL is stored as-is (execution/transpile happens later at run time);
- reuses `brain.ingest.extract` (sqlglot AST) so derived business definitions are
  consistent with the legacy-ingestion path.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..brain.ingest import extract
from ..domain import NodeKind, ReviewStatus

SNAPSHOT_SOURCE = "knowledge_graph_snapshot.json"


@dataclass
class NodeSpec:
    """A draft describing how to create one knowledge node via `CompanyBrain.create`."""
    kind: NodeKind
    title: str
    summary: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    source_ref: str = ""
    evidence_ref: str = ""
    confidence: Optional[Dict[str, float]] = None
    created_by: str = "migration"
    status: ReviewStatus = ReviewStatus.CANDIDATE


def load_snapshot(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def node_key(section: str, ident: str) -> str:
    """Stable, unique source key (drives the loader's idempotency guard)."""
    return f"{SNAPSHOT_SOURCE}#{section}/{ident}"


def _conf_source() -> Dict[str, float]:
    """Confidence for a node that is directly sourced from the knowledge graph."""
    return {"evidence": 0.0, "review": 0.0, "definition": 1.0,
            "freshness": 0.8, "reproducibility": 0.0, "source": 1.0}


def _uid(*parts: Any) -> str:
    """Short content hash — a stable, collision-resistant ident for a record."""
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _map_golden_query(rec: Dict[str, Any], reasoning: Optional[str]) -> NodeSpec:
    card_id = str(rec.get("card_id", "")).strip()
    ident = card_id or _uid(rec.get("name"), rec.get("sql"))
    payload: Dict[str, Any] = {
        "sql": rec.get("sql", ""),
        "dialect": rec.get("dialect", "athena"),
        "table_name": rec.get("table_name", ""),
        "journey_stage": rec.get("journey_stage", ""),
        "business_goal": rec.get("intent", ""),
        "via": card_id,
    }
    if reasoning:
        payload["reasoning_summary"] = reasoning
    title = rec.get("name") or f"Query {card_id or ident}"
    summary = (f"Tables: {rec.get('table_name') or 'n/a'} | "
               f"Stage: {rec.get('journey_stage') or 'n/a'} | Read-only golden "
               f"query from Metabase card {card_id or 'n/a'}.")
    return NodeSpec(
        kind=NodeKind.QUERY,
        title=title,
        summary=summary,
        payload=payload,
        source_ref=node_key("golden_queries", ident),
        evidence_ref=card_id or ident,
        confidence=_conf_source(),
    )

def _derive_definitions(rec: Dict[str, Any], sql: str) -> List[NodeSpec]:
    """Emit DEFINITION drafts from equality filters in a golden query's SQL."""
    specs: List[NodeSpec] = []
    info = extract(sql)
    card_id = str(rec.get("card_id", "")).strip()
    ident = card_id or _uid(rec.get("name"), sql)
    for col, values in info.get("filters", {}).items():
        if len(values) > 1 or (values and isinstance(values[0], str)):
            specs.append(NodeSpec(
                kind=NodeKind.DEFINITION,
                title=f"Definition: {col} ∈ {values}",
                summary=f"Column `{col}` uses business values {values} "
                        f"(derived from {ident}).",
                payload={"column": col, "values": values,
                         "source_sql": sql, "via": card_id},
                source_ref=node_key(f"golden_queries/{ident}#definition", col),
                evidence_ref=card_id or ident,
                confidence=_conf_source(),
            ))
    return specs


def _map_idiom(rec: Dict[str, Any], idx: int) -> NodeSpec:
    name = rec.get("name")
    ident = _uid(name, rec.get("description"), rec.get("sql_skeleton"))
    return NodeSpec(
        kind=NodeKind.IDIOM,
        title=name or f"Idiom {idx}",
        summary=rec.get("description", ""),
        payload={"sql_skeleton": rec.get("sql_skeleton", ""),
                 "when_to_use": rec.get("when_to_use", ""),
                 "category": rec.get("category", "")},
        source_ref=node_key("idioms", ident),
        confidence=_conf_source(),
    )


def _map_rule(rec: Dict[str, Any], idx: int) -> NodeSpec:
    rule_type = rec.get("rule_type") or ""
    ident = _uid(rule_type, rec.get("description"), rec.get("reasoning"))
    return NodeSpec(
        kind=NodeKind.BUSINESS_RULE,
        title=f"Rule: {rule_type or 'unnamed'}",
        summary=rec.get("description", ""),
        payload={"rule_type": rule_type, "reasoning": rec.get("reasoning", "")},
        source_ref=node_key("rules", ident),
        confidence=_conf_source(),
    )


def _map_stage(rec: Dict[str, Any]) -> Optional[NodeSpec]:
    stage = (rec.get("name") or "").strip()
    if not stage:
        return None
    return NodeSpec(
        kind=NodeKind.DEFINITION,
        title=f"Journey stage: {stage}",
        summary=f"Journey stage taxonomy entry: {stage}.",
        payload={"stage": stage},
        source_ref=node_key("stages", stage),
        confidence=_conf_source(),
    )


def _map_table(rec: Dict[str, Any]) -> Optional[NodeSpec]:
    name = (rec.get("name") or "").strip()
    if not name:
        return None
    return NodeSpec(
        kind=NodeKind.DEFINITION,
        title=f"Table: {name}",
        summary=f"Source table catalog: {name}.",
        payload={"database": rec.get("database", ""),
                 "column_count": rec.get("column_count"),
                 "row_count": rec.get("row_count")},
        source_ref=node_key("tables", name),
        confidence=_conf_source(),
    )


def plan_from_snapshot(snapshot: Dict[str, Any],
                       *,
                       derive_definitions: bool = True,
                       default_status: ReviewStatus = ReviewStatus.CANDIDATE) -> List[NodeSpec]:
    """Translate a parsed snapshot into a flat, ordered list of `NodeSpec` drafts."""
    specs: List[NodeSpec] = []

    # business-reasoning lookup keyed by Metabase card_id -> `intents[].b`
    intents: Dict[str, Dict[str, Any]] = {}
    for it in snapshot.get("intents", []):
        b = it.get("b") or {}
        cid = str(b.get("card_id", "")).strip()
        if cid:
            intents.setdefault(cid, b)

    for g in snapshot.get("golden_queries", []):
        rec = g.get("g") or {}
        reasoning = None
        cid = str(rec.get("card_id", "")).strip()
        if cid and cid in intents:
            reasoning = intents[cid].get("reasoning_summary")
        specs.append(_map_golden_query(rec, reasoning))
        if derive_definitions:
            specs.extend(_derive_definitions(rec, rec.get("sql", "")))

    for idx, it in enumerate(snapshot.get("idioms", [])):
        specs.append(_map_idiom(it.get("i") or {}, idx))

    for idx, it in enumerate(snapshot.get("rules", [])):
        specs.append(_map_rule(it.get("r") or {}, idx))

    for it in snapshot.get("stages", []):
        spec = _map_stage(it.get("st") or {})
        if spec:
            specs.append(spec)

    for it in snapshot.get("tables", []):
        spec = _map_table(it.get("t") or {})
        if spec:
            specs.append(spec)

    if default_status != ReviewStatus.CANDIDATE:
        for spec in specs:
            spec.status = default_status
    return specs