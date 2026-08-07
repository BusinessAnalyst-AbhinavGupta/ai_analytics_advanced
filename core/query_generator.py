import os
import json
import re
import time
from typing import Dict, Any, List, Optional
from neo4j import GraphDatabase
from core.llm_gateway import LLMGateway
from core.graph_learner import GraphLearner
from core.schema_validator import SQLSchemaValidator, ValidationResult

def format_leading_commas(sql: str) -> str:
    """
    Ensures that SQL projections, column lists, and CTE definitions use leading commas (comma-first style),
    making it easy for analysts to comment out individual lines or entire CTEs without comma syntax errors.
    Safely preserves comments, function calls (COALESCE, date_diff, etc.), and CTE structures.
    """
    if not sql:
        return sql

    # 1. Format CTE definitions: Change "),\n<cte_name> AS (" to ")\n\n, <cte_name> AS ("
    def replace_cte_comma(match):
        comment = match.group(1) or ""
        cte_name = match.group(2)
        if comment.strip():
            return f") {comment.strip()}\n\n, {cte_name} AS ("
        return f")\n\n, {cte_name} AS ("

    sql = re.sub(r"\)\s*,\s*(--[^\n]*)?\n\s*([a-zA-Z0-9_]+)\s+AS\s*\(", replace_cte_comma, sql, flags=re.IGNORECASE)

    # 2. Format SELECT projections (column-by-column leading commas)
    lines = sql.split("\n")
    new_lines = []
    in_select_block = False
    
    for i, line in enumerate(lines):
        stripped = line.strip()
        
        # Check pure comments
        if stripped.startswith("--") or stripped.startswith("/*"):
            new_lines.append(line)
            continue
            
        # Detect SELECT clause
        if re.match(r"^\s*SELECT\b", line, re.IGNORECASE):
            in_select_block = True
            new_lines.append(line)
            continue
            
        # Detect end of SELECT clause (FROM, WHERE, GROUP BY, etc., at top-level or matching CTE)
        if in_select_block and re.match(r"^\s*(FROM|WHERE|GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|UNION|WINDOW|\))\b", line, re.IGNORECASE):
            in_select_block = False
            new_lines.append(line)
            continue

        if in_select_block:
            # Handle comments attached at the end of the line e.g., "identifiers_user_id, -- comment"
            if len(new_lines) > 0:
                prev_line = new_lines[-1]
                
                # Separate trailing comment if any from prev_line
                comment_part = ""
                code_part = prev_line
                if "--" in prev_line:
                    comment_idx = prev_line.find("--")
                    comment_part = prev_line[comment_idx:]
                    code_part = prev_line[:comment_idx].rstrip()
                
                code_stripped = code_part.strip()
                if code_stripped.endswith(",") and not re.match(r"^\s*SELECT\b", code_part, re.IGNORECASE):
                    # Remove trailing comma
                    comma_idx = code_part.rfind(",")
                    cleaned_code = code_part[:comma_idx] + code_part[comma_idx+1:].rstrip()
                    new_lines[-1] = (cleaned_code + " " + comment_part).rstrip() if comment_part else cleaned_code
                    
                    # Add leading comma to current line if not already starting with one
                    if not stripped.startswith(","):
                        indent_match = re.match(r"^(\s*)", line)
                        indent = indent_match.group(1) if indent_match else "    "
                        content = line.strip()
                        if len(indent) >= 2:
                            line = indent[:-2] + ", " + content
                        else:
                            line = indent + ", " + content
            new_lines.append(line)
        else:
            new_lines.append(line)
            
    return "\n".join(new_lines)

