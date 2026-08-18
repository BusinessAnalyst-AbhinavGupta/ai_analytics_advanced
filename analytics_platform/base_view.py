"""Base views: the governed ID-grain population every answer is computed from.

**This is the file the triangulation guarantee lives in.** Two questions asked
of the same warehouse should be answerable from the same rows, so that when
their numbers are compared they either agree or the disagreement is explainable.
Today every turn authors its own FROM/JOIN/WHERE, so two answers can silently
rest on two different populations and nobody can prove which. The warehouse fix
is CREATE VIEW; this Athena account is read-only and no additional access is
being requested, so the view becomes a client-side construct: a stored, hashed,
governed SQL definition inlined **verbatim** as a CTE into every derived query.

Two hashes, because the base decides *which rows exist* and the columns above it
are a projection:

  population_hash  -- over source_sql + sorted grain + canonicalised attributions.
                      THIS is the reconciliation key.
  projection_hash  -- over the exposed column list. Informational; never gates
                      reconciliation, because adding a column cannot change a
                      SUM over the same rows.

A per-question filter is a **slice**, not a population: it sits in the cube's
WHERE above the base and is deliberately not hashed. That is what makes
"question A filtered to Germany" and "question B unfiltered" reconcilable.

Composition never executes anything and never imports an executor, so the same
SQL can be hashed, logged, and shown to a human before it runs.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import asdict
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .config import MAX_CUBE_CELLS, MAX_DIMENSION_CARDINALITY
from .database import dump_json
from .domain import (AttributionRule, BaseView, ColumnProfile, CubeMeasure, CubeSpec,
                     CubeSQL, KnowledgeNode, NodeKind, ReconcileResult, ReviewStatus,
                     now_iso)

logger = logging.getLogger(__name__)

BASE_VIEW_TITLE_PREFIX = "Base View: "

_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")

_AVG_RE = re.compile(r"^\s*AVG\s*\((.+)\)\s*$", re.IGNORECASE | re.DOTALL)
_NON_ADDITIVE_RE = re.compile(
    r"COUNT\s*\(\s*DISTINCT|APPROX_DISTINCT|MEDIAN|PERCENTILE|APPROX_PERCENTILE|"
    r"\bMODE\s*\(|STDDEV|VARIANCE|CORR\s*\(", re.IGNORECASE)


def canonicalise_sql(sql: str) -> str:
    """Strip comments and collapse whitespace -- but NEVER lowercase.

    String literals are case-sensitive and 'mobile' is not 'Mobile', so folding
    case here would make two genuinely different populations hash the same. A
    cosmetic reformat of source_sql therefore does change the hash. That is
    correct and intended: the base is a governed artifact edited by a human
    through a review flow, not a string an LLM re-emits each turn, and a human
    edit *should* announce that answers before and after it are no longer
    trivially comparable.
    """
    stripped = _BLOCK_COMMENT_RE.sub(" ", sql or "")
    stripped = _LINE_COMMENT_RE.sub(" ", stripped)
    return _WHITESPACE_RE.sub(" ", stripped).strip()


def _canonical_attributions(rules: Sequence[AttributionRule]) -> str:
    """The rule *list* is a set, so it is sorted. The ranking *inside* a rule is
    the semantics and is never sorted."""
    parts = []
    for r in sorted(rules, key=lambda r: (r.column, ",".join(sorted(r.grain)))):
        parts.append("|".join([
            r.column,
            ",".join(sorted(r.grain)),
            r.strategy,
            ">".join(r.priority_values),     # order-significant: it IS the ranking
            ">".join(r.tiebreakers),         # order-significant
        ]))
    return ";".join(parts)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _quote(value: Any) -> str:
    """Single-quote a literal, doubling any internal quote."""
    return "'" + str(value).replace("'", "''") + "'"


def _escape_ident(name: str) -> str:
    """Column names reach an identifier position, so they are constrained to a
    plain identifier shape rather than quoted-and-hoped."""
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name or ""):
        raise ValueError(f"not a usable column identifier: {name!r}")
    return name


def reconcile(population_hash_a: str, value_a: float,
              population_hash_b: str, value_b: float,
              measure: str, tolerance: float = 1e-6) -> ReconcileResult:
    """Do these two numbers rest on the same rows, and if so do they agree?

    A mismatch in population is not a small disagreement to be reported with a
    caveat -- it means the two numbers are not comparable at all, which is the
    exact mistake this whole design exists to prevent.
    """
    if not population_hash_a or not population_hash_b:
        return ReconcileResult(
            same_population=False, population_hash_a=population_hash_a,
            population_hash_b=population_hash_b, measure=measure, agrees=False,
            explanation="At least one answer was computed with no base view governing "
                        "it, so it has no population to compare against. Numbers from "
                        "that path cannot be reconciled with anything.")
    if population_hash_a != population_hash_b:
        return ReconcileResult(
            same_population=False, population_hash_a=population_hash_a,
            population_hash_b=population_hash_b, measure=measure, agrees=False,
            explanation=f"These answers rest on different row populations "
                        f"({population_hash_a[:8]}… and {population_hash_b[:8]}…), so "
                        f"their {measure} figures are not comparable. Comparing them "
                        f"would invite reading the difference as meaningful.")
    agrees = abs(float(value_a) - float(value_b)) <= tolerance
    verdict = "they agree" if agrees else "they DISAGREE"
    return ReconcileResult(
        same_population=True, population_hash_a=population_hash_a,
        population_hash_b=population_hash_b, measure=measure,
        value_a=float(value_a), value_b=float(value_b), agrees=agrees,
        explanation=f"Both answers were computed over the same population "
                    f"({population_hash_a[:8]}…); {measure} is {value_a:,.2f} and "
                    f"{value_b:,.2f} — {verdict}.")


class BaseViewRegistry:

    # Base views are found by scanning DEFINITION nodes, and a base view that
    # falls off the end of that scan does not error -- it silently stops
    # existing, so a governed population quietly becomes an ungoverned one-off.
    # The live tenant already holds 634 DEFINITION nodes against the old 1000.
    SCAN_LIMIT = 100_000

    def __init__(self, brain_for: Callable[[str], Any]) -> None:
        self.brain_for = brain_for

    # -- hashing -------------------------------------------------------------
    def population_hash(self, view: BaseView) -> str:
        return _sha("\x1f".join([
            canonicalise_sql(view.source_sql),
            ",".join(sorted(view.grain)),          # grain is a set
            _canonical_attributions(view.attributions),
        ]))

    def projection_hash(self, columns: Iterable[str]) -> str:
        return _sha(",".join(sorted(columns)))

    # -- storage -------------------------------------------------------------
    def _node(self, tenant_id: str, name: str) -> Optional[KnowledgeNode]:
        title = f"{BASE_VIEW_TITLE_PREFIX}{name}"
        return next((n for n in self.brain_for(tenant_id).all(kind=NodeKind.DEFINITION, limit=self.SCAN_LIMIT)
                     if n.title == title), None)

    def all(self, tenant_id: str, approved_only: bool = True) -> List[BaseView]:
        out: List[BaseView] = []
        for node in self.brain_for(tenant_id).all(kind=NodeKind.DEFINITION, limit=self.SCAN_LIMIT):
            if not node.title.startswith(BASE_VIEW_TITLE_PREFIX):
                continue
            if approved_only and not node.status.is_usable():
                continue
            try:
                out.append(BaseView.from_dict(node.payload))
            except (TypeError, ValueError) as exc:
                logger.warning("skipping malformed base view node %s: %s", node.id, exc)
        return out

    def status_of(self, tenant_id: str, name: str) -> Optional[ReviewStatus]:
        node = self._node(tenant_id, name)
        return node.status if node else None

    def is_approved(self, tenant_id: str, name: str) -> bool:
        status = self.status_of(tenant_id, name)
        return bool(status and status.is_usable())

    def get(self, tenant_id: str, name: str,
            approved_only: bool = True) -> Optional[BaseView]:
        node = self._node(tenant_id, name)
        if node is None:
            return None
        if approved_only and not node.status.is_usable():
            return None
        try:
            return BaseView.from_dict(node.payload)
        except (TypeError, ValueError) as exc:
            logger.warning("malformed base view %s: %s", name, exc)
            return None

    def upsert(self, tenant_id: str, view: BaseView, by: str) -> KnowledgeNode:
        """Created unapproved and promoted through the existing submit/approve
        flow -- no new governance machinery, the same as metrics."""
        brain = self.brain_for(tenant_id)
        payload = asdict(view)
        payload["population_hash"] = self.population_hash(view)
        summary = (f"{view.name}: one row per {', '.join(view.grain) or 'unstated grain'}; "
                   f"~{view.row_count_estimate:,} rows. {view.description}".strip())
        node = self._node(tenant_id, view.name)
        if node is not None:
            # A human approved SPECIFIC SQL. If the population changed, that
            # approval no longer describes what would run, and the APPROVED badge
            # is precisely what tells a reader the figure is not provisional --
            # so the review is withdrawn and has to be earned again. Cosmetic
            # edits (description, aliases) leave the hash alone and keep it:
            # re-reviewing a base over a typo fix trains people to rubber-stamp.
            previous = (node.payload or {}).get("population_hash", "")
            brain.update_field(node.id, "payload", dump_json(payload))
            node = brain.update_field(node.id, "summary", summary)
            if previous and previous != payload["population_hash"] and node.status.is_usable():
                logger.info("base view %s changed population (%s -> %s); withdrawing "
                            "approval pending re-review", view.name, previous[:8],
                            payload["population_hash"][:8])
                node = brain.update_field(node.id, "status", ReviewStatus.CANDIDATE.value)
            return node
        return brain.create(kind=NodeKind.DEFINITION,
                            title=f"{BASE_VIEW_TITLE_PREFIX}{view.name}",
                            summary=summary, payload=payload, created_by=by,
                            status=ReviewStatus.CANDIDATE)

    # -- grain verification --------------------------------------------------
    # A base view asserts "one row per session_id". Nothing until now checked it,
    # and the layer that used to check it cannot: GROUP BY deduplicates the
    # dimension tuple unconditionally, so a base emitting three rows per key
    # produces a cube where every cell is unique and every SUM is silently
    # tripled. The violation is invisible at exactly the layer that used to catch
    # it. So it is verified on the DEFINITION -- once per population_hash, which
    # also means an edited source_sql re-probes automatically -- and a cube over
    # an unverified base is refused outright.

    def compose_grain_probe(self, view: BaseView) -> str:
        """Two numbers, one round trip: rows in the population, distinct keys."""
        if not view.grain:
            raise ValueError(f"base view {view.name!r} declares no grain to verify")
        keys = [_escape_ident(k) for k in view.grain]
        if len(keys) == 1:
            distinct = f"COUNT(DISTINCT {keys[0]})"
        else:
            # Trino/Athena will not COUNT(DISTINCT ROW(...)), so a composite grain
            # is concatenated with ASCII 31 (unit separator) -- a character that
            # cannot appear in an identifier value, so it cannot merge two
            # distinct tuples into one. NULL keys COALESCE to '' and therefore
            # collide; that can only make the probe MORE likely to flag, which is
            # the right direction to be wrong in, since a NULL grain key is
            # already a grain violation.
            joined = " || CHR(31) || ".join(
                f"COALESCE(CAST({k} AS VARCHAR), '')" for k in keys)
            distinct = f"COUNT(DISTINCT {joined})"
        # NULL keys are counted separately: they are not duplicates, but they
        # make rows > keys look exactly like fan-out.
        null_test = " OR ".join(f"{k} IS NULL" for k in keys)
        return (f"WITH base AS (\n{view.source_sql}\n)\n"
                f"SELECT COUNT(*) AS row_count, {distinct} AS key_count\n"
                f"     , SUM(CASE WHEN {null_test} THEN 1 ELSE 0 END) AS null_keys\n"
                f"FROM base")

    def needs_grain_check(self, view: BaseView) -> bool:
        """True until the CURRENT population has been probed. Keyed by hash, not
        by a flag, so editing the SQL or an attribution rule re-probes by
        itself -- which is the second reason the hash is over canonicalised SQL
        rather than a version number nobody would bump."""
        return (not view.grain_checked_hash
                or view.grain_checked_hash != self.population_hash(view))

    def record_grain_check(self, tenant_id: str, view: BaseView,
                           rows: int, keys: int, null_keys: int = 0) -> BaseView:
        """Write the measurement back onto the node and return the updated view.

        `row_count_estimate` is overwritten with the probe's `rows`: the cube cell
        guard needs a real number and profiling could only offer a sampled floor.
        This is the honest one and it costs nothing extra.
        """
        rows, keys, null_keys = int(rows), int(keys), int(null_keys or 0)
        # A NULL-keyed row collapses to one GROUP BY group but contributes
        # nothing to COUNT(DISTINCT), so `keys` under-counts by exactly one when
        # NULLs are present. Add that group back before comparing, or a base
        # whose only defect is NULL keys is also reported as duplicated.
        expected = keys + (1 if null_keys else 0)
        view.grain_null_keys = null_keys
        view.grain_verified = rows > 0 and rows == expected and not null_keys
        view.grain_violation_ratio = (round(1 - expected / rows, 6)
                                      if rows > 0 and expected < rows else 0.0)
        view.grain_checked_at = now_iso()
        view.grain_checked_hash = self.population_hash(view)
        if rows > 0:
            view.row_count_estimate = rows
        self.upsert(tenant_id, view, by="junior")
        return view

    def _grain_refusal(self, view: BaseView) -> str:
        if view.grain_null_keys:
            return (f"base view {view.name!r} has {view.grain_null_keys:,} rows whose "
                    f"{view.grain} is NULL. Those rows have no key, so they are not at "
                    f"the declared grain and every one of them collapses into a single "
                    f"bucket -- they are NOT duplicates, so do not go looking for one. "
                    f"Exclude them in the base, or key the population on something "
                    f"that is always present.")
        if view.grain_violation_ratio:
            return (f"base view {view.name!r} is not at the grain it claims: "
                    f"{view.grain_violation_ratio:.1%} of its rows are duplicates at "
                    f"{view.grain}, so every measure over it would be multiplied. "
                    f"Fix the base (an attribution rule or a de-duplicating pick) "
                    f"rather than aggregating over it.")
        return (f"base view {view.name!r} has not had its grain verified. A cube over "
                f"an unverified base hides fan-out behind GROUP BY -- every cell "
                f"looks unique while every measure is multiplied. Probe it first.")

    # -- measures ------------------------------------------------------------
    def _resolve_measure(self, m: CubeMeasure) -> CubeMeasure:
        """Rewrite AVG into a sum and a count, and classify additivity.

        The rewrite happens here rather than being trusted to the caller: an AVG
        column in a cube manifest would let the DataManager roll up an average of
        averages, which is wrong the moment the cube is aggregated further.
        """
        avg = _AVG_RE.match(m.expr or "")
        if avg:
            inner = avg.group(1).strip()
            return CubeMeasure(
                name=m.name,
                expr=f"SUM({inner}) AS {m.name}_sum, COUNT({inner}) AS {m.name}_count",
                additive=True,
                read_expr=f"{m.name}_sum / NULLIF({m.name}_count, 0)")
        additive = not bool(_NON_ADDITIVE_RE.search(m.expr or ""))
        return CubeMeasure(name=m.name, expr=f"{m.expr} AS {m.name}",
                           additive=additive, read_expr=m.read_expr)

    @staticmethod
    def _measure_columns(m: CubeMeasure) -> List[str]:
        if m.read_expr and "_sum" in m.expr:
            return [f"{m.name}_sum", f"{m.name}_count"]
        return [m.name]

    # -- the guard -----------------------------------------------------------
    def _guard(self, view: BaseView, spec: CubeSpec,
               profiles: Dict[str, ColumnProfile]) -> CubeSQL:
        """Returns a CubeSQL carrying only the refusal, or ok=True with the
        estimate. Sizing happens before any SQL is built."""
        warnings: List[str] = []
        offending: List[str] = []

        legal = set(view.dimension_columns)
        unknown = [d for d in spec.dimensions if d not in legal]
        unknown += [c for c in spec.filters if c not in legal and c not in spec.dimensions]
        if unknown:
            return CubeSQL(ok=False, offending_dimensions=sorted(set(unknown)),
                           error=f"base view {view.name!r} does not carry "
                                 f"{sorted(set(unknown))}. Reaching around the base into a "
                                 f"raw table produces a number that cannot be reconciled "
                                 f"with anything.")

        cardinalities: Dict[str, int] = {}
        for d in spec.dimensions:
            profile = profiles.get(d)
            if profile is None:
                # Fail closed. Absent profiles are absent, never defaulted -- the
                # guard cannot honestly certify a cube it is unable to size, and
                # an unprofiled column must not be able to sneak a 10M-cell cube
                # past it.
                warnings.append(f"{d!r} is not profiled, so its cardinality is unknown "
                                f"and this cube cannot be sized. Profile the table first.")
                offending.append(d)
                cardinalities[d] = MAX_DIMENSION_CARDINALITY
                continue
            cardinalities[d] = max(1, int(profile.distinct_count))
            if cardinalities[d] > MAX_DIMENSION_CARDINALITY:
                offending.append(d)

        if offending:
            return CubeSQL(ok=False, warnings=warnings,
                           offending_dimensions=sorted(set(offending)),
                           error=f"{sorted(set(offending))} cannot be cube dimensions: "
                                 f"above {MAX_DIMENSION_CARDINALITY} distinct values they "
                                 f"are keys or free text, not dimensions.")

        product = 1
        for d in spec.dimensions:
            product *= cardinalities[d]
        estimated = min(product, view.row_count_estimate or MAX_CUBE_CELLS)

        if estimated > MAX_CUBE_CELLS:
            # Name the smallest set of dimensions whose removal brings it under,
            # widest first, so the planner can drop or bucket rather than guess.
            widest = sorted(spec.dimensions, key=lambda d: -cardinalities[d])
            culprits, remaining = [], product
            for d in widest:
                if remaining <= MAX_CUBE_CELLS:
                    break
                culprits.append(d)
                remaining //= cardinalities[d]
            return CubeSQL(ok=False, estimated_cells=estimated, warnings=warnings,
                           offending_dimensions=culprits,
                           error=f"that cube would produce about {estimated:,} cells, over "
                                 f"the limit of {MAX_CUBE_CELLS}. The largest dimensions "
                                 f"are {culprits} — drop or bucket one.")

        return CubeSQL(ok=True, estimated_cells=estimated, warnings=warnings)

    # -- composition ---------------------------------------------------------
    def _where(self, spec: CubeSpec, extra: str = "") -> str:
        clauses: List[str] = []
        for column in sorted(spec.filters):
            values = spec.filters[column]
            if not values:
                continue
            literals = ", ".join(_quote(v) for v in values)
            clauses.append(f"{_escape_ident(column)} IN ({literals})")
        if spec.time_column and spec.time_start and spec.time_end:
            clauses.append(f"{_escape_ident(spec.time_column)} BETWEEN "
                           f"DATE {_quote(spec.time_start)} AND DATE {_quote(spec.time_end)}")
        if extra:
            clauses.append(extra)
        if not clauses:
            return ""
        return "WHERE " + "\n  AND ".join(clauses)

    def _select_block(self, dimensions: Sequence[str],
                      measures: Sequence[CubeMeasure]) -> str:
        pieces = [_escape_ident(d) for d in dimensions]
        lines = []
        if pieces:
            lines.append("SELECT " + ", ".join(pieces))
            lines.extend(f"     , {m.expr}" for m in measures)
        elif measures:
            lines.append(f"SELECT {measures[0].expr}")
            lines.extend(f"     , {m.expr}" for m in measures[1:])
        else:
            lines.append("SELECT *")
        return "\n".join(lines)

    def compose_cube(self, view: BaseView, spec: CubeSpec,
                     profiles: Dict[str, ColumnProfile]) -> CubeSQL:
        """GROUP BY <dimensions> over the inlined base, run in the warehouse.

        The stored source_sql is inlined byte for byte. The LLM never re-authors
        it -- a re-emitted base is a different string and therefore, correctly, a
        different population_hash, which would break the one thing the base
        exists to guarantee.
        """
        if not view.grain_verified:
            return CubeSQL(ok=False, error=self._grain_refusal(view),
                           population_hash=self.population_hash(view))
        guard = self._guard(view, spec, profiles)
        if not guard.ok:
            guard.population_hash = self.population_hash(view)
            return guard

        try:
            measures = [self._resolve_measure(m) for m in spec.measures]
            select_block = self._select_block(spec.dimensions, measures)
            where = self._where(spec)
        except ValueError as exc:
            return CubeSQL(ok=False, error=str(exc),
                           population_hash=self.population_hash(view))

        parts = [f"WITH base AS (\n{view.source_sql}\n)", select_block, "FROM base"]
        if where:
            parts.append(where)
        if spec.dimensions:
            # Ordinal GROUP BY: Athena accepts it and it cannot drift out of sync
            # with the SELECT list the way a repeated column list can.
            parts.append("GROUP BY " + ", ".join(str(i + 1) for i in range(len(spec.dimensions))))

        columns = list(spec.dimensions) + [c for m in measures
                                           for c in self._measure_columns(m)]
        return CubeSQL(
            ok=True, sql="\n".join(parts),
            population_hash=self.population_hash(view),
            projection_hash=self.projection_hash(columns),
            estimated_cells=guard.estimated_cells,
            measures=measures, columns=columns,
            non_additive=[m.name for m in measures if not m.additive],
            warnings=guard.warnings)

    def compose_keyset_chunk(self, view: BaseView, spec: CubeSpec, last_seen: Any,
                             chunk_rows: int, keys: Optional[List[str]] = None) -> str:
        """One page of a keyset walk. NEVER OFFSET.

        Athena rescans from the top on every OFFSET page, which is quadratic and,
        on a changing table, silently skips and duplicates rows. Two callers use
        this: ID-grain rows when a measure is genuinely non-additive, and any
        cube whose estimate exceeds one round trip -- the latter passes its own
        dimensions as `keys` rather than walking the grain.
        """
        if not view.grain_verified:
            # Fan-out multiplies an ID-grain page exactly as it multiplies a cube;
            # here it is worse, because the duplicate rows arrive looking real.
            raise ValueError(self._grain_refusal(view))
        key_columns = [_escape_ident(k) for k in (keys or view.grain)]
        if not key_columns:
            raise ValueError("keyset pagination needs at least one ordering key")

        cursor = ""
        if last_seen not in ("", None, []):
            values = last_seen if isinstance(last_seen, (list, tuple)) else [last_seen]
            if len(values) != len(key_columns):
                raise ValueError(
                    f"cursor {values!r} does not match key columns {key_columns!r}")
            if len(key_columns) == 1:
                cursor = f"{key_columns[0]} > {_quote(values[0])}"
            else:
                cursor = (f"({', '.join(key_columns)}) > "
                          f"({', '.join(_quote(v) for v in values)})")

        measures = [self._resolve_measure(m) for m in spec.measures]
        select_block = self._select_block(spec.dimensions, measures) if spec.dimensions \
            else self._select_block([], measures)
        parts = [f"WITH base AS (\n{view.source_sql}\n)", select_block, "FROM base"]
        where = self._where(spec, extra=cursor)
        if where:
            parts.append(where)
        if spec.dimensions:
            parts.append("GROUP BY " + ", ".join(str(i + 1) for i in range(len(spec.dimensions))))
        parts.append("ORDER BY " + ", ".join(key_columns))
        parts.append(f"LIMIT {int(chunk_rows)}")
        return "\n".join(parts)

    # -- prompt block --------------------------------------------------------
    def render(self, views: Sequence[BaseView], tenant_id: str = "",
               statuses: Optional[Dict[str, bool]] = None) -> str:
        """The block that goes into the planning and synthesis prompts, after the
        semantics and before the schema.

        Approval is a property of the Brain *node*, not of the BaseView payload,
        so it has to be resolved rather than read off the view. Pass `tenant_id`
        and the registry looks it up; pass `statuses` (name -> approved) when the
        caller already knows, to avoid a second read. With neither, every view
        renders as DRAFT -- the safe direction to be wrong in, since it only ever
        makes an answer *more* provisional than it needs to be.
        """
        lines = ["=== BASE VIEWS (the row populations you may build on) ===", ""]
        approved_by_name = dict(statuses or {})
        if tenant_id:
            for v in views:
                approved_by_name.setdefault(v.name, self.is_approved(tenant_id, v.name))
        for v in views:
            approved = approved_by_name.get(v.name, False)
            badge = "[APPROVED]" if approved else \
                "[DRAFT -- unreviewed, answers using it are provisional]"
            lines.append(f"BASE {v.name}  {badge}")
            lines.append(f"  Grain      : {', '.join(v.grain)}  (one row per "
                         f"{' + '.join(v.grain) or 'identifier'})")
            if v.row_count_estimate:
                lines.append(f"  Rows       : ~{v.row_count_estimate:,}")
            lines.append(f"  Dimensions : {', '.join(v.dimension_columns) or 'none declared'}")
            lines.append(f"  Measures   : {', '.join(v.measure_columns) or 'none declared'}")
            for rule in v.attributions:
                lines.append(f"  Attribution: {rule.column} collapsed to one value per "
                             f"{', '.join(rule.grain)} by {rule.strategy.replace('_', ' ')}"
                             + (f" ({' > '.join(rule.priority_values)})"
                                if rule.priority_values else ""))
            if v.description:
                lines.append(f"  Population : {v.description}")
            lines.append("")
        if not views:
            lines.append("(none defined yet for this company)")
            lines.append("")
        lines.extend([
            "RULES:",
            "- Choose exactly ONE base view and name it. Every number in your answer must",
            "  come from that one population, so that this answer can be compared against",
            "  others.",
            "- You do NOT write the base. It is inlined verbatim for you. You choose the",
            "  dimensions to GROUP BY, the measures, and the filters that slice it.",
            "- Slice and group using only that base's listed dimension columns. If the",
            "  question needs a column the base does not carry, say so -- do not reach",
            "  around the base into a raw table, because a number produced that way cannot",
            "  be reconciled with anything.",
            "- Prefer additive measures (SUM, COUNT(*), MIN, MAX). Ask for AVG as a plain",
            "  AVG and it will be stored as a sum and a count for you. COUNT(DISTINCT),",
            "  medians and percentiles do not roll up -- name them only when the question",
            "  truly needs them.",
            "- If no base view fits, propose one at ID grain (one row per identifier) and",
            "  say so in your rationale. It will be recorded as a DRAFT for human review",
            "  and the answer will be marked provisional.",
            "",
        ])
        return "\n".join(lines)


__all__ = ["BaseViewRegistry", "reconcile", "canonicalise_sql", "BASE_VIEW_TITLE_PREFIX"]
