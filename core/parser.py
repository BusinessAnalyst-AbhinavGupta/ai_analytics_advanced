import re
import json
import logging
import os
from typing import Dict, Any, List, Optional
from core.llm_gateway import LLMGateway

logger = logging.getLogger(__name__)

class QueryAnalyzer:
    """Regex & AST analyzer to extract CTEs, metrics, column aliases, and base filters."""
    def __init__(self, sql: str):
        self.sql = sql

    def extract_aliases(self) -> List[Dict[str, Any]]:
        """Extracts column projections and aliases like 'col AS alias' or 'expr AS alias'."""
        aliases = []
        # Match table name from FROM clause
        tbl_match = re.search(r"FROM\s+([a-zA-Z0-9_.]+)", self.sql, re.IGNORECASE)
        table_name = tbl_match.group(1).strip() if tbl_match else ""

        # Match simple col AS alias
        # e.g., natco_code AS natco, identifiers_user_id AS user_id
        alias_matches = re.findall(r"([a-zA-Z0-9_.]+)\s+AS\s+([a-zA-Z0-9_]+)", self.sql, re.IGNORECASE)
        for col_expr, alias in alias_matches:
            col_cleaned = col_expr.strip().split(".")[-1]
            alias_cleaned = alias.strip()
            # Avoid SQL keywords as aliases
            if alias_cleaned.upper() not in {"FROM", "WHERE", "GROUP", "ORDER", "JOIN", "ON", "AND", "OR", "AS", "LIMIT"}:
                aliases.append({
                    "physical_column": col_cleaned,
                    "alias": alias_cleaned,
                    "expression": col_expr.strip(),
                    "table_name": table_name,
                    "reasoning": f"Extracted from projection: `{col_expr} AS {alias}`"
                })

        # Match functional expressions e.g., date_trunc('week', event_date) AS week_start
        func_matches = re.findall(r"([a-zA-Z0-9_]+\s*\([^)]+\))\s+AS\s+([a-zA-Z0-9_]+)", self.sql, re.IGNORECASE)
        for func_expr, alias in func_matches:
            alias_cleaned = alias.strip()
            # Extract inner column name if identifiable
            inner_col_match = re.search(r"\b([a-zA-Z0-9_]+)\b\s*\)", func_expr)
            inner_col = inner_col_match.group(1) if inner_col_match else ""
            if alias_cleaned.upper() not in {"FROM", "WHERE", "GROUP", "ORDER", "JOIN", "ON", "AND", "OR", "AS", "LIMIT"}:
                if not any(a["alias"] == alias_cleaned for a in aliases):
                    aliases.append({
                        "physical_column": inner_col or func_expr.strip(),
                        "alias": alias_cleaned,
                        "expression": func_expr.strip(),
                        "table_name": table_name,
                        "reasoning": f"Extracted from functional expression: `{func_expr} AS {alias}`"
                    })

        return aliases

    def analyze(self) -> dict:
        metrics = []
        filter_blocks = []
        aliases = self.extract_aliases()

        # 1. Regex find CASE WHEN ... END patterns for metrics
        case_matches = re.findall(r"(COUNT\s*\(\s*DISTINCT\s+CASE\s+WHEN\s+.*?\s+END\s*\))\s+AS\s+([a-zA-Z0-9_]+)", self.sql, re.IGNORECASE | re.DOTALL)
        for logic, alias in case_matches:
            metrics.append({
                "name": alias,
                "raw_logic": " ".join(logic.split())
            })

        # Simple COUNT patterns
        count_matches = re.findall(r"(COUNT\s*\(\s*DISTINCT\s+[a-zA-Z0-9_.]+\s*\))\s+AS\s+([a-zA-Z0-9_]+)", self.sql, re.IGNORECASE)
        for logic, alias in count_matches:
            if not any(m["name"] == alias for m in metrics):
                metrics.append({
                    "name": alias,
                    "raw_logic": logic
                })

        # 2. Extract base WHERE conditions
        where_matches = re.findall(r"WHERE\s+(.*?)(?:GROUP\s+BY|ORDER\s+BY|LIMIT|\)|;|$)", self.sql, re.IGNORECASE | re.DOTALL)
        for wm in where_matches:
            conditions = re.split(r"\s+AND\s+", wm, flags=re.IGNORECASE)
            for c in conditions:
                cleaned = " ".join(c.strip().split())
                if cleaned and len(cleaned) < 200:
                    filter_blocks.append(cleaned)

        return {
            "common_context": {
                "base_filters": list(set(filter_blocks)),
                "raw_basis": self.sql
            },
            "extracted_metrics": metrics,
            "column_aliases": aliases
        }

def clean_and_parse_llm_json(response_text: str) -> dict:
    """Extracts and parses JSON block from LLM response, stripping reasoning tags."""
    cleaned_text = re.sub(r"<think>.*?</think>", "", response_text, flags=re.DOTALL).strip()
    
    # 1. First try raw decode from first '{'
    start = cleaned_text.find('{')
    if start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(cleaned_text[start:])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    # 2. Try regex extraction
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned_text, re.DOTALL | re.IGNORECASE)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except Exception:
            pass
            
    # 3. Fallback to start to end brace
    end = cleaned_text.rfind('}')
    if start != -1 and end != -1:
        return json.loads(cleaned_text[start:end+1])
        
    raise ValueError(f"No JSON block found in LLM response")

