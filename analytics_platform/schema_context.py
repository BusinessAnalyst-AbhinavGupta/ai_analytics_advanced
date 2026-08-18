"""The single place that decides what an LLM sees before it writes anything.

Everything Tasks 5, 6, and 7 wrote to the Company Brain is inert until something
puts it in a prompt. `SchemaContext.rendered` is three blocks in a fixed order --
semantics, then base views, then schema -- business meaning first, then the
populations that meaning may be measured over, then the physical layout
underneath. **When they disagree, semantics beat base views beat schema beat the
retrieved example queries.**

This closes the gap that produced generic SQL: the query-writing prompt used to
be assembled from node titles and summaries and nothing else -- no column list,
no types, no idea what values a column holds -- so the model inferred column
names from whichever example query retrieval happened to surface, and invented
filter literals.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .domain import PROFILE_TOP_VALUES, AttributionRule, ColumnProfile, NodeKind

logger = logging.getLogger(__name__)

MAX_CONTEXT_TABLES = 8      # do not dump the whole warehouse into every prompt
ATTRIBUTION_HEADING = "=== APPROVED ATTRIBUTION RULES ==="
IDENTIFIER_DISTINCT_SHARE = 0.9   # distinct >= this share of rows -> [identifier]

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _tokens(text: str) -> set:
    return set(_TOKEN_RE.findall((text or "").lower()))


@dataclass
class SchemaContext:
    tables: List[Dict[str, Any]] = field(default_factory=list)   # {table, columns, row_count_estimate}
    semantics: Any = None                    # SemanticResolution
    base_views: List[Any] = field(default_factory=list)          # approved first, then drafts
    attributions: List[Any] = field(default_factory=list)        # AttributionRule, APPROVED only
    profiles: Dict[str, ColumnProfile] = field(default_factory=dict)  # flattened, for the cube guard
    rendered: str = ""
    profiled_now: List[str] = field(default_factory=list)   # profiled inline this turn
    unprofiled: List[str] = field(default_factory=list)     # could not profile -> a caveat
    collisions: List[str] = field(default_factory=list)     # same column name, two tables
    truncated_tables: bool = False


class SchemaContextBuilder:
    def __init__(self, junior, brain_for: Callable[[str], Any], settings,
                 semantic, base_views) -> None:
        self.junior = junior
        self.brain_for = brain_for
        self.settings = settings
        self.semantic = semantic
        self.base_views = base_views

    # -- table selection -----------------------------------------------------
    def relevant_tables(self, tenant_id: str, question: str,
                        query_nodes: Sequence[Any], defn_nodes: Sequence[Any]) -> List[str]:
        """Ranked, then capped. The first tier is never dropped: a matched
        metric's source table and a resolvable base view's own tables are the
        tables the query certainly needs."""
        from .brain.ingest import extract    # the existing parser; do not write a second

        pinned: List[str] = []
        overflow: List[str] = []

        def add(bucket: List[str], name: str) -> None:
            name = (name or "").strip()
            if name and name not in pinned and name not in overflow:
                bucket.append(name)

        resolution = self.semantic.resolve(tenant_id, question)
        for m in resolution.metrics:
            for t in m.source_tables:
                add(pinned, t)
        for d in resolution.dimensions:
            for t in d.source_tables:
                add(pinned, t)
        # A base view shown as selectable while its schema is invisible is the
        # worst of both, so its underlying tables are pinned too.
        for view in self._ordered_views(tenant_id):
            for t in extract(view.source_sql).get("tables", []):
                add(pinned, t)

        for node in query_nodes:
            sql = (getattr(node, "payload", {}) or {}).get("sql", "")
            for t in extract(sql).get("tables", []) if sql else []:
                add(overflow, t)
        for node in defn_nodes:
            title = getattr(node, "title", "") or ""
            if title.startswith("Table: "):
                add(overflow, title[len("Table: "):].strip())

        question_tokens = _tokens(question)
        catalog = self._catalog(tenant_id)
        for entry in catalog:
            table = entry.get("table", "")
            if table.lower() in question_tokens or (
                    question_tokens & {c.lower() for c in entry.get("columns", [])}):
                add(overflow, table)

        return (pinned + overflow)[:MAX_CONTEXT_TABLES]

    def _catalog(self, tenant_id: str) -> List[Dict[str, Any]]:
        try:
            return list(self.junior.get_catalog(tenant_id).get("tables", []))
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read catalog for tenant %s: %s", tenant_id, exc)
            return []

    def _ordered_views(self, tenant_id: str) -> List[Any]:
        """Approved first, then drafts -- approved views are always preferred, and
        the ordering is what the planner reads as a preference."""
        approved = self.base_views.all(tenant_id, approved_only=True)
        names = {v.name for v in approved}
        drafts = [v for v in self.base_views.all(tenant_id, approved_only=False)
                  if v.name not in names]
        return approved + drafts

    # -- attribution ---------------------------------------------------------
    def attribution_rules(self, tenant_id: str,
                          tables: Optional[Sequence[str]] = None) -> List[AttributionRule]:
        """The tenant's APPROVED attribution rules, for the tables in play.

        Approved only, and that is the whole point: a draft rule that reached the
        prompt would steer an answer that no human has agreed to, and would do it
        invisibly, because by the time it is baked into a base view's source_sql
        it is already inside a population_hash.
        """
        from .junior import ATTRIBUTION_TITLE_PREFIX

        wanted = {t for t in (tables or []) if t}
        out: List[AttributionRule] = []
        try:
            nodes = self.brain_for(tenant_id).all(kind=NodeKind.DEFINITION, limit=1000)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read attribution rules for %s: %s", tenant_id, exc)
            return []
        for node in nodes:
            if not (node.title or "").startswith(ATTRIBUTION_TITLE_PREFIX):
                continue
            if not node.status.is_usable():
                continue
            payload = dict(node.payload or {})
            if wanted and (payload.get("table") or "") not in wanted:
                continue
            try:
                out.append(AttributionRule.from_dict(payload))
            except (TypeError, ValueError) as exc:
                logger.warning("skipping malformed attribution node %s: %s", node.id, exc)
        return out

    # -- build ---------------------------------------------------------------
    def build(self, tenant_id: str, question: str, query_nodes: Sequence[Any],
              defn_nodes: Sequence[Any], profile_if_missing: bool = True) -> SchemaContext:
        resolution = self.semantic.resolve(tenant_id, question)
        views = self._ordered_views(tenant_id)
        candidates = self.relevant_tables(tenant_id, question, query_nodes, defn_nodes)

        # The cap is applied in relevant_tables; recompute whether it bit so the
        # rendered block can say so.
        all_candidates = len(set(candidates))
        ctx = SchemaContext(semantics=resolution, base_views=views)

        catalog_by_table = {e.get("table"): e for e in self._catalog(tenant_id)}
        row_estimates: Dict[str, int] = {}

        for table in candidates:
            profiles = self._profiles_for(tenant_id, table, profile_if_missing, ctx)
            entry = catalog_by_table.get(table, {})
            payload = {}
            try:
                payload = self.junior.get_profile_payload(tenant_id, table) or {}
            except Exception:  # noqa: BLE001
                payload = {}
            rows = int(payload.get("row_count_estimate") or 0)
            row_estimates[table] = rows
            ctx.tables.append({
                "table": table,
                "columns": [p.column for p in profiles] or list(entry.get("columns", [])),
                "profiles": profiles,
                "types": list(entry.get("types", [])),
                "row_count_estimate": rows,
            })

        ctx.attributions = self.attribution_rules(
            tenant_id, [e["table"] for e in ctx.tables])
        ctx.truncated_tables = self._selection_was_capped(
            tenant_id, question, query_nodes, defn_nodes)
        ctx.profiles = self._flatten(ctx, row_estimates)
        ctx.rendered = self._render(tenant_id, ctx)
        return ctx

    def _selection_was_capped(self, tenant_id, question, query_nodes, defn_nodes) -> bool:
        from .brain.ingest import extract
        seen = set()
        for node in query_nodes:
            sql = (getattr(node, "payload", {}) or {}).get("sql", "")
            seen.update(extract(sql).get("tables", []) if sql else [])
        for node in defn_nodes:
            title = getattr(node, "title", "") or ""
            if title.startswith("Table: "):
                seen.add(title[len("Table: "):].strip())
        resolution = self.semantic.resolve(tenant_id, question)
        seen.update(t for m in resolution.metrics for t in m.source_tables)
        for view in self._ordered_views(tenant_id):
            seen.update(extract(view.source_sql).get("tables", []))
        return len(seen) > MAX_CONTEXT_TABLES

    def _profiles_for(self, tenant_id: str, table: str, profile_if_missing: bool,
                      ctx: SchemaContext) -> List[ColumnProfile]:
        try:
            profiles = self.junior.get_column_profiles(tenant_id, table)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read profiles for %s: %s", table, exc)
            profiles = []
        if profiles or not profile_if_missing:
            return profiles
        # Profile inline and continue the same turn. A table that cannot be
        # profiled (permissions, executor failure) goes into `unprofiled` and the
        # turn proceeds with columns-and-types only -- it must never take the chat
        # down.
        try:
            produced = self.junior.profile_tables(tenant_id, [table]) or {}
            profiles = list(produced.get(table) or [])
            ctx.profiled_now.append(table)
        except Exception as exc:  # noqa: BLE001
            logger.warning("inline profiling of %s failed for tenant %s: %s",
                           table, tenant_id, exc)
            ctx.unprofiled.append(table)
        return profiles

    def _flatten(self, ctx: SchemaContext,
                 row_estimates: Dict[str, int]) -> Dict[str, ColumnProfile]:
        """column -> profile, across tables. On a collision keep the entry from
        the table with the larger row estimate and record it: a silently-wrong
        cardinality is exactly what the cube guard cannot survive."""
        flat: Dict[str, ColumnProfile] = {}
        owner: Dict[str, str] = {}
        for entry in ctx.tables:
            table = entry["table"]
            for p in entry["profiles"]:
                if p.column not in flat:
                    flat[p.column], owner[p.column] = p, table
                    continue
                previous = owner[p.column]
                ctx.collisions.append(
                    f"column {p.column!r} exists in both {previous!r} and {table!r} with "
                    f"different profiles; used {previous!r}'s")
                if row_estimates.get(table, 0) > row_estimates.get(previous, 0):
                    flat[p.column], owner[p.column] = p, table
                    ctx.collisions[-1] = (
                        f"column {p.column!r} exists in both {previous!r} and {table!r} "
                        f"with different profiles; used {table!r}'s")
        return flat

    # -- rendering -----------------------------------------------------------
    def _render(self, tenant_id: str, ctx: SchemaContext) -> str:
        blocks = []
        semantics = self.semantic.render(ctx.semantics)
        if semantics:
            blocks.append(semantics)
        blocks.append(self.base_views.render(ctx.base_views, tenant_id))
        attributions = self._render_attributions(ctx.attributions)
        if attributions:
            blocks.append(attributions)
        blocks.append(self._render_schema(ctx))
        return "\n".join(blocks)

    @staticmethod
    def _render_attributions(rules: Sequence[AttributionRule]) -> str:
        """Between the base views and the schema, because that is where it acts.

        A rule is a property of the population, so it belongs inside a base
        view's source_sql -- applied there once, hashed, inherited by every cube.
        Applied instead as a per-question filter it would be applied twice, or
        not at all, and two questions would again disagree.
        """
        if not rules:
            return ""
        lines = [ATTRIBUTION_HEADING, "",
                 "Approved by this company. Apply these INSIDE the base view's "
                 "source_sql (a ROW_NUMBER pick over the grain), never as a "
                 "per-question filter -- a rule applied above the base is outside "
                 "its population_hash and cannot be reconciled.", ""]
        for r in rules:
            lines.append(f"- {r.column} collapses onto {', '.join(r.grain) or 'the grain'} "
                         f"by {r.strategy}")
            if r.priority_values:
                lines.append(f"    ranked best first: {r.priority_values}")
            if r.tiebreakers:
                lines.append(f"    tiebreakers: {r.tiebreakers}")
            if r.rationale:
                lines.append(f"    {r.rationale}")
        lines.append("")
        return "\n".join(lines)

    def _render_schema(self, ctx: SchemaContext) -> str:
        lines = ["=== DATABASE SCHEMA (authoritative -- use these exact table and "
                 "column names) ===", ""]
        for entry in ctx.tables:
            rows = entry["row_count_estimate"]
            header = f"TABLE {entry['table']}"
            if rows:
                header += f"  (~{rows:,} rows)"
            lines.append(header)
            profiles = entry["profiles"]
            if not profiles:
                for column in entry["columns"]:
                    lines.append(f"  {column}")
                if not entry["columns"]:
                    lines.append("  (columns unknown -- this table could not be described)")
            for p in profiles:
                lines.extend(self._render_column(p, rows))
            lines.append("")
        if ctx.unprofiled:
            lines.append(f"NOT PROFILED (values were not verified against the data): "
                         f"{', '.join(ctx.unprofiled)}")
            lines.append("")
        if ctx.truncated_tables:
            lines.append(f"NOTE: this list was truncated to {MAX_CONTEXT_TABLES} tables. "
                         f"Other tables exist that you have NOT been shown -- do not "
                         f"assume you have seen the whole warehouse.")
            lines.append("")
        lines.extend([
            "RULES:",
            "- Use only the table and column names listed above. If the question needs a",
            "  column that is not listed, say so instead of inventing one.",
            "- When a column shows ALL VALUES, that list is exhaustive -- filter using those",
            "  exact literals, matching their exact casing. Never invent a value or guess",
            "  its casing.",
            "- When a column shows TOP N (not exhaustive), other values exist; prefer a",
            "  range or a LIKE predicate over an IN list you cannot complete.",
            "- Ranges are the true min/max in the data -- do not filter outside them.",
            "- FAN-OUT means that column holds more than one value for a single key. If you",
            "  extract at that key's grain, you MUST collapse the column with an explicit",
            "  attribution rule -- never by adding it to GROUP BY, which silently changes",
            "  the grain and double-counts those keys.",
            "",
        ])
        return "\n".join(lines)

    def _render_column(self, p: ColumnProfile, table_rows: int) -> List[str]:
        tag = " [identifier]" if self._is_identifier(p, table_rows) else ""
        null_pct = f"{p.null_fraction * 100:.0f}% null"
        head = f"  {p.column:<16}{p.dtype:<10}"
        if p.distinct_count:
            head += f"{p.distinct_count:,} distinct, "
        head += null_pct
        if p.min_value or p.max_value:
            head += f"   range {p.min_value} .. {p.max_value}"
        out = [head + tag]
        pad = " " * 28
        if p.values and p.values_complete:
            listed = ", ".join(f"'{v}'" for v in p.values)
            out.append(f"{pad}ALL VALUES: {listed}")
        elif p.values:
            listed = ", ".join(f"'{v}'" for v in p.values[:PROFILE_TOP_VALUES])
            out.append(f"{pad}TOP {min(len(p.values), PROFILE_TOP_VALUES)} OF "
                       f"~{p.distinct_count} (not exhaustive): {listed}")
        for key, share in sorted(p.fanout_by_key.items()):
            if share > 0:
                out.append(f"{pad}FAN-OUT: {share * 100:.0f}% of {key} have >1 value "
                           f"-- needs attribution")
        return out

    @staticmethod
    def _is_identifier(p: ColumnProfile, table_rows: int) -> bool:
        if p.column.lower().endswith(("_id", "_key")):
            return True
        return bool(table_rows) and p.distinct_count >= IDENTIFIER_DISTINCT_SHARE * table_rows


__all__ = ["SchemaContextBuilder", "SchemaContext", "MAX_CONTEXT_TABLES"]
