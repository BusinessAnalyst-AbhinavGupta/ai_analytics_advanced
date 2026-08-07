"""Legacy query + analysis ingestion -> candidate knowledge nodes.

Replaces regex-only extraction with `sqlglot` AST parsing: detect read-only,
extract tables, columns, WHERE equality filters (candidate business definitions),
group-by / aggregations. Every imported query lands as an unverified CANDIDATE
node (lifecycle) — never auto-approved.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import sqlglot

from ..domain import KnowledgeNode, NodeKind, ReviewStatus
from .store import CompanyBrain


def _extract_ast(sql: str) -> Optional[sqlglot.expressions.Expression]:
    try:
        return sqlglot.parse_one(sql, read="athena")
    except Exception:
        try:
            return sqlglot.parse_one(sql)
        except Exception:
            return None


def is_read_only(sql: str) -> bool:
    head = re.sub(r"\s+", " ", sql).strip().lstrip("(").upper()
    for banned in ("INSERT", "UPDATE ", "DELETE ", "CREATE ", "DROP ", "ALTER ",
                   "TRUNCATE ", "GRANT ", "REVOKE ", "MERGE ", "CALL ", "EXEC "):
        if head.startswith(banned):
            return False
    return True


def extract(sql: str) -> Dict[str, Any]:
    """Return a structural summary of a legacy query (no execution)."""
    ast = _extract_ast(sql)
    info: Dict[str, Any] = {"read_only": is_read_only(sql), "has_ast": ast is not None,
                            "tables": [], "columns": [], "filters": {},
                            "dialect": "athena", "aggregate": False}
    if ast is None:
        return info

    for node in ast.walk():
        if isinstance(node, sqlglot.exp.Table):
            t = node.name
            if t and t not in info["tables"]:
                info["tables"].append(t)
        elif isinstance(node, sqlglot.exp.Column):
            c = node.name
            if c and c not in info["columns"]:
                info["columns"].append(c)
        elif isinstance(node, sqlglot.exp.EQ):
            col, val = _eq_pair(node)
            if col:
                info["filters"].setdefault(col, [])
                if val not in info["filters"][col]:
                    info["filters"][col].append(val)
        elif isinstance(node, (sqlglot.exp.Sum, sqlglot.exp.Count, sqlglot.exp.Avg,
                               sqlglot.exp.Min, sqlglot.exp.Max, sqlglot.exp.Group)):
            info["aggregate"] = True
    return info


def _eq_pair(eq_node) -> Tuple[Optional[str], Any]:
    left, right = eq_node.left, eq_node.right
    if isinstance(left, sqlglot.exp.Column):
        col = left.name
        val = _literal(right)
        return (col, val)
    if isinstance(right, sqlglot.exp.Column):
        return (right.name, _literal(left))
    return (None, None)


def _literal(node) -> Any:
    if node is None:
        return None
    if isinstance(node, sqlglot.exp.Literal):
        return node.this
    try:
        if isinstance(node, sqlglot.exp.Star):
            return "*"
        val = node.name
        return val
    except Exception:
        return str(node)


def ingest_sql(brain: CompanyBrain, sql: str, source_ref: str = "",
               title: Optional[str] = None, created_by: str = "ingest") -> List[KnowledgeNode]:
    """Parse a legacy query and create CANDIDATE knowledge nodes."""
    info = extract(sql)
    nodes: List[KnowledgeNode] = []
    if title is None:
        title = f"Legacy query from {source_ref or 'unknown'}"
    summary = (f"Tables: {', '.join(info['tables']) or 'n/a'} | "
               f"Columns: {', '.join(info['columns'][:8]) or 'n/a'} | "
               f"Read-only: {info['read_only']}")
    q = brain.create(NodeKind.QUERY, title=title, summary=summary,
                     payload={"sql": sql, "structure": info}, source_ref=source_ref,
                     created_by=created_by, status=ReviewStatus.CANDIDATE)
    nodes.append(q)

    # Candidate business definitions from equality filters (e.g. action='apptBooked')
    for col, values in info["filters"].items():
        if len(values) > 1 or (values and isinstance(values[0], str)):
            title_def = f"Definition: {col} ∈ {values}"
            d = brain.create(NodeKind.DEFINITION, title=title_def,
                             summary=f"Column `{col}` uses business values {values}.",
                             payload={"column": col, "values": values, "source_sql": sql},
                             source_ref=source_ref, created_by=created_by,
                             status=ReviewStatus.CANDIDATE)
            nodes.append(d)
    return nodes


def column_business_definitions(brain: CompanyBrain, columns: Optional[List[str]] = None,
                                limit_per_column: int = 8) -> Dict[str, List[Any]]:
    """Return approved/usable candidate filter VALUES per column (golden-aware)."""
    nodes = [n for n in brain.all(kind=NodeKind.DEFINITION, limit=500)
             if n.status.is_usable()]
    out: Dict[str, List[Any]] = {}
    for n in nodes:
        col = n.payload.get("column")
        if columns and col and col not in columns:
            continue
        vals = n.payload.get("values", [])
        if not col:
            continue
        out.setdefault(col, [])
        for v in vals:
            if v not in out[col]:
                out[col].append(v)
        if len(out[col]) > limit_per_column:
            out[col] = out[col][:limit_per_column]
    return out