def analyze_sql_deep_reasoning(
    sql: str,
    metadata: Optional[Dict[str, Any]] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    context_window: int = 262144
) -> dict:
    """
    Performs deep domain and structural SQL reasoning using Gemini 3.5 Flash.
    Extracts business intent, column usage contexts, SQL design patterns/idioms,
    disambiguation rules, and clean canonical templates.
    """
    metadata = metadata or {}
    dash_name = metadata.get("dashboard_name") or "E-Commerce Journey Dashboard"
    tab_name = metadata.get("tab_name") or metadata.get("journey_stage") or "General"
    card_name = metadata.get("card_name") or metadata.get("name") or "Analytics Question"
    card_desc = metadata.get("description") or ""
    display_type = metadata.get("display") or metadata.get("display_type") or "table"
    viz_settings = metadata.get("visualization_settings") or {}
    template_tags = metadata.get("template_tags") or {}
    
    viz_summary = []
    if display_type:
        viz_summary.append(f"- Visualization Display Type: {display_type}")
    if "funnel.rows" in viz_settings:
        active_steps = [r.get("name") or r.get("key") for r in viz_settings["funnel.rows"] if r.get("enabled", True)]
        viz_summary.append(f"- Active Funnel Steps in UI: {', '.join(active_steps)}")
    if "table.columns" in viz_settings:
        active_cols = [c.get("name") for c in viz_settings["table.columns"] if c.get("enabled", True)]
        viz_summary.append(f"- Displayed Result Columns: {', '.join(active_cols)}")
        
    viz_context_str = "\n".join(viz_summary) if viz_summary else "- Display: " + str(display_type)

    system_prompt = """You are a Principal Data Architect and SQL Intelligence Engineer.
Your mission is to analyze production SQL queries within their exact business context (dashboard, journey stage, question intent, and UI visualization) and extract deep structural intelligence.
Teach the Knowledge Graph HOW and WHY this query is constructed so future queries can be synthesized with 100% domain accuracy."""

    user_prompt = f"""Analyze the following analytics SQL query and its business context:

BUSINESS CONTEXT:
- Dashboard: {dash_name}
- Journey Stage / Section Tab: {tab_name}
- Question / Card Name: {card_name}
- Description: {card_desc}
{viz_context_str}

NATIVE SQL QUERY:
```sql
{sql}
```

Perform deep domain reasoning and return a structured JSON response with:
1. "intent_name": A concise semantic title for this business problem (e.g. "Registration Step Funnel Analysis", "Drop-Off Drilldown at OTP Viewed").
2. "journey_stage": The specific stage in the e-commerce funnel (e.g. "Registration", "PersonalInfo", "Identification", "Shipping", "Appointment", "Consent", "Checkout").
3. "business_goal": Why this query is executed and what KPI/action it enables.
4. "reasoning_summary": A detailed breakdown of WHY the query is structured this way (e.g., why CTEs are used, why session grouping is needed, why certain filters isolate Germany acquisition traffic).
5. "root_tables": Array of strings representing ALL physical database base tables referenced in the FROM or JOIN clauses (excluding CTE aliases).
6. "column_usages": Array of objects detailing each column used in SELECT, WHERE, CASE WHEN, GROUP BY:
   - "column_name": Exact column name in the DB.
   - "role": Its semantic purpose (e.g., "Funnel Step Action Identification", "Form Name Scope", "Page URL Filter", "Session Key", "Country Traffic Scope").
   - "predicate_pattern": The exact literal predicate pattern used with this column (e.g., "action = 'contentFillOut' AND attr_form_name = 'registration form'", "nc = 'de'").
   - "importance_weight": Float between 0.5 and 1.0.
   - "reasoning": Why this specific column and value are required.
7. "column_aliases": Array of objects detailing column projections, aliases, abbreviations, or calculated business terms defined in SELECT (e.g. `SELECT natco_code AS natco`, `SELECT date_trunc('week', event_date) AS week_start`):
   - "physical_column": Underlying database column name (e.g. "natco_code", "event_date", "identifiers_user_id").
   - "alias": Business alias or shorthand (e.g. "natco", "week_start", "user_id").
   - "expression": The full expression (e.g. "natco_code", "date_trunc('week', event_date)").
   - "reasoning": Why this alias/shorthand represents this business concept.
8. "sql_idioms": Array of reusable structural design patterns identified in this query:
   - "name": e.g. "Session-Level Event Flagging CTE", "Funnel Step Union Aggregation", "Drop-off Range Segmentation", "Last-Event Error Attribution".
   - "category": e.g. "Funnel Analysis", "Drop-off Analysis", "Error Diagnosis".
   - "description": Explanation of the pattern.
   - "sql_skeleton": Generalized SQL skeleton showing how this pattern is written.
   - "when_to_use": When an analyst should use this pattern.
9. "learned_rules": Array of domain rules, disambiguation rules, or anti-patterns:
   - "rule_type": "DISAMBIGUATION" | "FILTER_CONSTRAINT" | "ANTI_PATTERN" | "COMPUTED_METRIC"
   - "description": Rule explanation (e.g. "When querying Registration OTP verification, filter login_identifier_viewed = 0 to prevent miscounting login users as registration users").
   - "reasoning": The technical or domain reason.
10. "canonical_golden_query": Clean, standardized SQL with a multi-line header comment explaining the business problem, journey stage, and instructions for execution.

Respond strictly with valid JSON conforming to this schema:
```json
{{
  "intent_name": "string",
  "journey_stage": "string",
  "business_goal": "string",
  "reasoning_summary": "string",
  "primary_table": "eshop_data.es_events_v2",
  "root_tables": ["eshop_data.es_events_v2"],
  "column_usages": [
    {{
      "column_name": "string",
      "role": "string",
      "predicate_pattern": "string",
      "importance_weight": 0.9,
      "reasoning": "string"
    }}
  ],
  "column_aliases": [
    {{
      "physical_column": "string",
      "alias": "string",
      "expression": "string",
      "reasoning": "string"
    }}
  ],
  "sql_idioms": [
    {{
      "name": "string",
      "category": "string",
      "description": "string",
      "sql_skeleton": "string",
      "when_to_use": "string"
    }}
  ],
  "learned_rules": [
    {{
      "rule_type": "DISAMBIGUATION",
      "description": "string",
      "reasoning": "string"
    }}
  ],
  "extracted_metrics": [
    {{
      "name": "string",
      "type": "Count",
      "raw_logic": "string"
    }}
  ],
  "canonical_golden_query": "string"
}}
```"""

    active_provider = provider or (metadata.get("llm_provider") if metadata else None) or ("OpenRouter API" if os.getenv("OPENROUTER_API_KEY") else "Local Ollama")
    active_api_key = api_key or (metadata.get("llm_api_key") if metadata else None) or os.getenv("OPENROUTER_API_KEY", "")
    active_model = model or (metadata.get("llm_model") if metadata else None) or ("deepseek/deepseek-v4-flash-0731" if active_provider == "OpenRouter API" else "qwen2.5-coder:14b")
    active_context_window = (metadata.get("context_window") if metadata else None) or context_window or 262144

    analyzer = QueryAnalyzer(sql)
    parsed_ast = analyzer.analyze()
    ast_aliases = parsed_ast.get("column_aliases", [])

    try:
        gateway_res = LLMGateway.generate(
            prompt=user_prompt,
            system_prompt=system_prompt,
            provider=active_provider,
            model=active_model,
            api_key=active_api_key,
            temperature=0.0,
            context_window=active_context_window,
            json_mode=True
        )
        parsed_res = clean_and_parse_llm_json(gateway_res["text"])
        
        # Merge AST/regex aliases with LLM aliases to guarantee 100% recall
        llm_aliases = parsed_res.get("column_aliases", [])
        combined_aliases = list(llm_aliases)
        for ast_a in ast_aliases:
            if not any(la.get("alias", "").lower() == ast_a.get("alias", "").lower() for la in combined_aliases):
                combined_aliases.append(ast_a)
        parsed_res["column_aliases"] = combined_aliases
        return parsed_res
    except Exception as e:
        logger.warning(f"Deep SQL reasoning failed: {e}. Falling back to standard parser.")
        return {
            "intent_name": card_name,
            "journey_stage": tab_name,
            "business_goal": card_desc or card_name,
            "reasoning_summary": "Extracted via regex fallback analyzer.",
            "primary_table": parsed_ast.get("column_aliases", [{}])[0].get("table_name") or "eshop_data.es_events_v2",
            "column_usages": [],
            "column_aliases": ast_aliases,
            "sql_idioms": [],
            "learned_rules": [],
            "extracted_metrics": parsed_ast.get("extracted_metrics", []),
            "canonical_golden_query": sql
        }

def parse_analytics_logic(
    sql: str,
    metadata: Optional[Dict[str, Any]] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    context_window: int = 262144
) -> dict:
    """
    Parses SQL analytics query using analyze_sql_deep_reasoning.
    Maintains backward compatibility with legacy IngestionPipeline output keys.
    """
    deep_res = analyze_sql_deep_reasoning(
        sql=sql,
        metadata=metadata,
        provider=provider,
        model=model,
        api_key=api_key,
        context_window=context_window
    )
    
    # Backward compatibility mapping
    base_filters = [cu.get("predicate_pattern") for cu in deep_res.get("column_usages", []) if cu.get("predicate_pattern")]
    metrics = deep_res.get("extracted_metrics", [])
    if not metrics and deep_res.get("column_usages"):
        for cu in deep_res.get("column_usages", []):
            if "Metric" in cu.get("role", "") or "Aggregation" in cu.get("role", ""):
                metrics.append({"name": cu.get("column_name"), "raw_logic": cu.get("predicate_pattern")})

    return {
        "extracted_metrics": metrics,
        "common_context": {
            "base_filters": base_filters,
            "raw_basis": sql
        },
        "deep_reasoning": deep_res
    }

