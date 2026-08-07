"""Interpretation/analysis: profile + business rules + answer framing.

Wraps the existing FastSummaryProfiler and BusinessRuleEngine so the platform gets
fast, deterministic profiling and rule-based anomaly detection. The pipeline then
frames an answer (facts vs hypotheses vs uncertainties vs next actions) with the
required discipline: LLM text is never an observed fact without an evidence link.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional

import pandas as pd

from core.profiler.fast_summary import FastSummaryProfiler
from core.rules.engine import BusinessRuleEngine


def profile_df(df: pd.DataFrame, title: str = "Analysis") -> Dict[str, Any]:
    p = FastSummaryProfiler().profile(pd.DataFrame(df) if df is not None else pd.DataFrame())
    d: Dict[str, Any] = {
        "row_count": p.row_count,
        "col_count": p.col_count,
        "summary": p.summary_dict,
        "duration_seconds": p.duration_seconds,
        "quality_warnings": p.quality_warnings,
    }
    if not df.empty:
        num_cols = {k: v for k, v in p.summary_dict.items() if isinstance(v, dict)}
        d["numeric_columns"] = num_cols
    return d


def evaluate_rules(df: pd.DataFrame) -> List[Dict[str, Any]]:
    res = BusinessRuleEngine().evaluate(df)
    out = []
    for o in getattr(res, "observations", []):
        out.append({
            "rule_id": o.rule_id, "rule_name": o.rule_name, "category": o.category,
            "severity": o.severity.value if hasattr(o.severity, "value") else str(o.severity),
            "target_column": o.target_column, "observation": o.observation_text,
            "hypotheses": list(o.hypotheses), "recommended_checks": list(o.recommended_checks),
            "confidence": o.confidence,
        })
    return out


def frame_answer(df: pd.DataFrame, sql: str, question: str, *,
                 rules: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Deterministic answer framing; LLM can enrich this but never replace it."""
    rules = rules if rules is not None else evaluate_rules(df)
    facts: List[str] = []
    if not df.empty:
        facts.append(f"Query returned {len(df)} row(s) with {len(df.columns)} column(s).")
        for col in df.columns:
            if pd.api.types.is_numeric_dtype(df[col]):
                s = df[col]
                facts.append(f"Column `{col}`: min={s.min()!s}, max={s.max()!s}, mean={round(s.mean(), 3) if not s.empty else 'n/a'}.")
                break
    anomalies = [r for r in rules if r["severity"] in ("CRITICAL", "HIGH", "WARNING")]
    hypotheses = []
    for r in anomalies:
        h = (r.get("hypotheses") or ["Unknown root cause"])[0]
        hypotheses.append(f"[{r['severity']}] {r['observation']} -> candidate cause: {h}")
    uncertainties = []
    if not df.empty and len(df) == 0:
        uncertainties.append("Result set is empty.")
    next_actions = []
    if anomalies:
        next_actions.append("Route to senior review — anomaly detected, do not auto-promote.")
    else:
        next_actions.append("Candidate finding ready for senior review before promotion to Brain.")
    return {
        "facts": facts,
        "hypotheses": hypotheses,
        "uncertainties": uncertainties,
        "next_actions": next_actions,
        "anomalies": anomalies,
        "summary": _summarize(facts, anomalies, question),
    }


def _summarize(facts: List[str], anomalies: List[Any], question: str) -> str:
    lines = [f"Question: {question}"]
    lines += facts[:4]
    if anomalies:
        lines.append(f"{len(anomalies)} rule trigger(s) detected — needs review.")
    return " | ".join(lines)