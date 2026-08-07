"""
LLM Product Analyst Reasoning Layer
Acts as an experienced Senior Product Analyst to evaluate query results,
diagnose anomalies, provide hypotheses, and recommend next actions.
"""
import os
import json
import logging
from typing import Dict, Any, Optional, List
import pandas as pd

from core.llm_gateway import LLMGateway
from core.profiler.base import ProfilerResult
from core.rules.models import RuleEvaluationResult

logger = logging.getLogger(__name__)


class ProductAnalystAgent:
    """Senior Product Analyst AI Agent that interprets data, stats, and business rules."""

    def __init__(
        self,
        provider: Optional[str] = None,
        model_name: Optional[str] = None,
        api_key: Optional[str] = None,
        ollama_url: Optional[str] = None,
    ):
        self._provider = provider or "OpenRouter API"
        self._model = model_name or "deepseek/deepseek-v4-flash-0731"
        self._api_key = api_key or ""
        self._ollama_url = ollama_url or "http://127.0.0.1:11434"

    def analyze_results(
        self,
        question: str,
        sql: str,
        profiler_result: ProfilerResult,
        rule_result: RuleEvaluationResult,
        glossary_context: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.2,
    ) -> Dict[str, Any]:
        """
        Synthesizes question, SQL, profiling stats, and business observations into an analyst briefing.
        """
        # Format triggered observations for prompt
        observations_payload = []
        for obs in rule_result.observations:
            observations_payload.append({
                "rule_id": obs.rule_id,
                "rule_name": obs.rule_name,
                "severity": obs.severity.value,
                "target": obs.target_column,
                "observation": obs.observation_text,
                "hypotheses": obs.hypotheses,
                "recommended_checks": obs.recommended_checks
            })

        system_prompt = (
            "You are a Senior Staff Product Analyst & Analytics Architect at a world-class technology company.\n"
            "Your task is NOT to merely repeat basic statistics. Your task is to think critically like a seasoned human product analyst:\n"
            "1. Answer the executive business question directly with clarity and numbers.\n"
            "2. Evaluate dataset health and identify subtle business or telemetry anomalies.\n"
            "3. Formulate concrete hypotheses when anomalies occur (e.g. Is it an SQL denominator filtering issue? A telemetry drop? Or genuine consumer behavior?).\n"
            "4. Recommend the top 3 highest-leverage next actions or follow-up investigations.\n\n"
            "CRITICAL: Output your analysis in strictly valid JSON format with the following keys:\n"
            "{\n"
            '  "executive_summary": "Succinct 2-3 sentence executive answer to the business question.",\n'
            '  "key_findings": ["Bullet 1 with exact numbers", "Bullet 2", "Bullet 3"],\n'
            '  "data_health_status": "CLEAN" | "WARNINGS_DETECTED" | "CRITICAL_ANOMALIES",\n'
            '  "anomaly_diagnostics": [\n'
            '    {\n'
            '      "title": "Anomaly title",\n'
            '      "severity": "CRITICAL" | "HIGH" | "WARNING" | "INFO",\n'
            '      "observed_pattern": "What looks unusual in the data",\n'
            '      "plausible_hypotheses": ["Hypothesis A (SQL bug)", "Hypothesis B (Instrumentation)", "Hypothesis C (Real behavior)"],\n'
            '      "recommended_verification": "Exact check or SQL slice to prove/disprove"\n'
            '    }\n'
            '  ],\n'
            '  "suggested_investigations": [\n'
            '    {\n'
            '      "title": "Short title",\n'
            '      "action_type": "LOCAL_FILTER" | "LOCAL_PIVOT" | "NEW_SQL_QUERY" | "INSTRUMENTATION_CHECK",\n'
            '      "description": "What to do and why it is valuable",\n'
            '      "priority": "HIGH" | "MEDIUM"\n'
            '    }\n'
            '  ]\n'
            "}"
        )

        user_content = (
            f"### 🎯 Business Question:\n{question}\n\n"
            f"### 📝 Generated SQL Query:\n```sql\n{sql}\n```\n\n"
            f"### 📊 Dataset Fast Profiling Summary ({profiler_result.row_count} rows, {profiler_result.col_count} columns):\n"
            f"```json\n{profiler_result.summary_json_str}\n```\n\n"
            f"### 📋 Triggered Business Validation Rules ({len(observations_payload)} observations):\n"
            f"```json\n{json.dumps(observations_payload, indent=2)}\n```\n\n"
        )
        if glossary_context:
            user_content += f"### 🧠 Domain Knowledge Graph Context:\n{glossary_context}\n\n"

        user_content += "Please produce the structured Product Analyst reasoning JSON now."

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]

        try:
            _key = api_key or self._api_key
            gw_res = LLMGateway.generate(
                messages=messages,
                provider=self._provider,
                model=self._model,
                api_key=_key,
                temperature=temperature,
                ollama_url=self._ollama_url,
            )
            raw_response = gw_res.get("text", "")
            
            # Extract JSON block
            clean_json = raw_response.strip()
            if "```json" in clean_json:
                clean_json = clean_json.split("```json")[1].split("```")[0].strip()
            elif "```" in clean_json:
                clean_json = clean_json.split("```")[1].split("```")[0].strip()

            parsed = json.loads(clean_json)
            parsed["raw_llm_response"] = raw_response
            return parsed
        except Exception as e:
            logger.warning(f"Product Analyst reasoning parsing error: {e}")
            return {
                "executive_summary": f"Query returned {profiler_result.row_count} rows. Analysis could not be parsed into JSON format.",
                "key_findings": [f"Evaluated {profiler_result.col_count} columns across {profiler_result.row_count} records."],
                "data_health_status": "WARNINGS_DETECTED" if observations_payload else "CLEAN",
                "anomaly_diagnostics": [
                    {
                        "title": obs.get("rule_name", "Observation"),
                        "severity": obs.get("severity", "WARNING"),
                        "observed_pattern": obs.get("observation", ""),
                        "plausible_hypotheses": obs.get("hypotheses", []),
                        "recommended_verification": (obs.get("recommended_checks") or ["Inspect raw records."])[0]
                    } for obs in observations_payload
                ],
                "suggested_investigations": [
                    {
                        "title": "Explore In-Memory Data",
                        "action_type": "LOCAL_FILTER",
                        "description": "Slice and group dataset using conversational chat.",
                        "priority": "HIGH"
                    }
                ],
                "raw_llm_response": str(e)
            }

    @staticmethod
    def get_critical_anomalies(briefing: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Filters analyst briefing anomaly diagnostics to only CRITICAL and HIGH severity items.

        Returns a list of dicts with:
          - title, severity, observed_pattern, plausible_hypotheses, recommended_verification
          - feedback_text: formatted string ready to pass to AutoHealer as the "error_message"

        Example feedback_text:
          "BUSINESS_ANOMALY — 100% Conversion Anomaly [CRITICAL]:
           Observed: Month 2026-03 shows 100.0% appointment completion rate.
           Root cause hypothesis: The SQL denominator (appointment_views) may be
           filtered to only completed sessions, making the ratio always 100%.
           Recommended fix: Ensure denominator counts ALL session views, not just completed ones."
        """
        actionable = []
        critical_severities = {"CRITICAL", "HIGH"}
        for diag in briefing.get("anomaly_diagnostics", []):
            sev = (diag.get("severity") or "").upper()
            if sev not in critical_severities:
                continue
            top_hypothesis = (diag.get("plausible_hypotheses") or ["Unknown root cause"])[0]
            rec = diag.get("recommended_verification", "Inspect raw SQL denominator logic.")
            feedback_text = (
                f"BUSINESS_ANOMALY — {diag.get('title', 'Anomaly')} [{sev}]:\n"
                f"Observed pattern: {diag.get('observed_pattern', '')}\n"
                f"Top root-cause hypothesis: {top_hypothesis}\n"
                f"Recommended SQL correction: {rec}"
            )
            actionable.append({**diag, "feedback_text": feedback_text})
        return actionable
