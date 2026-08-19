"""The semantic layer: what a metric MEANS, in front of every prompt.

This is the difference between a *technically valid* query and an *analytically
correct* one. A schema says `funnel_events.status` exists and holds 'completed'.
It does not say that conversion rate is completed/eligible, is measured at
session grain, may be sliced by country/device/channel/date, and excludes test
traffic. Nothing in the platform stopped the analyst computing conversion at the
wrong grain, or forgetting the exclusion -- and a wrong number delivered
confidently is worse than no number.

It lives in the Company Brain rather than a new store or a YAML file outside
review: NodeKind.METRIC already exists, the CANDIDATE -> submit -> approve flow
already exists, and a metric definition is exactly the kind of fact that should
pass a senior's review before it silently reshapes every answer. What was
missing was *structure* on the payload.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .database import dump_json
from .domain import (KnowledgeNode, NodeKind, ReviewStatus, SemanticDimension,
                     SemanticMetric)

logger = logging.getLogger(__name__)

METRIC_TITLE_PREFIX = "Metric: "
DIMENSION_TITLE_PREFIX = "Dimension: "

# Measure-shaped words. A question containing one of these with no matching
# approved metric is an uncertainty the answer has to declare, not paper over.
MEASURE_TERMS = (
    "rate", "conversion", "churn", "retention", "margin", "aov", "arpu", "ltv",
    "cac", "yield", "uplift", "attach", "utilisation", "utilization", "nps",
    "abandonment", "throughput", "velocity", "payback",
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> List[str]:
    """Lowercase word parts, splitting on punctuation AND underscores.

    Splitting `conversion_rate` into {conversion, rate} is what lets the stored
    metric name match a stakeholder who wrote "our conversion rate", and what
    stops "rate" being reported as an undefined measure when a metric already
    defines it. Matching is then whole-token subset, so "reconversion" is not a
    match for "conversion".
    """
    return _TOKEN_RE.findall((text or "").lower())


@dataclass
class SemanticResolution:
    metrics: List[SemanticMetric] = field(default_factory=list)
    dimensions: List[SemanticDimension] = field(default_factory=list)
    required_filters: List[str] = field(default_factory=list)
    caveats: List[str] = field(default_factory=list)
    unresolved_terms: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"metrics": [asdict(m) for m in self.metrics],
                "dimensions": [asdict(d) for d in self.dimensions],
                "required_filters": list(self.required_filters),
                "caveats": list(self.caveats),
                "unresolved_terms": list(self.unresolved_terms)}


class SemanticLayer:
    def __init__(self, brain_for: Callable[[str], Any]) -> None:
        self.brain_for = brain_for

    # -- read ---------------------------------------------------------------
    def _typed_nodes(self, tenant_id: str, kind: NodeKind, prefix: str,
                     builder, approved_only: bool) -> List[Any]:
        out = []
        for node in self.brain_for(tenant_id).all(kind=kind, limit=1000):
            if not node.title.startswith(prefix):
                continue
            if approved_only and not node.status.is_usable():
                continue
            try:
                out.append(builder(node.payload))
            except (TypeError, ValueError) as exc:
                # A hand-edited node must never take a chat turn down, and must
                # never be half-loaded into a prompt either.
                logger.warning("skipping malformed semantic node %s (%s): %s",
                               node.id, node.title, exc)
        return out

    def metrics(self, tenant_id: str, approved_only: bool = True) -> List[SemanticMetric]:
        return self._typed_nodes(tenant_id, NodeKind.METRIC, METRIC_TITLE_PREFIX,
                                 SemanticMetric.from_dict, approved_only)

    def dimensions(self, tenant_id: str, approved_only: bool = True) -> List[SemanticDimension]:
        return self._typed_nodes(tenant_id, NodeKind.DEFINITION, DIMENSION_TITLE_PREFIX,
                                 SemanticDimension.from_dict, approved_only)

    # -- write ---------------------------------------------------------------
    def _find(self, tenant_id: str, title: str) -> Optional[KnowledgeNode]:
        kind = NodeKind.METRIC if title.startswith(METRIC_TITLE_PREFIX) else NodeKind.DEFINITION
        return next((n for n in self.brain_for(tenant_id).all(kind=kind, limit=1000)
                     if n.title == title), None)

    def _upsert(self, tenant_id: str, kind: NodeKind, title: str, summary: str,
                payload: Dict[str, Any], by: str) -> KnowledgeNode:
        brain = self.brain_for(tenant_id)
        node = self._find(tenant_id, title)
        if node is not None:
            # In place, not a second node. update_field re-syncs the search index.
            brain.update_field(node.id, "payload", dump_json(payload))
            return brain.update_field(node.id, "summary", summary)
        # Created unapproved: a definition that reshapes every answer touching it
        # earns a human's review first.
        return brain.create(kind=kind, title=title, summary=summary, payload=payload,
                            created_by=by, status=ReviewStatus.CANDIDATE)

    def upsert_metric(self, tenant_id: str, m: SemanticMetric, by: str) -> KnowledgeNode:
        summary = f"{m.name}: {m.definition} (at {', '.join(m.grain) or 'unstated grain'})"
        return self._upsert(tenant_id, NodeKind.METRIC, f"{METRIC_TITLE_PREFIX}{m.name}",
                            summary, asdict(m), by)

    def upsert_dimension(self, tenant_id: str, d: SemanticDimension, by: str) -> KnowledgeNode:
        summary = d.description or f"{d.name} -> {d.column}"
        return self._upsert(tenant_id, NodeKind.DEFINITION,
                            f"{DIMENSION_TITLE_PREFIX}{d.name}", summary, asdict(d), by)

    # -- resolve -------------------------------------------------------------
    def resolve(self, tenant_id: str, question: str) -> SemanticResolution:
        """Match the question against approved metrics by name and alias, plus the
        Brain's own hybrid recall. Matching is lexical and deliberately dumb -- a
        second embedding path here would duplicate BrainIndex's fused recall."""
        metrics = self.metrics(tenant_id, approved_only=True)
        question_tokens = set(_tokens(question))

        matched: Dict[str, SemanticMetric] = {}
        for m in metrics:
            for label in [m.name, *m.aliases]:
                label_tokens = _tokens(label)
                # Whole-token match: 'reconversion' must not match 'conversion'.
                if label_tokens and set(label_tokens) <= question_tokens:
                    matched[m.name] = m
                    break

        by_name = {m.name: m for m in metrics}
        try:
            for node in self.brain_for(tenant_id).search(
                    question, kind=NodeKind.METRIC, usable_only=True, limit=3):
                name = node.payload.get("name")
                if name in by_name:
                    matched.setdefault(name, by_name[name])
        except Exception as exc:  # noqa: BLE001 - recall is additive, never required
            logger.warning("semantic recall failed for tenant %s: %s", tenant_id, exc)

        ordered = [m for m in metrics if m.name in matched]

        wanted_dimensions = {d for m in ordered for d in m.dimensions}
        dimensions = [d for d in self.dimensions(tenant_id, approved_only=True)
                      if d.name in wanted_dimensions or set(_tokens(d.name)) <= question_tokens]

        required_filters, caveats = [], []
        for m in ordered:
            for f in m.filters:
                if f not in required_filters:
                    required_filters.append(f)
            for c in m.caveats:
                if c not in caveats:
                    caveats.append(c)

        # The uncertainty signal: a measure-shaped word with no approved metric.
        defined_tokens = {t for m in metrics for label in [m.name, *m.aliases]
                          for t in _tokens(label)}
        unresolved = [t for t in MEASURE_TERMS
                      if t in question_tokens and t not in defined_tokens]

        return SemanticResolution(metrics=ordered, dimensions=dimensions,
                                  required_filters=required_filters, caveats=caveats,
                                  unresolved_terms=unresolved)

    # -- render --------------------------------------------------------------
    def render(self, res: SemanticResolution) -> str:
        """The block that goes into every prompt, ahead of the schema."""
        if not (res.metrics or res.dimensions or res.unresolved_terms):
            return ""
        lines = ["=== BUSINESS SEMANTICS (authoritative -- these definitions "
                 "override your own assumptions) ===", ""]
        for m in res.metrics:
            alias_note = f"  (aliases: {', '.join(m.aliases)})" if m.aliases else ""
            lines.append(f"METRIC {m.name}{alias_note}")
            lines.append(f"  Definition : {m.definition}")
            lines.append(f"  Grain      : {', '.join(m.grain) or 'unstated'}")
            lines.append(f"  Dimensions : {', '.join(m.dimensions) or 'unstated'}")
            lines.append(f"  Source     : {', '.join(m.source_tables) or 'unstated'}")
            for f in m.filters:
                lines.append(f"  ALWAYS APPLY: {f}")
            if m.caveats:
                lines.append(f"  Caveats    : {'; '.join(m.caveats)}")
            if m.freshness:
                lines.append(f"  Freshness  : {m.freshness}")
            lines.append("")
        for d in res.dimensions:
            desc = f"  -- {d.description}" if d.description else ""
            lines.append(f"DIMENSION {d.name} -> {d.column}{desc}")
        if res.dimensions:
            lines.append("")
        if res.unresolved_terms:
            lines.append("NO APPROVED DEFINITION for: " + ", ".join(res.unresolved_terms))
            lines.append("")
        lines.extend([
            "RULES:",
            "- Compute a metric only at its stated grain. If the question needs it at a",
            "  different grain, say so rather than silently re-deriving it.",
            "- Every filter under ALWAYS APPLY is mandatory in every query touching that",
            "  metric, whether or not the user mentioned it.",
            "- Slice only by the dimensions listed for that metric.",
            "- If a measure in the question has no metric defined here, say so explicitly",
            "  in your rationale. Do not invent a definition.",
            "",
        ])
        return "\n".join(lines)


__all__ = ["SemanticLayer", "SemanticResolution", "MEASURE_TERMS",
           "METRIC_TITLE_PREFIX", "DIMENSION_TITLE_PREFIX"]
