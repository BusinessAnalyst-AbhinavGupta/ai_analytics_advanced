"""
Declarative Business Rule Engine
Evaluates YAML-defined domain rules over query results & dataset profiling.
"""
import os
import re
import yaml
import logging
from typing import Dict, Any, List, Optional
import pandas as pd
from datetime import datetime, timezone

from core.rules.models import Observation, RuleEvaluationResult, Severity

logger = logging.getLogger(__name__)
DEFAULT_RULES_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config", "business_rules.yaml")


class BusinessRuleEngine:
    """Evaluates domain-specific business validation rules over DataFrames."""

    def __init__(self, rules_path: str = DEFAULT_RULES_PATH):
        self.rules_path = os.path.abspath(rules_path)
        self.rules: List[Dict[str, Any]] = self._load_rules()

    def _load_rules(self) -> List[Dict[str, Any]]:
        """Loads business rules from YAML file or returns core defaults."""
        if os.path.exists(self.rules_path):
            try:
                with open(self.rules_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                    return data.get("rules", [])
            except Exception as e:
                logger.warning(f"Failed to load business rules from {self.rules_path}: {e}")
        return self._default_rules()

    @staticmethod
    def _default_rules() -> List[Dict[str, Any]]:
        return [
            {
                "id": "BR_PERFECT_CONVERSION",
                "name": "100% Conversion Anomaly",
                "category": "conversion",
                "metric_pattern": ".*(?:conversion|completed_pct|completion_rate|view_to_complete|rate|pct).*",
                "condition_type": "column_value_boundary",
                "condition": "value >= 100.0",
                "severity": "WARNING",
                "observation": "A conversion or completion rate of exactly 100% was observed.",
                "hypotheses": [
                    "The denominator was filtered in SQL WHERE clause to only successful sessions.",
                    "The step is mandatory or auto-completed in the user flow.",
                    "Join duplication between event and completion tables inflated downstream step."
                ],
                "recommended_checks": [
                    "Check if the base query filters out sessions that did not complete.",
                    "Verify join keys (e.g. session_id AND date) to avoid Cartesian joins."
                ]
            }
        ]

    def evaluate(self, df: pd.DataFrame) -> RuleEvaluationResult:
        """
        Evaluate all loaded business rules against a DataFrame.
        """
        if df is None or df.empty:
            return RuleEvaluationResult(total_rules_evaluated=0, triggered_count=0)

        observations: List[Observation] = []
        total_evaluated = 0

        for rule in self.rules:
            total_evaluated += 1
            rule_id = rule.get("id", "UNKNOWN_RULE")
            rule_name = rule.get("name", "Domain Rule")
            category = rule.get("category", "general")
            sev_str = rule.get("severity", "MEDIUM").upper()
            severity = getattr(Severity, sev_str, Severity.MEDIUM)
            cond_type = rule.get("condition_type", "column_value_boundary")

            try:
                # ── Rule Type 1: Column Value Boundary ────────────────────────
                if cond_type == "column_value_boundary":
                    metric_pattern = rule.get("metric_pattern", ".*")
                    cond_expr = rule.get("condition", "False")

                    matching_cols = [c for c in df.columns if re.match(metric_pattern, c, re.IGNORECASE)]
                    for col in matching_cols:
                        if not pd.api.types.is_numeric_dtype(df[col]):
                            continue
                        
                        clean_s = df[col].dropna()
                        if clean_s.empty:
                            continue

                        # Evaluate condition (e.g. value >= 100.0 or value < 0)
                        if ">= 100" in cond_expr:
                            triggered = (clean_s >= 100.0).any()
                            violating_vals = clean_s[clean_s >= 100.0].tolist()
                        elif "== 0" in cond_expr:
                            triggered = (clean_s == 0.0).any()
                            violating_vals = clean_s[clean_s == 0.0].tolist()
                        elif "< 0" in cond_expr:
                            triggered = (clean_s < 0).any()
                            violating_vals = clean_s[clean_s < 0].tolist()
                        elif "> 100" in cond_expr:
                            triggered = (clean_s > 100.0).any()
                            violating_vals = clean_s[clean_s > 100.0].tolist()
                        else:
                            triggered = False
                            violating_vals = []

                        if triggered:
                            obs = Observation(
                                rule_id=rule_id,
                                rule_name=rule_name,
                                category=category,
                                severity=severity,
                                target_column=col,
                                observation_text=f"{rule.get('observation', 'Boundary anomaly')} in column '{col}'. Triggering values: {violating_vals[:3]}",
                                context_data={
                                    "column": col,
                                    "sample_trigger_values": violating_vals[:5],
                                    "total_violating_rows": len(violating_vals)
                                },
                                hypotheses=rule.get("hypotheses", []),
                                recommended_checks=rule.get("recommended_checks", []),
                                confidence="HIGH" if len(violating_vals) > 1 else "MEDIUM"
                            )
                            observations.append(obs)

                # ── Rule Type 2: Funnel Sequence Monotonicity ─────────────────
                elif cond_type == "funnel_sequence":
                    # Look for standard sequential column pairs (e.g. views vs completed, or start vs finish)
                    cols_lower = {c.lower(): c for c in df.columns}
                    funnel_pairs = [
                        ("views", "completed"),
                        ("appointment_views", "appointment_completions"),
                        ("product_views", "add_to_cart"),
                        ("add_to_cart", "checkout"),
                        ("checkout", "purchase"),
                        ("started", "completed")
                    ]
                    for up_name, down_name in funnel_pairs:
                        if up_name in cols_lower and down_name in cols_lower:
                            up_col = cols_lower[up_name]
                            down_col = cols_lower[down_name]
                            if pd.api.types.is_numeric_dtype(df[up_col]) and pd.api.types.is_numeric_dtype(df[down_col]):
                                violations = (df[down_col] > df[up_col])
                                if violations.any():
                                    count_v = int(violations.sum())
                                    obs = Observation(
                                        rule_id=rule_id,
                                        rule_name=rule_name,
                                        category=category,
                                        severity=severity,
                                        target_column=f"{up_col} -> {down_col}",
                                        observation_text=f"Funnel inversion detected: downstream step '{down_col}' has higher count than upstream '{up_col}' in {count_v} row(s).",
                                        context_data={"upstream": up_col, "downstream": down_col, "violating_rows": count_v},
                                        hypotheses=rule.get("hypotheses", []),
                                        recommended_checks=rule.get("recommended_checks", []),
                                        confidence="HIGH"
                                    )
                                    observations.append(obs)

                # ── Rule Type 3: Null Ratio Threshold ────────────────────────
                elif cond_type == "null_ratio_threshold":
                    thresh = float(rule.get("threshold", 0.30))
                    for col in df.columns:
                        null_ratio = float(df[col].isnull().mean())
                        if null_ratio >= thresh:
                            obs = Observation(
                                rule_id=rule_id,
                                rule_name=rule_name,
                                category=category,
                                severity=severity,
                                target_column=col,
                                observation_text=f"Column '{col}' has {null_ratio*100:.1f}% NULL values (threshold: {thresh*100:.0f}%).",
                                context_data={"column": col, "null_ratio": null_ratio},
                                hypotheses=rule.get("hypotheses", []),
                                recommended_checks=rule.get("recommended_checks", []),
                                confidence="HIGH"
                            )
                            observations.append(obs)

                # ── Rule Type 4: Temporal Boundaries ──────────────────────────
                elif cond_type == "temporal_boundary":
                    now_utc = datetime.now(timezone.utc)
                    for col in df.columns:
                        if pd.api.types.is_datetime64_any_dtype(df[col]):
                            clean_dt = df[col].dropna()
                            # Check if timestamps are significantly in future (>1 day)
                            future_mask = clean_dt > pd.Timestamp.now() + pd.Timedelta(days=1)
                            if future_mask.any():
                                obs = Observation(
                                    rule_id=rule_id,
                                    rule_name=rule_name,
                                    category=category,
                                    severity=severity,
                                    target_column=col,
                                    observation_text=f"Column '{col}' contains future timestamps ({future_mask.sum()} records).",
                                    context_data={"column": col, "future_count": int(future_mask.sum())},
                                    hypotheses=rule.get("hypotheses", []),
                                    recommended_checks=rule.get("recommended_checks", []),
                                    confidence="HIGH"
                                )
                                observations.append(obs)

            except Exception as eval_err:
                logger.warning(f"Error evaluating rule {rule_id}: {eval_err}")

        crit_count = sum(1 for o in observations if o.severity == Severity.CRITICAL)
        warn_count = sum(1 for o in observations if o.severity in [Severity.HIGH, Severity.WARNING])
        info_count = sum(1 for o in observations if o.severity in [Severity.MEDIUM, Severity.INFO])

        return RuleEvaluationResult(
            total_rules_evaluated=total_evaluated,
            triggered_count=len(observations),
            observations=observations,
            critical_count=crit_count,
            warning_count=warn_count,
            info_count=info_count
        )
