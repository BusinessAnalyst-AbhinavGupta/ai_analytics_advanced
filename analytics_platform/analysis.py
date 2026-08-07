"""Interpretation/analysis: profile + business rules + answer framing.

Wraps the existing FastSummaryProfiler and BusinessRuleEngine so the platform gets
fast, deterministic profiling and rule-based anomaly detection. The pipeline then
frames an answer (facts vs hypotheses vs uncertainties vs next actions) with the
required discipline: LLM text is never an observed fact without an evidence link.
"""
from __future__ import annotations

from dataclasses import asdict
import re
from typing import Any, Dict, List, Optional

import pandas as pd

from core.profiler.fast_summary import FastSummaryProfiler
from core.rules.engine import BusinessRuleEngine


def _norm_tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _novel(text: str, existing: Optional[List[str]] = None) -> bool:
    """Coarse but honest novelty check against the tenant's approved Brain findings.

    An insight is "novel" when no existing finding shares >=2 content tokens. The
    scope is intentionally the tenant's Brain only — external truth is out of reach.
    """
    if not existing:
        return True
    tokens = _norm_tokens(text)
    for e in existing:
        if len(tokens & _norm_tokens(e)) >= 2:
            return False
    return True


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


def analyze_results(df: pd.DataFrame, sql: str, question: str, *,
                    rules: Optional[List[Dict[str, Any]]] = None,
                    existing: Optional[List[str]] = None,
                    row_limit: int = 50000) -> Dict[str, Any]:
    """Deterministic analysis with the "insight-or-actionable + assumptions"
    discipline (CP-12). Every run yields facts, insights (either novel or already
    covered), actionables, and an explicit assumptions list. LLM may enrich this
    via `synthesize_analysis_llm` but never replaces it.
    """
    rules = rules if rules is not None else evaluate_rules(df)
    facts: List[str] = []
    insights: List[Dict[str, Any]] = []
    assumptions: List[str] = []
    actionables: List[str] = []

    if df is not None and not df.empty:
        facts.append(f"Query returned {len(df)} row(s) with {len(df.columns)} column(s).")
        if row_limit and len(df) >= row_limit:
            assumptions.append(
                f"Result set hit the {row_limit}-row cap; figures may be truncated.")
        assumptions.append(
            "Figures are computed over the rows this read-only query returned from the "
            "connected data source, as of query time.")
        num_cols = 0
        for col in df.columns:
            if pd.api.types.is_numeric_dtype(df[col]):
                s = df[col].dropna()
                if s.empty:
                    continue
                facts.append(f"Column `{col}`: min={s.min()!s}, max={s.max()!s}, "
                             f"mean={round(float(s.mean()), 3)}.")
                insights.append({
                    "text": f"`{col}` ranges {s.min():g} → {s.max():g} "
                            f"(mean {float(s.mean()):.2f}) across {len(df)} rows.",
                    "novel": _novel(f"{col} {col}", existing), "kind": "descriptive"})
                num_cols += 1
        if num_cols == 0:
            assumptions.append(
                "No numeric columns detected; the analysis is structural/count-oriented.")

    anomalies = [r for r in rules if r["severity"] in ("CRITICAL", "HIGH", "WARNING")]
    for r in anomalies:
        cause = (r.get("hypotheses") or ["Unknown root cause"])[0]
        line = f"[{r['severity']}] {r['observation']} -> candidate cause: {cause}"
        fact_col = r.get("target_column", "?")
        insights.append({"text": line, "novel": _novel(r["observation"], existing),
                         "kind": "anomaly"})
        actionables.append(
            f"Investigate `{fact_col}` ({r['severity']}) — confirm root cause before acting.")
        facts.append(line)

    uncertainties: List[str] = []
    if df is None or df.empty:
        uncertainties.append("Result set is empty; no computation beyond row count.")
    elif not anomalies:
        actionables.append("Candidate finding ready for senior review before promotion to Brain.")
    else:
        actionables.append("Route to senior review — anomaly detected, do not auto-promote.")

    if not insights:
        insights.append({"text": "Query completed and returned 0 rows; no insight derivable.",
                         "novel": False, "kind": "descriptive"})

    return {
        "facts": facts,
        "hypotheses": [i["text"] for i in insights if i["kind"] == "anomaly"],
        "uncertainties": uncertainties,
        "next_actions": actionables,
        "actionables": actionables,
        "insights": insights,
        "assumptions": assumptions,
        "anomalies": anomalies,
        "summary": _summarize(facts, anomalies, question),
    }


def synthesize_analysis_llm(profile: Dict[str, Any], rules: List[Dict[str, Any]],
                            question: str, insights: List[Dict[str, Any]],
                            assumptions: List[str], llm: Any) -> Optional[str]:
    """Optional LLM-narrated insight over a *sanitized digest* only.

    Never sends raw rows or SQL — only column summaries, rule triggers, existing
    insight/assumption text and the question. Best-effort: any failure returns None
    and the deterministic analysis remains authoritative.
    """
    if getattr(llm, "name", "null") == "null":
        return None
    digest = "\n".join([
        f"Question: {question}",
        f"Numeric columns: {profile.get('numeric_columns', {})}",
        f"Rule triggers: {rules}",
        f"Insights so far: {insights}",
        f"Assumptions so far: {assumptions}",
    ])[:4000]
    prompt = (
        "Draft one concise, decision-useful analytical insight (2-4 sentences) for a "
        "product analyst at a telecom digital-sales funnel, plus (a) the most important "
        "additional assumption and (b) one concrete next step. Use ONLY the digest — do "
        "not invent numbers. Keep it falsifiable.\n\nDigest:\n"
        f"{digest}"
    )
    try:
        res = llm.generate(prompt=prompt, system_prompt=(
            "You are a senior product-analytics assistant. Be concrete, honest about "
            "assumptions, and separate insight from actionable."), temperature=0.2)
        return res.text.strip() or None
    except Exception:  # noqa: BLE001 - LLM is an additive nicety
        return None