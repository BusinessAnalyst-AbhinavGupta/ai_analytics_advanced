"""Does the workspace already hold what this turn needs?

That question -- "August, Germany, over the checkout-sessions population, cut by
device" -- is set containment with a right answer, so it is decided here, in
plain Python, over the extract manifests. **There is no model in this file.** The
LLM's job upstream is to *state* the requirement; deciding whether it is met is
not a judgement call, and delegating it is why a follow-up that was fully
answerable from cached data went back to the warehouse.

The containment rule is the one this design was always built around -- finer
stored grain is reusable, coarser is not -- moved up one level, from ID grain to
cube dimensions. A cube grouped by country x device x service_line answers any
question over a *subset* of those dimensions by summing over the rest. Two gates
sit around it, and both are load-bearing: the population must match exactly, and
every measure the requirement asks for must be additive in that cube.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .domain import CubeMeasure

logger = logging.getLogger(__name__)


@dataclass
class DataRequirement:
    base_view: str                         # which population
    population_hash: str = ""              # the reconciliation key; "" reuses nothing
    grain: List[str] = field(default_factory=list)        # the base's ID grain
    dimensions: List[str] = field(default_factory=list)   # the cube's GROUP BY
    measures: List[CubeMeasure] = field(default_factory=list)
    filters: Dict[str, List[str]] = field(default_factory=dict)   # the SLICE
    time_column: str = ""
    time_start: str = ""                   # ISO date, inclusive
    time_end: str = ""                     # ISO date, inclusive

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["measures"] = [asdict(m) for m in self.measures]
        return d


@dataclass
class CoverageVerdict:
    decision: str                # "reuse" | "widen" | "retrieve"
    label: str = ""              # the cube to reuse or widen, when there is one
    missing_dimensions: List[str] = field(default_factory=list)
    missing_measures: List[str] = field(default_factory=list)
    missing_time_ranges: List[Tuple[str, str]] = field(default_factory=list)
    supersedes: str = ""         # on "widen": the narrower cube this one replaces
    reason: str = ""             # human-readable; goes into the answer's provenance

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["missing_time_ranges"] = [list(r) for r in self.missing_time_ranges]
        return d


def _shift(iso: str, days: int) -> str:
    try:
        return (date.fromisoformat(iso) + timedelta(days=days)).isoformat()
    except ValueError:
        return iso


@dataclass
class _Candidate:
    """One cached cube, and what it can and cannot answer."""
    label: str
    dimensions: List[str]
    grain: List[str]
    columns: List[str]
    non_additive: List[str]
    filters: Dict[str, List[str]]
    time_column: str
    time_start: str
    time_end: str
    truncated: bool
    row_count: int

    missing_dimensions: List[str] = field(default_factory=list)
    missing_measures: List[str] = field(default_factory=list)
    missing_time_ranges: List[Tuple[str, str]] = field(default_factory=list)
    blocker: str = ""            # a hard no: non-additive roll-up, wider slice, truncation

    @property
    def sufficient(self) -> bool:
        return not (self.blocker or self.missing_dimensions
                    or self.missing_measures or self.missing_time_ranges)


class DataManager:
    def __init__(self, cache, workspace, settings) -> None:
        self.cache = cache
        self.workspace = workspace
        self.settings = settings

    # -- measure bookkeeping -------------------------------------------------
    @staticmethod
    def _required_columns(m: CubeMeasure) -> List[str]:
        """An averaged measure is stored as a sum and a count, so reading it back
        needs both."""
        if m.read_expr and f"{m.name}_sum" in m.read_expr:
            return [f"{m.name}_sum", f"{m.name}_count"]
        return [m.name]

    # -- assessment ----------------------------------------------------------
    def assess(self, tenant_id: str, conversation_id: str,
               req: DataRequirement) -> CoverageVerdict:
        frames = self.cache.list_available(tenant_id, conversation_id)

        # Gate 0: same population, or nothing. A cube over a different population
        # is not "close enough to reuse with a caveat" -- reusing it is exactly the
        # silent cross-population comparison this whole design exists to prevent.
        if not req.population_hash:
            return CoverageVerdict(
                decision="retrieve",
                reason="this turn has no base view, so it has no population to match "
                       "against anything already in the workspace")
        same_population = [f for f in frames
                           if f.get("population_hash") == req.population_hash]
        if not same_population:
            others = {f.get("base_view") or "?" for f in frames}
            detail = (f" (the workspace holds {sorted(others)}, over a different "
                      f"population)" if frames else "")
            return CoverageVerdict(
                decision="retrieve",
                reason=f"nothing in the workspace was computed over the "
                       f"{req.base_view!r} population{detail}")

        candidates = [self._evaluate(f, req) for f in same_population]

        sufficient = [c for c in candidates if c.sufficient]
        if sufficient:
            best = self._pick(sufficient)
            return CoverageVerdict(decision="reuse", label=best.label,
                                   reason=self._reuse_reason(best, req))

        # Only dimension or measure gaps, over the same population -> widen: re-run
        # the cube over the same base with the union of old and new dimensions.
        widenable = [c for c in candidates
                     if not c.blocker and (c.missing_dimensions or c.missing_measures
                                           or c.missing_time_ranges)]
        if widenable:
            best = self._pick(widenable)
            return CoverageVerdict(
                decision="widen", label=best.label,
                missing_dimensions=best.missing_dimensions,
                missing_measures=best.missing_measures,
                missing_time_ranges=best.missing_time_ranges,
                # Both share a population and the wider cube sums down to the
                # narrower, so no answer already given from it is invalidated.
                supersedes=best.label,
                reason=self._widen_reason(best))

        blocked = next((c for c in candidates if c.blocker), None)
        return CoverageVerdict(
            decision="retrieve",
            reason=(blocked.blocker if blocked else
                    "the workspace does not cover this requirement"))

    def _evaluate(self, frame: Dict[str, Any], req: DataRequirement) -> _Candidate:
        c = _Candidate(
            label=frame.get("label", ""),
            dimensions=list(frame.get("dimensions") or []),
            grain=list(frame.get("grain") or []),
            columns=list(frame.get("columns") or []),
            non_additive=list(frame.get("non_additive") or []),
            filters=dict(frame.get("filters") or {}),
            time_column=frame.get("time_column") or "",
            time_start=frame.get("time_start") or "",
            time_end=frame.get("time_end") or "",
            truncated=bool(frame.get("truncated")),
            row_count=int(frame.get("row_count") or 0))

        # Rule 1: containment. Cube dimensions when there are any; otherwise this
        # is a keyset ID-grain extract and the original grain rule applies to it
        # unchanged -- finer stored grain is reusable, coarser is not.
        if c.dimensions or req.dimensions:
            c.missing_dimensions = [d for d in req.dimensions if d not in c.dimensions]
        elif not set(req.grain) <= set(c.grain):
            c.blocker = (f"{c.label} is stored at {c.grain}, coarser than the "
                         f"{req.grain} this question needs -- the detail is gone")
            return c

        # A filter on a column the cube does not carry is also a miss: you cannot
        # filter on what you did not GROUP BY.
        for column in req.filters:
            if c.dimensions and column not in c.dimensions and column not in c.missing_dimensions:
                c.missing_dimensions.append(column)

        # Rule 2: the additivity gate, which is what makes rule 1 safe. Rolling a
        # cube up to fewer dimensions is only valid when every measure the
        # requirement asks for is additive in it.
        rolling_up = c.dimensions and set(req.dimensions) < set(c.dimensions)
        if rolling_up:
            offenders = [m.name for m in req.measures if m.name in c.non_additive]
            if offenders:
                c.blocker = (
                    f"{c.label} carries {', '.join(offenders)}, which is a distinct "
                    f"count or percentile and cannot be rolled up to "
                    f"{req.dimensions or 'fewer dimensions'} -- re-querying")
                return c
        # An AVG column in a cube manifest is a Task 7 bug, not something to
        # quietly roll up.
        avg_columns = [col for col in c.columns if col.lower().startswith("avg(")]
        if avg_columns:
            logger.error("cube %s stores raw averages %s -- compose_cube should have "
                         "rewritten these to a sum and a count", c.label, avg_columns)

        # Rule 3: measures present.
        for m in req.measures:
            if not all(col in c.columns for col in self._required_columns(m)):
                c.missing_measures.append(m.name)

        # Rule 4: slice satisfiable. Narrower is fine (filter the surplus rows
        # locally); wider is not, because those rows were never fetched.
        narrower_slice = False
        for column, cube_values in c.filters.items():
            wanted = req.filters.get(column)
            if wanted is None or not set(wanted) <= set(cube_values):
                c.blocker = (
                    f"{c.label} was fetched with {column} restricted to "
                    f"{sorted(cube_values)}; this question needs rows outside that")
                return c
            if set(wanted) < set(cube_values):
                narrower_slice = True
        if any(col not in c.filters for col in req.filters):
            narrower_slice = True

        # Rule 5: time covered.
        if req.time_column and req.time_start and req.time_end:
            if not (c.time_start and c.time_end):
                c.missing_time_ranges = [(req.time_start, req.time_end)]
            else:
                if req.time_start < c.time_start:
                    c.missing_time_ranges.append(
                        (req.time_start, _shift(c.time_start, -1)))
                if req.time_end > c.time_end:
                    c.missing_time_ranges.append((_shift(c.time_end, 1), req.time_end))

        # Rule 6: a truncated cube dropped cells at the ceiling, so totals and
        # rates over the whole population are wrong. Only a strictly narrower
        # slice is safe.
        if c.truncated and not narrower_slice:
            c.blocker = (f"{c.label} was truncated at its row ceiling, so it cannot "
                         f"answer a question about the whole population")
        return c

    @staticmethod
    def _pick(candidates: Sequence[_Candidate]) -> _Candidate:
        """Rule 8 before rule 9. A cube whose dimensions strictly contain another
        candidate's supersedes it and is preferred even when the narrower one is
        smaller -- keeping answers on the widest available cube is what keeps
        later follow-ups local. Rule 9 (fewest rows, then most recent) breaks ties
        only among cubes that are not in a supersedes relationship."""
        maximal = [c for c in candidates
                   if not any(set(c.dimensions) < set(other.dimensions)
                              for other in candidates)]
        pool = maximal or list(candidates)
        return sorted(pool, key=lambda c: (c.row_count, c.label))[0]

    @staticmethod
    def _reuse_reason(c: _Candidate, req: DataRequirement) -> str:
        shape = " x ".join(c.dimensions) if c.dimensions else " + ".join(c.grain)
        window = (f", {c.time_start}..{c.time_end}"
                  if c.time_start and c.time_end else "")
        rolled = ("" if set(req.dimensions) == set(c.dimensions)
                  else f" -- {', '.join(req.dimensions) or 'the total'} rolls up from it")
        note = (" (truncated, but this question is a strictly narrower slice of it)"
                if c.truncated else "")
        return (f"reused {c.label} ({shape}{window}, {c.row_count:,} cells)"
                f"{rolled}{note}")

    @staticmethod
    def _widen_reason(c: _Candidate) -> str:
        gaps = []
        if c.missing_dimensions:
            gaps.append(", ".join(c.missing_dimensions))
        if c.missing_measures:
            gaps.append(", ".join(c.missing_measures))
        if c.missing_time_ranges:
            gaps.append(" and ".join(f"{a}..{b}" for a, b in c.missing_time_ranges))
        widened = " x ".join(sorted(set(c.dimensions) | set(c.missing_dimensions)))
        return (f"{c.label} covers this population but not {'; '.join(gaps)}; "
                f"widening to {widened or 'the same cut'}")


__all__ = ["DataManager", "DataRequirement", "CoverageVerdict"]
