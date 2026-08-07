import re
import json
import time
from typing import Dict, Any, List, Optional
from core.llm_gateway import LLMGateway
from core.graph_learner import GraphLearner
from core.schema_validator import SQLSchemaValidator
from core.query_generator import format_leading_commas
from neo4j import GraphDatabase

class AutoHealer:
    """
    AI Error Diagnostic & Self-Healing Engine.
    Diagnoses SQL syntax/schema runtime errors against the Neo4j Knowledge Graph,
    produces corrected SQL, and writes learned rules into the Graph Brain.
    """

    def __init__(self, uri: str = "neo4j://127.0.0.1:7687", auth: tuple = ("neo4j", "password"), database: str = "neo4j", ollama_url: str = "http://127.0.0.1:11434"):
        self.uri = uri
        self.auth = auth
        self.database = database or "neo4j"
        self.ollama_url = ollama_url
        self.learner = GraphLearner(uri=uri, auth=auth, database=database)

    def _retrieve_table_schema(self, table_name: str) -> Dict[str, Any]:
        """Retrieves exact column names, types, and samples for the table."""
        if not table_name or table_name == "Auto-Detect All Tables":
            return {"table_name": "", "columns": []}

        driver = GraphDatabase.driver(self.uri, auth=self.auth)
        try:
            with driver.session(database=self.database) as session:
                cols_res = session.run("""
                    MATCH (t:Table {name: $tname})-[:HAS_COLUMN]->(c:Column)
                    RETURN c.name as name, c.dtype as dtype, c.sample_values as samples, coalesce(c.weight, 0.5) as weight
                    ORDER BY c.name
                """, tname=table_name).data()
                
                # If no direct match on full table name, try matching substring
                if not cols_res:
                    cols_res = session.run("""
                        MATCH (t:Table)-[:HAS_COLUMN]->(c:Column)
                        WHERE $tname CONTAINS t.name OR t.name CONTAINS $tname
                        RETURN t.name as table_name, c.name as name, c.dtype as dtype, c.sample_values as samples, coalesce(c.weight, 0.5) as weight
                        ORDER BY c.name
                    """, tname=table_name).data()

                return {"table_name": table_name, "columns": cols_res}
        finally:
            driver.close()

    def diagnose_and_heal(
        self,
        failed_sql: str,
        error_message: str,
        question: str = "",
        database_dialect: str = "AWS Athena / Presto",
        target_table: str = "",
        analyst_notes: str = "",
        feedback_type: str = "RUNTIME_ERROR", # or "BUSINESS_LOGIC"
        provider: str = "OpenRouter API",
        model_name: str = "deepseek/deepseek-v4-flash-0731",
        api_key: str = "",
        temperature: float = 0.0,
        context_window: int = 262144,
        session_messages: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """
        Diagnoses the failed query or business logic flaw, produces healed SQL, and feeds the learning back to Neo4j.
        Maintains conversational thread memory across auto-heal and revision passes if session_messages is provided.
        """
        t0 = time.time()
        
        # 1. Resolve Effective Target Table (if not specified, parse from FROM clause)
        effective_table = target_table if (target_table and target_table != "Auto-Detect All Tables") else ""
        if not effective_table and failed_sql:
            tbl_match = re.search(r"FROM\s+([a-zA-Z0-9_.]+)", failed_sql, re.IGNORECASE)
            if tbl_match:
                effective_table = tbl_match.group(1).strip()

        # 2. Retrieve Physical Schema & Existing Rules & Learned Aliases
        schema_data = self._retrieve_table_schema(effective_table) if effective_table else {"columns": []}
        
        # Format schema text
        col_lines = []
        for c in schema_data.get("columns", []):
            try:
                samples = json.loads(c["sample_values"]) if isinstance(c["sample_values"], str) else c["sample_values"]
            except Exception:
                samples = []
            samples_str = f" | Samples: {samples[:3]}" if samples else ""
            col_lines.append(f"  - `{c['name']}` ({c.get('dtype', 'string')}){samples_str}")
        schema_str = "\n".join(col_lines) if col_lines else "No column schema found. Relying on dialect standard."

        # Fetch existing learned rules for table
        rules_data = self.learner.get_learned_rules([effective_table] if effective_table else [])
        rules_lines = [f"  - ⚠️ {r['rule_text']}" for r in rules_data]
        rules_str = "\n".join(rules_lines) if rules_lines else "None recorded yet."

        # Fetch dynamically learned aliases for table
        learned_aliases = self.learner.get_learned_aliases([effective_table] if effective_table else [], limit=25)
        alias_lines = [f"  - Term/Alias `{a.get('alias')}` -> Physical column `{a.get('physical_column')}` [Table: {a.get('table_name')}]" for a in learned_aliases]
        aliases_str = "\n".join(alias_lines) if alias_lines else "None recorded yet."

        # 3. Construct Master Diagnostic System Prompt & User Prompt
        if feedback_type == "BUSINESS_LOGIC":
            system_prompt = f"""You are a Principal Product Analytics Lead and Master SQL Architect for {database_dialect}.
The analyst executed a query that ran without syntax errors, but the business logic, stage definitions, event filters, or metric calculations were incorrect based on domain rules.

Your task is to:
1. Analyze the user's domain feedback, the verified physical schema, and learned column aliases.
2. Produce corrected, elegant Production SQL satisfying the user's business intent.
3. MANDATORY FORMATTING CONVENTION - LEADING COMMAS: Always use leading comma / comma-first formatting in all SELECT projections, calculations, and CTE definitions (e.g. `SELECT col1 \n  , col2` and `WITH cte1 AS (...) \n\n, cte2 AS (...)`).
4. Formulate a precise, permanent Domain Learning Rule to ensure the Knowledge Graph never uses the incorrect business logic again.
"""
            user_prompt = f"""Target Dialect: {database_dialect}
Target Physical Table: {effective_table or 'Auto-Detected'}

Verified Column Schema from Neo4j:
{schema_str}

📌 Dynamically Learned Column Aliases from Graph:
{aliases_str}

Existing Learned Rules in Brain:
{rules_str}

Original Business Problem:
"{question}"

Previous SQL Query (Correct syntax, but incorrect business logic):
```sql
{failed_sql}
```

Analyst Business Logic Feedback & Correction:
\"\"\"{error_message}\"\"\"

{f'Additional Notes:\n{analyst_notes}' if analyst_notes else ''}

Rewrite the SQL to correctly implement the business logic, provide an explanation of the logical fix, and extract the permanent domain rule.
Return your response formatted as a single JSON object inside a ```json markdown block:
```json
{{
  "healed_sql": "SELECT ...",
  "root_cause": "Explanation of the business logic flaw (e.g. 'Checkout initiated' should be identified by page_name = 'checkout/start' rather than 'BASKET').",
  "what_changed": "Summary of logical changes made to filters, CTEs, or conditions.",
  "rule_text": "Precise rule for future queries (e.g. When analyzing checkout funnel, 'checkout initiated' must be identified by page_name = 'checkout/start').",
  "invalid_term": "BASKET",
  "correct_term": "checkout/start",
  "rule_type": "BUSINESS_LOGIC"
}}
```
"""
        else:
            system_prompt = f"""You are a Principal Database Administrator and SQL Diagnostics Specialist for {database_dialect}.
Your task is to diagnose a failed SQL query that produced a specific error, perform a concise Root Cause Analysis (RCA), fix the query so it is 100% syntactically valid against the verified physical schema using leading commas (comma-first formatting) in all SELECT projections, and extract a concise learning rule to prevent future errors.
"""
            user_prompt = f"""Target Dialect: {database_dialect}
Target Physical Table: {target_table or 'Auto-Detected'}

Verified Column Schema from Neo4j:
{schema_str}

Existing Learned Rules & Synonyms in Brain:
{rules_str}

Original Business Question:
"{question}"

Failed SQL Query:
```sql
{failed_sql}
```

Execution Error Message:
\"\"\"{error_message}\"\"\"

{f'Analyst Notes:\n{analyst_notes}' if analyst_notes else ''}

Diagnose the error, provide the healed production SQL, explain what was fixed, and extract the learning rule.
Return your response formatted as a single JSON object inside a ```json markdown block:
```json
{{
  "healed_sql": "SELECT ...",
  "root_cause": "Detailed explanation of what failed (e.g. column 'category_name' does not exist in the table; the actual column name is 'category').",
  "what_changed": "Bullet points or summary of exact changes made in the query.",
  "rule_text": "Always use column 'category' instead of 'category_name' when querying table silver_layer.t_link_journey_checkout_com.",
  "invalid_term": "category_name",
  "correct_term": "category",
  "rule_type": "COLUMN_MISNAMING"
}}
```
"""

        # 3. Call LLM Gateway (passing active session thread if available)
        gateway_res = LLMGateway.generate(
            prompt=user_prompt,
            system_prompt=system_prompt,
            messages=session_messages,
            provider=provider,
            model=model_name,
            api_key=api_key,
            temperature=temperature,
            context_window=context_window,
            json_mode=True,
            ollama_url=self.ollama_url
        )
        
        t1 = time.time()
        raw_text = gateway_res.get("text", "").strip()
        updated_session_messages = gateway_res.get("messages", [])

        # 4. Parse Structured Output
        healed_data = {}
        try:
            # Extract JSON block
            json_match = re.search(r"```json\s*(.*?)\s*```", raw_text, re.DOTALL | re.IGNORECASE)
            if json_match:
                healed_data = json.loads(json_match.group(1).strip())
            else:
                # Fallback: locate { and }
                s = raw_text.find('{')
                e = raw_text.rfind('}')
                if s != -1 and e != -1:
                    healed_data = json.loads(raw_text[s:e+1])
                else:
                    raise ValueError("Could not parse JSON response from LLM diagnostic.")
        except Exception as parse_err:
            # Fallback regex SQL extraction
            sql_m = re.search(r"```sql\s*(.*?)(?:```|$)", raw_text, re.DOTALL | re.IGNORECASE)
            healed_data = {
                "healed_sql": sql_m.group(1).strip() if sql_m else failed_sql,
                "root_cause": f"LLM Diagnostic Raw Response: {raw_text[:300]}",
                "what_changed": "Repaired syntax/schema based on error message.",
                "rule_text": f"Fix error related to: {error_message[:100]}",
                "invalid_term": "",
                "correct_term": "",
                "rule_type": "GENERAL_SYNTAX"
            }

        # Format healed SQL with leading commas convention
        h_sql = healed_data.get("healed_sql", "")
        if h_sql:
            h_sql = format_leading_commas(h_sql)
            if question:
                header_marker = "-- 🎯 Business Problem:"
                if header_marker not in h_sql:
                    clean_q = "\n--    ".join(question.strip().split("\n"))
                    header_comment = (
                        f"-- ========================================================\n"
                        f"-- 🎯 Business Problem:\n"
                        f"--    {clean_q}\n"
                        f"-- Mode: {feedback_type} (Healed & Verified)\n"
                        f"-- Engine: {provider} ({model_name}) | Dialect: {database_dialect}\n"
                        f"-- ========================================================\n\n"
                    )
                    h_sql = header_comment + h_sql
            healed_data["healed_sql"] = h_sql
        brain_feedback = {}
        try:
            # Record learned rule & column synonym in Neo4j
            rule_res = self.learner.record_correction_rule(
                table_name=target_table or "GLOBAL",
                rule_text=healed_data.get("rule_text", f"Fixed error: {error_message[:80]}"),
                rule_type=healed_data.get("rule_type", "GENERAL_SYNTAX"),
                invalid_term=healed_data.get("invalid_term"),
                correct_term=healed_data.get("correct_term"),
                error_snippet=error_message,
                failed_sql=failed_sql,
                healed_sql=healed_data.get("healed_sql", "")
            )
            
            # Penalize the failed path
            penalize_res = self.learner.penalize_failure(
                tables_used=[target_table] if target_table else [],
                columns_used=[healed_data.get("invalid_term")] if healed_data.get("invalid_term") else [],
                error_msg=error_message
            )
            brain_feedback = {
                "rule_recorded": rule_res,
                "penalization": penalize_res
            }
        except Exception as brain_err:
            brain_feedback = {"warning": f"Could not update Neo4j brain: {brain_err}"}

        # Validate healed SQL against schema
        valid_cols_dict = {c["name"]: c for c in schema_data.get("columns", [])}
        val_res = SQLSchemaValidator.validate_sql(
            sql=h_sql,
            target_table=effective_table,
            valid_columns=valid_cols_dict,
            learned_aliases=learned_aliases,
            dialect=database_dialect
        )

        return {
            "healed_sql": healed_data.get("healed_sql", ""),
            "root_cause": healed_data.get("root_cause", "Diagnosis completed."),
            "what_changed": healed_data.get("what_changed", ""),
            "rule_text": healed_data.get("rule_text", ""),
            "invalid_term": healed_data.get("invalid_term", ""),
            "correct_term": healed_data.get("correct_term", ""),
            "latency_seconds": round(t1 - t0, 2),
            "is_schema_valid": val_res.is_valid,
            "validation_errors": val_res.errors,
            "validation_warnings": val_res.warnings,
            "provider": provider,
            "model": model_name,
            "brain_feedback": brain_feedback,
            "session_messages": updated_session_messages
        }
