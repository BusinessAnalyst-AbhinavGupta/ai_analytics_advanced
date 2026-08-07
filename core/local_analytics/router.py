"""
Local In-Memory Analytics & Query Router (DuckDB Engine)
Determines whether follow-up questions can be answered from the active DataFrame,
and executes sub-second DuckDB SQL locally to avoid redundant warehouse queries.
"""
import os
import json
import logging
import duckdb
from typing import Dict, Any, Optional
import pandas as pd

from core.llm_gateway import LLMGateway

logger = logging.getLogger(__name__)


class LocalQueryRouter:
    """Intelligently routes conversational questions to local DuckDB or cloud warehouse."""

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

    def route_and_execute(
        self,
        user_query: str,
        active_df: pd.DataFrame,
        original_question: str,
        original_sql: str,
        api_key: Optional[str] = None,
        temperature: float = 0.1,
    ) -> Dict[str, Any]:
        """
        Classifies user intent and executes local DuckDB SQL if possible.
        """
        if active_df is None or active_df.empty:
            return {
                "handled_locally": False,
                "reason": "Active DataFrame is empty.",
                "requires_cloud_query": True
            }

        columns_meta = {c: str(active_df[c].dtype) for c in active_df.columns}
        sample_rows = active_df.head(3).to_dict(orient="records")

        system_prompt = (
            "You are an expert Data Router & SQL Architect.\n"
            "Given a user's follow-up question and the schema/sample of an active in-memory pandas DataFrame named 'current_df', "
            "determine if the question can be answered LOCALLY using SQL operations (filtering, sorting, aggregation, math, grouping) on 'current_df'.\n\n"
            "Rules:\n"
            "- If the question asks for dimensions, metrics, timeframes, or granular tables NOT present in 'current_df', set 'can_answer_locally': false.\n"
            "- If the question can be answered on 'current_df', write standard DuckDB SQL referencing table 'current_df' and set 'can_answer_locally': true.\n\n"
            "Respond strictly in valid JSON format:\n"
            "{\n"
            '  "can_answer_locally": true | false,\n'
            '  "duckdb_sql": "SELECT ... FROM current_df ...",\n'
            '  "explanation": "Plain-English explanation of the answer or why cloud query is needed.",\n'
            '  "missing_data_reasons": ["List of missing columns/tables if false"]\n'
            "}"
        )

        user_content = (
            f"### 💬 User Follow-up Question:\n{user_query}\n\n"
            f"### 📋 Original Business Question:\n{original_question}\n\n"
            f"### 📝 Original Cloud SQL Query:\n```sql\n{original_sql}\n```\n\n"
            f"### 📊 Active DataFrame Schema & Sample:\n"
            f"Columns: {json.dumps(columns_meta)}\n"
            f"Total Rows: {len(active_df)}\n"
            f"Sample Data:\n```json\n{json.dumps(sample_rows, default=str, indent=2)}\n```\n"
        )

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
            raw_res = gw_res.get("text", "")
            clean_json = raw_res.strip()
            if "```json" in clean_json:
                clean_json = clean_json.split("```json")[1].split("```")[0].strip()
            elif "```" in clean_json:
                clean_json = clean_json.split("```")[1].split("```")[0].strip()

            parsed = json.loads(clean_json)
            can_local = parsed.get("can_answer_locally", False)
            duckdb_sql = parsed.get("duckdb_sql", "").strip()

            if can_local and duckdb_sql:
                # Execute in DuckDB
                conn = duckdb.connect(database=":memory:")
                conn.register("current_df", active_df)
                res_df = conn.execute(duckdb_sql).df()
                conn.close()

                return {
                    "handled_locally": True,
                    "result_df": res_df,
                    "duckdb_sql": duckdb_sql,
                    "explanation": parsed.get("explanation", ""),
                    "requires_cloud_query": False
                }
            else:
                return {
                    "handled_locally": False,
                    "reason": parsed.get("explanation", "Question requires querying data not in current dataset."),
                    "missing_data_reasons": parsed.get("missing_data_reasons", []),
                    "requires_cloud_query": True
                }
        except Exception as e:
            logger.warning(f"Local query routing failed: {e}")
            return {
                "handled_locally": False,
                "reason": f"Routing error: {e}",
                "requires_cloud_query": True
            }