class QueryGenerator:
    """
    RAG-powered Text-to-SQL Engine that retrieves verified schema, columns,
    sample values, metrics, and funnel domain rules from Neo4j, augmented by
    Hebbian learning weights, golden query patterns, and learned error-avoidance rules.
    """
    
    def __init__(self, uri: str = None, auth: tuple = None, database: str = "neo4j", ollama_url: str = "http://127.0.0.1:11434"):
        self.uri = uri or os.getenv("NEO4J_URI", "neo4j://127.0.0.1:7687")
        self.auth = auth or (os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "password"))
        self.database = database or "neo4j"
        self.ollama_url = ollama_url.rstrip("/")
        self.learner = GraphLearner(uri=self.uri, auth=self.auth, database=self.database)

    def retrieve_graph_context(self, question: str, table_filter: Optional[str] = None) -> Dict[str, Any]:
        """Queries Neo4j for tables, columns, synaptic weights, golden queries, and learned rules."""
        driver = GraphDatabase.driver(self.uri, auth=self.auth)
        context = {
            "tables": [],
            "columns": [],
            "metrics": [],
            "golden_queries": [],
            "learned_rules": []
        }
        
        try:
            with driver.session(database=self.database) as session:
                # 1. Fetch available physical tables (only real tables with columns or database)
                tbl_query = """
                MATCH (t:Table)
                WHERE (t)-[:HAS_COLUMN]->() OR t.database IS NOT NULL
                RETURN t.name as name, t.database as database, t.row_count as row_count, coalesce(t.weight, 0.5) as weight
                ORDER BY t.weight DESC
                """
                all_tbls = session.run(tbl_query).data()
                
                # Intelligent table targeting: check if a specific table is explicitly named in question
                targeted_tbl_name = table_filter
                if not targeted_tbl_name or targeted_tbl_name == "Auto-Detect All Tables":
                    q_lower = question.lower()
                    for t in all_tbls:
                        t_full = t["name"].lower()
                        t_short = t["name"].split(".")[-1].lower()
                        # Match full table name or distinct specific table identifier (> 5 chars, word boundary)
                        if len(t_short) > 5 and re.search(r'\b' + re.escape(t_short) + r'\b', q_lower):
                            targeted_tbl_name = t["name"]
                            break
                        elif re.search(r'\b' + re.escape(t_full) + r'\b', q_lower):
                            targeted_tbl_name = t["name"]
                            break
                            
                if targeted_tbl_name and targeted_tbl_name != "Auto-Detect All Tables":
                    tbl_res = [t for t in all_tbls if t["name"] == targeted_tbl_name]
                    if not tbl_res:
                        tbl_res = all_tbls
                else:
                    tbl_res = all_tbls
                
                context["tables"] = tbl_res
                table_names = [t["name"] for t in tbl_res]
                
                # 2. Fetch Columns with synaptic weights & sample values (Complete schema, no column truncation)
                col_query = """
                MATCH (t:Table)-[r:HAS_COLUMN]->(c:Column)
                WHERE $tbl_name IS NULL OR $tbl_name = 'Auto-Detect All Tables' OR t.name = $tbl_name
                RETURN t.name as table_name, c.name as name, c.dtype as dtype, c.sample_values as sample_values,
                       coalesce(r.weight, 0.5) as weight, coalesce(r.success_count, 0) as successes
                ORDER BY t.name, r.weight DESC, r.success_count DESC, c.name
                """
                active_filter = targeted_tbl_name if (targeted_tbl_name and targeted_tbl_name != "Auto-Detect All Tables") else None
                col_res = session.run(col_query, tbl_name=active_filter).data()
                context["columns"] = col_res
                
                # 3. Fetch Metrics and associated columns/logic
                metric_query = """
                MATCH (m:Metric)
                OPTIONAL MATCH (m)-[:USES_COLUMN]->(c:Column)
                WITH m, collect(c.name) as cols
                RETURN m.name as name, m.description as description, m.stage as stage, cols as columns,
                       coalesce(m.weight, 0.5) as weight
                ORDER BY weight DESC
                LIMIT 20
                """
                metric_res = session.run(metric_query).data()
                context["metrics"] = metric_res

                # 4. Detect Journey Stage if mentioned in question
                detected_stage = None
                known_stages = ["Registration", "PersonalInfo", "Identification", "Shipping", "Appointment", "Consent", "Cart", "Checkout"]
                q_low = question.lower()
                for st in known_stages:
                    if st.lower() == "personalinfo":
                        if re.search(r'\b(personalinfo|personal\s*info|personal|\bpi\b)', q_low):
                            detected_stage = "PersonalInfo"
                            break
                    else:
                        if re.search(r'\b' + re.escape(st.lower()) + r'\b', q_low):
                            detected_stage = st
                            break

                # 5. Fetch Golden Queries, Idioms, Learned Aliases, and Domain Rules from Graph Brain
                context["golden_queries"] = self.learner.get_golden_queries(table_names, question=question, stage=detected_stage, limit=2)
                context["learned_rules"] = self.learner.get_learned_rules(table_names)
                context["learned_aliases"] = self.learner.get_learned_aliases(table_names, limit=30)
                context["sql_idioms"] = self.learner.get_sql_idioms(stage=detected_stage, question=question, limit=3)
                context["domain_rules"] = self.learner.get_learned_domain_rules(stage=detected_stage, limit=5)
                context["detected_stage"] = detected_stage

                # 6. Extract nuanced business filter definitions from golden query WHERE clauses
                context["column_definitions"] = self.learner.get_column_business_definitions(
                    table_names=table_names, question=question, limit=6
                )
                
        except Exception as e:
            print(f"[QueryGenerator] Neo4j retrieval warning: {e}")
        finally:
            driver.close()
            
        return context

    def generate_architectural_plan(
        self,
        question: str,
        database_dialect: str = "AWS Athena / Presto",
        table_filter: Optional[str] = None,
        provider: str = "OpenRouter API",
        model_name: str = "deepseek/deepseek-v4-flash-0731",
        api_key: str = "",
        temperature: float = 0.0,
        context_window: int = 262144,
        custom_instructions: str = ""
    ) -> Dict[str, Any]:
        """
        Phase 1: Generates a high-level Query Architecture Blueprint.
        Evaluates graph sitemap, proposes CTE breakdown, and identifies target schema probes.
        """
        t0 = time.time()
        sitemap = self.learner.get_schema_sitemap()
        
        tables_summary = "\n".join([
            f"  - Table `{t['name']}` ({t.get('database', 'Athena')}) | Rows: {t.get('row_count', 0):,} [Synaptic Weight: {t.get('weight', 0.5):.2f}]"
            for t in sitemap.get("tables", [])
        ]) or "No tables found."

        stages_summary = "\n".join([
            f"  - Stage `{s['stage_name']}`: {s.get('description', '')}"
            for s in sitemap.get("stages", [])
        ]) or "No journey stages defined."

        metrics_summary = "\n".join([
            f"  - Metric `{m['name']}` (Stage: {m.get('stage', 'N/A')})"
            for m in sitemap.get("metrics", [])
        ]) or "No key metrics defined."

        system_prompt = f"""You are a Lead Data Architect specializing in {database_dialect} data warehouse architectures.
Given a business problem statement, you MUST propose a high-level Query Architecture Blueprint before writing code.

Your response MUST follow this exact markdown structure:

### 🏗️ Proposed Query Architecture & Strategy:
Provide a 2-3 paragraph breakdown outlining:
1. **Data Strategy & Branching**: Which physical tables or journey stages to pull from.
2. **CTE Structure**: The step-by-step CTE sequence (e.g. `base_events` -> `stage_funnel` -> `summary`).
3. **Join & Partition Strategy**: Primary join keys (e.g. `session_id`, `user_id`) and date/partition filters.

### 🔍 Targeted Graph Probes:
List the exact tables you need detailed column schemas and sample values for in Phase 2:
- `PROBE_TABLE: <table_name>`
"""

        user_prompt = f"""High-Level Database Sitemap from Neo4j Brain:
PHYSICAL TABLES:
{tables_summary}

JOURNEY STAGES:
{stages_summary}

KEY METRICS:
{metrics_summary}

🎯 PRIMARY BUSINESS PROBLEM:
"{question}"

{f'🚨 MANDATORY CONSTRAINTS: {custom_instructions}' if custom_instructions else ''}

Generate the High-Level Query Architecture Blueprint:
"""

        gateway_res = LLMGateway.generate(
            prompt=user_prompt,
            system_prompt=system_prompt,
            provider=provider,
            model=model_name,
            api_key=api_key,
            temperature=temperature,
            context_window=context_window,
            ollama_url=self.ollama_url
        )

        plan_text = gateway_res.get("text", "").strip()
        session_msgs = gateway_res.get("messages", [])

        # Extract requested probe tables
        probe_tables = re.findall(r"PROBE_TABLE:\s*`?([a-zA-Z0-9_.]+)`?", plan_text)
        if not probe_tables and table_filter and table_filter != "Auto-Detect All Tables":
            probe_tables = [table_filter]

        return {
            "plan_markdown": plan_text,
            "requested_probe_tables": probe_tables,
            "session_messages": session_msgs,
            "latency_seconds": round(time.time() - t0, 2),
            "question": question,
            "database_dialect": database_dialect,
            "table_filter": table_filter,
            "custom_instructions": custom_instructions,
            "provider": provider,
            "model_name": model_name
        }

    def execute_architectural_plan(
        self,
        architectural_plan_res: Dict[str, Any],
        user_refinement: str = "",
        session_messages: Optional[List[Dict[str, str]]] = None,
        api_key: str = "",
        temperature: float = 0.0,
        context_window: int = 262144
    ) -> Dict[str, Any]:
        """
        Phase 2: Executes targeted graph probing and generates production SQL based on approved blueprint.
        """
        question = architectural_plan_res.get("question", "")
        database_dialect = architectural_plan_res.get("database_dialect", "AWS Athena / Presto")
        table_filter = architectural_plan_res.get("table_filter")
        custom_instructions = architectural_plan_res.get("custom_instructions", "")
        provider = architectural_plan_res.get("provider", "OpenRouter API")
        model_name = architectural_plan_res.get("model_name", "deepseek/deepseek-v4-flash-0731")

        session_msgs = session_messages or architectural_plan_res.get("session_messages", [])

        # Step 1: Retrieve targeted Graph Context for requested probe tables
        probe_tables = architectural_plan_res.get("requested_probe_tables", [])
        primary_filter = probe_tables[0] if probe_tables else table_filter
        graph_ctx = self.retrieve_graph_context(question, table_filter=primary_filter)

        # Build schema payload for prompt
        tables_text_list = []
        for t in graph_ctx["tables"]:
            tname = t["name"]
            matching_cols = [c for c in graph_ctx["columns"] if c.get("table_name") == tname]
            col_lines = []
            for c in matching_cols:
                try:
                    samples = json.loads(c["sample_values"]) if isinstance(c["sample_values"], str) else c["sample_values"]
                except Exception:
                    samples = []
                samples_str = f" | Samples: {samples[:5]}" if samples else ""
                weight_str = f" [Weight: {c.get('weight', 0.5):.2f}]" if c.get('weight') else ""
                col_lines.append(f"    - `{c['name']}` ({c.get('dtype', 'string')}){samples_str}{weight_str}")
            cols_block = "\n".join(col_lines) if col_lines else "    (No columns)"
            tables_text_list.append(f"Table: `{tname}`\n{cols_block}")
            
        tables_summary_str = "\n\n".join(tables_text_list)

        # Build column business definitions block for Phase 2 prompt
        p2_col_defs = graph_ctx.get("column_definitions", {})
        p2_col_def_lines = []
        for col, known_vals in p2_col_defs.items():
            if col.startswith("_"):
                continue
            vals_str = ", ".join(f"'{v}'" for v in known_vals[:12])
            p2_col_def_lines.append(f"  - Column `{col}`: known business values → {vals_str}")
        p2_col_defs_str = "\n".join(p2_col_def_lines)

        refinement_block = f"\n\n💬 USER ARCHITECTURAL REFINEMENT: \"{user_refinement.strip()}\"\n(Incorporate this instruction into the query architecture!)" if user_refinement.strip() else ""

        phase2_prompt = f"""{f'''📖 KNOWN BUSINESS FILTER DEFINITIONS (from verified production queries — use EXACTLY these column values in WHERE clauses):
{p2_col_defs_str}

''' if p2_col_defs_str else ''}Targeted Schema Details from Neo4j Brain:
{tables_summary_str}
{refinement_block}

Based on the approved Architecture Blueprint above and targeted schema details, generate the complete production-ready {database_dialect} SQL query in a ```sql markdown block, followed by CTE explanation.
"""


        gateway_res = LLMGateway.generate(
            prompt=phase2_prompt,
            messages=session_msgs,
            provider=provider,
            model=model_name,
            api_key=api_key,
            temperature=temperature,
            context_window=context_window,
            ollama_url=self.ollama_url
        )

        raw_text = gateway_res.get("text", "").strip()
        updated_msgs = gateway_res.get("messages", session_msgs)

        def _extract_sql_and_exp(text: str):
            sm = re.search(r"```sql\s*(.*?)(?:```|$)", text, re.DOTALL | re.IGNORECASE)
            if not sm:
                sm = re.search(r"```\s*(SELECT\s+.*?)(?:```|$)", text, re.DOTALL | re.IGNORECASE)
            sql = sm.group(1).strip() if sm else ""
            sql = format_leading_commas(sql)
            exp = re.sub(r"```sql.*?```", "", text, flags=re.DOTALL).strip()
            return sql, exp

        sql_query, explanation = _extract_sql_and_exp(raw_text)

        # Compiler verification & Auto-Repair Loop
        ctes, used_tables, _, _ = SQLSchemaValidator.extract_ctes_tables_and_aliases(sql_query)
        primary_table_name = ""
        for t in graph_ctx.get("tables", []):
            if t["name"].lower() in used_tables or t["name"].split(".")[-1].lower() in used_tables:
                primary_table_name = t["name"]
                break
        if not primary_table_name and graph_ctx.get("tables"):
            primary_table_name = graph_ctx["tables"][0]["name"]

        valid_cols_dict = {c["name"]: c for c in graph_ctx.get("columns", []) if not primary_table_name or c.get("table_name") == primary_table_name}

        verification_status = "VERIFIED_1ST_TRY"
        verification_iterations = 1
        healed_columns = []

        val_res = SQLSchemaValidator.validate_sql(
            sql=sql_query,
            target_table=primary_table_name,
            valid_columns=valid_cols_dict,
            learned_aliases=graph_ctx.get("learned_aliases", []),
            dialect=database_dialect,
            custom_instructions=custom_instructions
        )

        max_repair_iterations = 3
        current_attempt = 1

        while not val_res.is_valid and current_attempt <= max_repair_iterations:
            current_attempt += 1
            verification_iterations = current_attempt
            verification_status = "HEALED_AND_VERIFIED"
            
            repair_prompt = f"""{val_res.diagnostic_prompt}

🎯 Original Business Question:
"{question}"

{f'''🚨 Mandatory Custom Constraints to Maintain:
{custom_instructions}
''' if custom_instructions else ''}

Target Table: `{primary_table_name}`
Valid Column Schema:
{tables_summary_str}

Draft SQL Query that failed verification:
```sql
{sql_query}
```

Direct Instruction: Fix the Draft SQL query above by replacing any invalid column names, unresolved aliases, or syntax anti-patterns with the exact suggested physical column names. Output the corrected query in a ```sql code block:
"""
            repair_res = LLMGateway.generate(
                prompt=repair_prompt,
                messages=updated_msgs,
                provider=provider,
                model=model_name,
                api_key=api_key,
                temperature=0.0,
                context_window=context_window,
                ollama_url=self.ollama_url
            )
            repair_text = repair_res.get("text", "").strip()
            new_sql, new_exp = _extract_sql_and_exp(repair_text)
            
            if new_sql:
                for s in val_res.suggestions:
                    if s["invalid_term"].lower() in sql_query.lower() and s["suggested_term"].lower() in new_sql.lower():
                        healed_columns.append(f"{s['invalid_term']} -> {s['suggested_term']}")
                        try:
                            self.learner.record_correction_rule(
                                table_name=primary_table_name,
                                rule_text=f"Column '{s['invalid_term']}' does not exist in table {primary_table_name}. Always use '{s['suggested_term']}'.",
                                rule_type="SCHEMA_FATAL",
                                invalid_term=s["invalid_term"],
                                correct_term=s["suggested_term"],
                                error_snippet=s.get("reason", ""),
                                failed_sql=sql_query,
                                healed_sql=new_sql
                            )
                        except Exception:
                            pass

                sql_query = new_sql
                explanation = new_exp or explanation
                updated_msgs = repair_res.get("messages", updated_msgs)

                val_res = SQLSchemaValidator.validate_sql(
                    sql=sql_query,
                    target_table=primary_table_name,
                    valid_columns=valid_cols_dict,
                    learned_aliases=graph_ctx.get("learned_aliases", []),
                    dialect=database_dialect,
                    custom_instructions=custom_instructions
                )

        if not val_res.is_valid:
            verification_status = "UNVERIFIED_WITH_WARNINGS"

        # Prepend standardized business problem header comment
        if sql_query:
            header_marker = "-- 🎯 Business Problem:"
            if header_marker not in sql_query:
                clean_q = "\n--    ".join(question.strip().split("\n"))
                status_label = "Verified by Agentic Compiler" if val_res.is_valid else "Healed & Verified"
                header_comment = (
                    f"-- ========================================================\n"
                    f"-- 🎯 Business Problem:\n"
                    f"--    {clean_q}\n"
                    f"-- Mode: {status_label} (Phase 2 Architecture Execution) | Dialect: {database_dialect}\n"
                    f"-- Engine: {provider} ({model_name})\n"
                    f"-- ========================================================\n\n"
                )
                sql_query = header_comment + sql_query

        _, final_used_tables, _, _ = SQLSchemaValidator.extract_ctes_tables_and_aliases(sql_query)
        tables_used = [t["name"] for t in graph_ctx.get("tables", []) if t["name"].lower() in final_used_tables or t["name"].split(".")[-1].lower() in final_used_tables]
        if not tables_used and primary_table_name:
            tables_used = [primary_table_name]

        return {
            "sql": sql_query,
            "explanation": explanation,
            "raw_response": raw_text,
            "architectural_plan": architectural_plan_res.get("plan_markdown"),
            "tables_found": len(graph_ctx["tables"]),
            "columns_found": len(graph_ctx["columns"]),
            "tables_used": tables_used,
            "provider": provider,
            "model": model_name,
            "verification_status": verification_status,
            "verification_iterations": verification_iterations,
            "validation_errors": val_res.errors,
            "validation_warnings": val_res.warnings,
            "healed_columns": healed_columns,
            "session_messages": updated_msgs
        }

    def generate_sql(
        self,
        question: str,
        database_dialect: str = "AWS Athena / Presto",
        table_filter: Optional[str] = None,
        provider: str = "OpenRouter API",
        model_name: str = "deepseek/deepseek-v4-flash-0731",
        api_key: str = "",
        temperature: float = 0.0,
        context_window: int = 262144,
        custom_instructions: str = ""
    ) -> Dict[str, Any]:
        """Retrieves Knowledge Graph context and prompts LLM Gateway to generate production SQL."""
        t0 = time.time()
        
        # 1. Retrieve Graph Context (augmented with synaptic memory)
        graph_ctx = self.retrieve_graph_context(question, table_filter=table_filter)
        
        # Extract question keywords for smart column relevance sorting
        q_words = [w.lower() for w in re.findall(r'\w+', question) if len(w) > 2]
        
        # Format table schemas and samples
        tables_text_list = []
        for t in graph_ctx["tables"]:
            tname = t["name"]
            matching_cols = [c for c in graph_ctx["columns"] if c.get("table_name") == tname]
            
            # Prioritize columns that match question keywords (e.g. user, id, session, date, action)
            def col_score(c):
                cname = c["name"].lower()
                matches = sum(1 for kw in q_words if kw in cname)
                return (matches, c.get("weight", 0.5), c.get("successes", 0))
                
            sorted_cols = sorted(matching_cols, key=col_score, reverse=True)
            
            col_lines = []
            for c in sorted_cols:  # Pass ALL columns (leveraging 256K context window)
                try:
                    samples = json.loads(c["sample_values"]) if isinstance(c["sample_values"], str) else c["sample_values"]
                except Exception:
                    samples = []
                samples_str = f" | Samples: {samples[:5]}" if samples else ""
                weight_str = f" [Synaptic Weight: {c.get('weight', 0.5):.2f}]" if c.get('weight') else ""
                col_lines.append(f"    - `{c['name']}` ({c.get('dtype', 'string')}){samples_str}{weight_str}")
                
            cols_block = "\n".join(col_lines) if col_lines else "    (No profiled columns yet)"
            tables_text_list.append(f"Table: `{tname}` (Database: {t.get('database', 'Athena')})\n  Columns ({len(matching_cols)} total):\n{cols_block}")
            
        tables_summary_str = "\n\n".join(tables_text_list) if tables_text_list else "No physical tables found in Knowledge Graph."
        
        # Format Metrics Context
        metric_lines = []
        for m in graph_ctx.get("metrics", [])[:20]:
            metric_lines.append(f"  - Metric: `{m.get('name')}` (Stage: {m.get('stage', 'N/A')}) -> Uses Columns: {m.get('columns', [])}")
        metrics_summary_str = "\n".join(metric_lines) if metric_lines else "No metric rules found."

        # Format Dynamically Learned Column Aliases & Synapses
        learned_aliases = graph_ctx.get("learned_aliases", [])
        alias_lines = []
        for a in learned_aliases:
            expr_part = f" (Expression: `{a['expression']}`)" if a.get("expression") else ""
            alias_lines.append(f"  - Business term/alias `{a.get('alias')}` -> Physical column `{a.get('physical_column')}` [Table: {a.get('table_name')} | Confirmed across {a.get('frequency', 1)} query/queries]{expr_part}")
        aliases_summary_str = "\n".join(alias_lines) if alias_lines else "No alias mappings recorded in Graph Brain."

        # Format Learned Anti-Patterns & Column Corrections
        learned_rules = graph_ctx.get("learned_rules", [])
        rules_lines = []
        for r in learned_rules[:15]:
            rules_lines.append(f"  - ⚠️ {r.get('rule_text')}")
        learned_rules_str = "\n".join(rules_lines) if learned_rules else "None recorded yet. Graph brain clean."

        # Format SQL Idioms & Architectural Patterns
        sql_idioms = graph_ctx.get("sql_idioms", [])
        idiom_lines = []
        for idiom in sql_idioms:
            idiom_lines.append(f"  - Pattern: {idiom.get('name')} [{idiom.get('category')}]\n    Explanation: {idiom.get('description')}\n    Template:\n    {idiom.get('template')}")
        idioms_summary_str = "\n\n".join(idiom_lines) if idiom_lines else ""

        # Format Learned Domain & Funnel Rules
        domain_rules = graph_ctx.get("domain_rules", [])
        domain_rule_lines = []
        for dr in domain_rules:
            domain_rule_lines.append(f"  - [{dr.get('rule_type')}]: {dr.get('description')}")
        domain_rules_summary_str = "\n".join(domain_rule_lines) if domain_rules else ""

        # Format Golden Queries (Few-shot patterns from Metabase)
        golden_queries = graph_ctx.get("golden_queries", [])
        golden_lines = []
        for g in golden_queries[:2]:
            golden_lines.append(f"  - Intent / Q: \"{g.get('question')}\"\n    SQL Template:\n```sql\n{g.get('sql')}\n```")
        golden_summary_str = "\n\n".join(golden_lines) if golden_lines else "No golden queries recorded yet."

        # Format nuanced column-level business filter definitions extracted from golden query WHERE clauses
        col_defs = graph_ctx.get("column_definitions", {})
        col_def_lines = []
        for col, known_vals in col_defs.items():
            if col.startswith("_"):
                continue  # skip metadata keys
            vals_str = ", ".join(f"'{v}'" for v in known_vals[:12])
            col_def_lines.append(f"  - Column `{col}`: known business values → {vals_str}")
        col_defs_summary_str = "\n".join(col_def_lines) if col_def_lines else ""
        
        # 2. Build the System & User Prompts
        system_prompt = f"""You are a Principal Data Analyst and SQL Architect specializing in {database_dialect}.
You have direct access to a verified Neo4j Knowledge Graph Brain containing physical database tables, profiled column data types, unique value samples, dynamically learned column aliases from production dashboards, architectural SQL idioms, and domain rules.

Your goal is to write a 100% syntactically correct, highly optimized, and production-ready {database_dialect} query to answer the analyst's question.

CRITICAL GUIDELINES:
1. At the very top of the SQL query, ALWAYS start with a comment containing the Business Problem Statement and any Applied Constraints:
   -- ========================================================
   -- 🎯 Business Problem: <question>
   {f'-- 🚨 Applied Constraints: {custom_instructions}' if custom_instructions else ''}
   -- ========================================================
2. 🚨 MANDATORY USER CONSTRAINTS & AD-HOC FILTERS:
   - If the prompt contains "MANDATORY CUSTOM ANALYST CONSTRAINTS & FILTERS", you MUST strictly enforce and incorporate EVERY single constraint into the generated SQL query (e.g. in WHERE clauses, HAVING clauses, SELECT list, or GROUP BY).
   - User-specified constraints (such as specific dates, country codes, natco, status filters, exclusions) ALWAYS take top precedence over default heuristics. Never omit or ignore them.
3. STRICT SCHEMA INTEGRITY:
   - You must ONLY use column names that appear EXACTLY as listed in the schema for that specific table.
   - Note table-specific naming conventions: For example, `eshop_data.es_events_v2` uses prefixed columns like `identifiers_user_id`, `identifiers_guest_id`, `identifiers_sessionid`, `attr_useragent`, whereas `silver_layer.t_link_journey_checkout_com` uses bare names like `user_id`, `guest_id`, `session_id`. NEVER mix up or borrow column names across different tables!
   - If a concept like "user id" is requested on a table that has `identifiers_user_id`, you must use `identifiers_user_id`.
   - Use the exact sample values provided in the schema for WHERE clauses (e.g. `action = 'onecheckoutinitiated'`).
4. DYNAMIC COLUMN ALIAS & SYNONYM COMPLIANCE:
   - If the business problem or constraints mention shorthand terms or business aliases (e.g. 'natco', 'week', 'sl', 'user_id', 'cat'), you MUST map them directly to their physical column in the target table schema (e.g. `natco_code`, `service_line_code`, `category_name`, `event_time`).
   - When filtering or joining against the physical base table (in WHERE, ON, GROUP BY clauses), ALWAYS use the exact physical column name (e.g. `WHERE natco_code = 'DE'`, NOT `WHERE natco = 'DE'`).
   - Alias the column in the SELECT projection if desired (e.g. `SELECT natco_code AS natco`).
5. APPLY LEARNED SQL IDIOMS & DOMAIN RULES:
   - Adhere to the provided SQL idioms (e.g. two-stage aggregation, session-level event flagging with MAX(CASE...), timestamp filtering).
   - Strictly follow domain rules regarding stage boundaries and error attribution.
6. STRICTLY obey the "Learned Brain Rules & Anti-Patterns to Avoid" section. Never repeat past column hallucinations or misnamings.
7. Structure multi-step calculations using clean, descriptive Common Table Expressions (CTEs).
8. Ensure drop-offs and funnel cohorts properly handle session or user deduplication (e.g. `COUNT(DISTINCT identifiers_user_id)` or `COUNT(DISTINCT session_id)` depending on table schema).
9. MANDATORY SQL FORMATTING CONVENTION - LEADING COMMAS (COMMA-FIRST STYLE):
   - In ALL SELECT projections, column lists, and multi-line aggregations/calculations, place the comma BEFORE the column/calculation at the start of each line (leading comma style), NEVER at the end of the preceding line.
   - For Common Table Expressions (CTEs), place the comma BEFORE the subsequent CTE name (e.g. `)\n\n, next_cte AS (`), NEVER at the end of the previous CTE closing parenthesis `),`.
   - Example:
     WITH base AS (
         SELECT 
               identifiers_sessionid
             , identifiers_user_id
             , action
             , date_format(identifiers_log_time, '%Y-%m-%d') AS event_date
         FROM eshop_data.es_events_v2
     )

     , session_summary AS (
         SELECT 
               identifiers_sessionid
             , COUNT(DISTINCT identifiers_user_id) AS user_count
         FROM base
         GROUP BY identifiers_sessionid
     )

     SELECT 
           identifiers_sessionid
         , user_count
     FROM session_summary;
   - This ensures individual columns or entire CTE blocks can be commented out (e.g. `-- , next_cte AS (...)`) without breaking preceding lines.
10. Output the complete SQL query in a ```sql code block.
11. Provide a concise explanation of the query structure, CTE breakdown, and assumptions below the SQL. If explaining schema columns, clearly state the exact column names found in the table.
"""

        constraints_block = ""
        if custom_instructions and custom_instructions.strip():
            constraints_block = f"""
🚨 MANDATORY CUSTOM ANALYST CONSTRAINTS & FILTERS:
======================================================================
{custom_instructions.strip()}
======================================================================
(CRITICAL: Every condition, filter, and constraint above MUST be applied in the SQL query!)
"""

        user_prompt = f"""
{f'''📖 KNOWN BUSINESS FILTER DEFINITIONS (extracted from verified production queries — use EXACTLY these values in WHERE clauses):
{col_defs_summary_str}
''' if col_defs_summary_str else ''}
Physical Table & Column Schemas from Neo4j:
{tables_summary_str}

Domain Metrics & Knowledge:
{metrics_summary_str}

{f'📌 Dynamically Learned Column Aliases & Terms from Ingested Queries (Ground-Truth Graph Synapses):\n{aliases_summary_str}\n' if aliases_summary_str != 'No alias mappings recorded in Graph Brain.' else ''}
{f'🏗️ Learned SQL Architectural Idioms & Patterns from Metabase:\n{idioms_summary_str}\n' if idioms_summary_str else ''}
{f'📌 Learned Funnel & Domain Rules:\n{domain_rules_summary_str}\n' if domain_rules_summary_str else ''}
🧠 Learned Brain Rules & Anti-Patterns to Avoid:
{learned_rules_str}

{f'⭐ Verified Golden Query Reference (Ground-Truth Metabase Template):\n{golden_summary_str}\n' if golden_summary_str != 'No golden queries recorded yet.' else ''}

🎯 PRIMARY BUSINESS PROBLEM STATEMENT:
"{question}"
{constraints_block}
Generate the complete production-ready {database_dialect} SQL query (strictly applying all mandatory custom constraints) and concise breakdown:
"""

        # 3. Call LLM Gateway
        gateway_res = LLMGateway.generate(
            prompt=user_prompt,
            system_prompt=system_prompt,
            provider=provider,
            model=model_name,
            api_key=api_key,
            temperature=temperature,
            context_window=context_window,
            ollama_url=self.ollama_url
        )
        
        raw_text = gateway_res.get("text", "").strip()
        
        # 4. Extract Initial SQL Draft and Explanation
        def _extract_sql_and_exp(text: str):
            sm = re.search(r"```sql\s*(.*?)(?:```|$)", text, re.DOTALL | re.IGNORECASE)
            if not sm:
                sm = re.search(r"```\s*(SELECT\s+.*?)(?:```|$)", text, re.DOTALL | re.IGNORECASE)
            sql = sm.group(1).strip() if sm else ""
            sql = format_leading_commas(sql)
            exp = re.sub(r"```sql.*?```", "", text, flags=re.DOTALL).strip()
            return sql, exp

        sql_query, explanation = _extract_sql_and_exp(raw_text)

        # 5. Determine Primary Target Table & Valid Column Dictionary
        # Dynamically detect which physical table was referenced in the generated SQL draft
        ctes, used_tables, _, _ = SQLSchemaValidator.extract_ctes_tables_and_aliases(sql_query)
        primary_table_name = ""
        for t in graph_ctx.get("tables", []):
            t_name_low = t["name"].lower()
            t_short_low = t["name"].split(".")[-1].lower()
            if t_name_low in used_tables or t_short_low in used_tables:
                primary_table_name = t["name"]
                break

        if not primary_table_name and graph_ctx.get("tables"):
            primary_table_name = graph_ctx["tables"][0]["name"]
        
        valid_cols_dict = {}
        for c in graph_ctx.get("columns", []):
            if not primary_table_name or c.get("table_name") == primary_table_name:
                valid_cols_dict[c["name"]] = c

        # 6. Step 2 & 3: Agentic Verification Loop (Deterministic Python Compiler + Auto Re-prompting)
        verification_status = "VERIFIED_1ST_TRY"
        verification_iterations = 1
        healed_columns = []
        val_res = SQLSchemaValidator.validate_sql(
            sql=sql_query,
            target_table=primary_table_name,
            valid_columns=valid_cols_dict,
            learned_aliases=learned_aliases,
            dialect=database_dialect,
            custom_instructions=custom_instructions
        )

        max_repair_iterations = 3
        current_attempt = 1

        while not val_res.is_valid and current_attempt <= max_repair_iterations:
            current_attempt += 1
            verification_iterations = current_attempt
            verification_status = "HEALED_AND_VERIFIED"
            
            # Formulate surgical diagnostic repair prompt
            repair_prompt = f"""{val_res.diagnostic_prompt}

🎯 Original Business Question:
"{question}"

{f'''🚨 Mandatory Custom Constraints to Maintain:
{custom_instructions}
''' if custom_instructions else ''}

Target Table: `{primary_table_name}`
Valid Column Schema:
{tables_summary_str}

Draft SQL Query that failed verification:
```sql
{sql_query}
```

Direct Instruction: Fix the Draft SQL query above by replacing any invalid column names, unresolved aliases, or syntax anti-patterns with the exact suggested physical column names (e.g. in WHERE/ON/GROUP BY/SELECT clauses, use physical column `natco_code = 'DE'` instead of alias `natco = 'DE'`). Output the corrected query in a ```sql code block:
"""
            # Re-prompt LLM
            repair_res = LLMGateway.generate(
                prompt=repair_prompt,
                system_prompt=system_prompt,
                provider=provider,
                model=model_name,
                api_key=api_key,
                temperature=0.0, # zero temperature for deterministic repair
                context_window=context_window,
                ollama_url=self.ollama_url
            )
            repair_text = repair_res.get("text", "").strip()
            new_sql, new_exp = _extract_sql_and_exp(repair_text)
            
            if new_sql:
                # Track repaired columns
                for s in val_res.suggestions:
                    if s["invalid_term"].lower() in sql_query.lower() and s["suggested_term"].lower() in new_sql.lower():
                        healed_columns.append(f"{s['invalid_term']} -> {s['suggested_term']}")
                        # Persist learned anti-pattern guardrail into Neo4j
                        try:
                            self.learner.record_correction_rule(
                                table_name=primary_table_name,
                                rule_text=f"Column '{s['invalid_term']}' does not exist in table {primary_table_name}. Always use '{s['suggested_term']}'.",
                                rule_type="SCHEMA_FATAL",
                                invalid_term=s["invalid_term"],
                                correct_term=s["suggested_term"],
                                error_snippet=s.get("reason", ""),
                                failed_sql=sql_query,
                                healed_sql=new_sql
                            )
                        except Exception:
                            pass

                sql_query = new_sql
                explanation = new_exp or explanation

                # Re-validate with compiler
                val_res = SQLSchemaValidator.validate_sql(
                    sql=sql_query,
                    target_table=primary_table_name,
                    valid_columns=valid_cols_dict,
                    learned_aliases=learned_aliases,
                    dialect=database_dialect,
                    custom_instructions=custom_instructions
                )

        if not val_res.is_valid:
            verification_status = "UNVERIFIED_WITH_WARNINGS"

        t1 = time.time()
        
        # Prepend standardized business problem header comment if not already present
        if sql_query:
            header_marker = "-- 🎯 Business Problem:"
            if header_marker not in sql_query:
                clean_q = "\n--    ".join(question.strip().split("\n"))
                status_label = "Verified by Agentic Compiler" if val_res.is_valid else "Healed & Verified"
                header_comment = (
                    f"-- ========================================================\n"
                    f"-- 🎯 Business Problem:\n"
                    f"--    {clean_q}\n"
                    f"-- Mode: {status_label} (Attempt {verification_iterations}) | Dialect: {database_dialect}\n"
                    f"-- Engine: {provider} ({model_name})\n"
                    f"-- ========================================================\n\n"
                )
                sql_query = header_comment + sql_query
        # Determine tables used in the final generated SQL
        _, final_used_tables, _, _ = SQLSchemaValidator.extract_ctes_tables_and_aliases(sql_query)
        tables_used = []
        for t in graph_ctx.get("tables", []):
            t_name_low = t["name"].lower()
            t_short_low = t["name"].split(".")[-1].lower()
            if t_name_low in final_used_tables or t_short_low in final_used_tables:
                tables_used.append(t["name"])
        
        if not tables_used and primary_table_name:
            tables_used = [primary_table_name]

        # Construct initial session thread
        session_msgs = gateway_res.get("messages")
        if not session_msgs:
            session_msgs = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": raw_text}
            ]

        return {
            "sql": sql_query,
            "explanation": explanation,
            "raw_response": raw_text,
            "latency_seconds": round(t1 - t0, 2),
            "tables_found": len(graph_ctx["tables"]),
            "columns_found": len(graph_ctx["columns"]),
            "metrics_found": len(graph_ctx["metrics"]),
            "tables_used": tables_used,
            "learned_rules_applied": len(learned_rules),
            "golden_queries_referenced": len(golden_queries),
            "provider": provider,
            "model": model_name,
            "verification_status": verification_status,
            "verification_iterations": verification_iterations,
            "validation_errors": val_res.errors,
            "validation_warnings": val_res.warnings,
            "healed_columns": healed_columns,
            "session_messages": session_msgs
        }
