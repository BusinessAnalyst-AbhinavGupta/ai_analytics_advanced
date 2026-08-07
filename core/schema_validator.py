import re
import difflib
from typing import Dict, Any, List, Set, Optional, Tuple

class ValidationResult:
    def __init__(self, is_valid: bool, errors: List[str], warnings: List[str], suggestions: List[Dict[str, str]], diagnostic_prompt: str = ""):
        self.is_valid = is_valid
        self.errors = errors
        self.warnings = warnings
        self.suggestions = suggestions
        self.diagnostic_prompt = diagnostic_prompt

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "suggestions": self.suggestions,
            "diagnostic_prompt": self.diagnostic_prompt
        }

class SQLSchemaValidator:
    """
    Deterministic AST & Token Schema Validator.
    Parses generated SQL, extracts referenced table and column identifiers,
    cross-references them against Neo4j's physical schema and learned aliases,
    and detects dialect-specific anti-patterns.
    """

    SQL_KEYWORDS = {
        "SELECT", "FROM", "WHERE", "GROUP", "BY", "ORDER", "HAVING", "JOIN", "INNER", "LEFT", "RIGHT", "FULL",
        "OUTER", "CROSS", "ON", "AND", "OR", "NOT", "IN", "IS", "NULL", "AS", "WITH", "DISTINCT", "CASE", "WHEN",
        "THEN", "ELSE", "END", "LIKE", "ILIKE", "BETWEEN", "EXISTS", "UNION", "ALL", "LIMIT", "OFFSET", "ASC", "DESC",
        "OVER", "PARTITION", "ROWS", "RANGE", "PRECEDING", "FOLLOWING", "UNBOUNDED", "CURRENT", "ROW", "TRUE", "FALSE",
        "CAST", "TRY_CAST", "EXTRACT", "DATE_TRUNC", "DATE_ADD", "INTERVAL", "DATE", "TIMESTAMP", "DOUBLE", "BIGINT",
        "VARCHAR", "INTEGER", "BOOLEAN", "ARRAY", "MAP", "STRUCT", "NULLIF", "COALESCE", "COUNT", "SUM", "AVG",
        "MIN", "MAX", "ROUND", "FLOOR", "CEIL", "ABS", "LOWER", "UPPER", "TRIM", "LTRIM", "RTRIM", "SUBSTRING",
        "REPLACE", "CONCAT", "SPLIT", "CARDINALITY", "ELEMENT_AT", "CONTAINS", "FILTER", "TRANSFORM", "REDUCE",
        "FROM_ISO8601_TIMESTAMP", "TO_ISO8601", "DATE_DIFF", "DATE_PARSE", "DATE_FORMAT", "NOW", "CURRENT_DATE",
        "CURRENT_TIMESTAMP", "CURRENT_TIME", "ROW_NUMBER", "RANK", "DENSE_RANK", "LAG", "LEAD", "FIRST_VALUE",
        "LAST_VALUE", "SAFE_DIVIDE", "IFNULL", "NVL", "DECODE", "WEEK", "MONTH", "DAY", "YEAR", "HOUR", "MINUTE",
        "SECOND", "QUARTER", "DESC", "ASC"
    }

    @classmethod
    def extract_ctes_tables_and_aliases(cls, sql: str) -> Tuple[Set[str], Set[str], Set[str], Set[str]]:
        """Extracts CTE names, physical table names/parts, table aliases, and query-level column aliases."""
        cte_names = set()
        table_names_and_parts = set()
        table_aliases = set()
        col_aliases = set()

        # 1. CTE Names: WITH cte1 AS (...), cte2 AS (...)
        cte_matches = re.findall(r"(?:WITH|,)\s*([a-zA-Z0-9_]+)\s+AS\s*\(", sql, re.IGNORECASE)
        for m in cte_matches:
            cte_names.add(m.strip().lower())

        # 2. Physical Tables in FROM / JOIN (e.g. silver_layer.t_link_journey_checkout_com)
        table_matches = re.findall(r"(?:FROM|JOIN)\s+([a-zA-Z0-9_.]+)", sql, re.IGNORECASE)
        for t in table_matches:
            t_clean = t.strip().lower()
            table_names_and_parts.add(t_clean)
            for part in t_clean.split("."):
                table_names_and_parts.add(part)

        # 3. Table Aliases: FROM tbl t, JOIN tbl t2
        tbl_alias_matches = re.findall(r"(?:FROM|JOIN)\s+[a-zA-Z0-9_.]+\s+(?:AS\s+)?([a-zA-Z0-9_]+)", sql, re.IGNORECASE)
        for a in tbl_alias_matches:
            a_low = a.strip().lower()
            if a.upper() not in cls.SQL_KEYWORDS and a_low not in cte_names and a_low not in table_names_and_parts:
                table_aliases.add(a_low)

        # 4. Column Aliases in SELECT: expr AS alias_name
        col_alias_matches = re.findall(r"\bAS\s+([a-zA-Z0-9_]+)", sql, re.IGNORECASE)
        for ca in col_alias_matches:
            ca_low = ca.strip().lower()
            if ca.upper() not in cls.SQL_KEYWORDS and ca_low not in cte_names:
                col_aliases.add(ca_low)

        return cte_names, table_names_and_parts, table_aliases, col_aliases

    @classmethod
    def extract_referenced_identifiers(cls, sql: str) -> List[str]:
        """Extracts potential column names referenced in the query."""
        # Strip string literals and comments
        clean_sql = re.sub(r"--.*$", "", sql, flags=re.MULTILINE)
        clean_sql = re.sub(r"/\*.*?\*/", "", clean_sql, flags=re.DOTALL)
        clean_sql = re.sub(r"'[^']*'", "''", clean_sql)
        clean_sql = re.sub(r'"[^"]*"', '""', clean_sql)

        cte_names, table_parts, table_aliases, col_aliases = cls.extract_ctes_tables_and_aliases(sql)

        # Extract all identifier tokens
        tokens = re.findall(r"\b([a-zA-Z][a-zA-Z0-9_]*)\b", clean_sql)
        
        referenced_cols = []
        for tok in tokens:
            tok_low = tok.lower()
            tok_up = tok.upper()
            if tok_up in cls.SQL_KEYWORDS:
                continue
            if tok_low in cte_names:
                continue
            if tok_low in table_parts:
                continue
            if tok_low in table_aliases:
                continue
            if tok.isdigit():
                continue
            referenced_cols.append(tok)

        return referenced_cols

    @classmethod
    def validate_sql(
        cls,
        sql: str,
        target_table: str,
        valid_columns: Dict[str, Dict[str, Any]], # dict: col_name -> col_meta
        learned_aliases: Optional[List[Dict[str, Any]]] = None,
        dialect: str = "AWS Athena / Presto",
        custom_instructions: Optional[str] = None
    ) -> ValidationResult:
        """
        Performs deep deterministic validation against Neo4j schema, custom constraints, and dialect requirements.
        """
        errors = []
        warnings = []
        suggestions = []
        learned_aliases = learned_aliases or []

        if not sql or not sql.strip():
            return ValidationResult(is_valid=False, errors=["Empty SQL query generated."], warnings=[], suggestions=[], diagnostic_prompt="Error: Generated SQL is empty.")

        cte_names, table_parts, table_aliases, col_aliases = cls.extract_ctes_tables_and_aliases(sql)
        referenced_identifiers = cls.extract_referenced_identifiers(sql)

        # Prepare column lookup (case-insensitive)
        valid_col_lookup = {c.lower(): c for c in valid_columns.keys()}
        alias_to_physical = {}
        for la in learned_aliases:
            alias_name = la.get("alias", "").lower()
            phys_col = la.get("physical_column", "")
            if alias_name and phys_col:
                alias_to_physical[alias_name] = phys_col

        # 1. Check for physical schema hallucinations
        checked_tokens = set()
        for token in referenced_identifiers:
            tok_low = token.lower()
            if tok_low in checked_tokens:
                continue
            checked_tokens.add(tok_low)

            # If it's a known valid physical column, valid CTE, table alias, or query-defined alias, it passes
            if tok_low in valid_col_lookup:
                continue
            if tok_low in cte_names or tok_low in table_parts or tok_low in table_aliases or tok_low in col_aliases:
                continue

            # It's an unrecognized identifier: Check if it's a known alias or fuzzy match
            if tok_low in alias_to_physical:
                correct_col = alias_to_physical[tok_low]
                err_msg = f"Unresolved identifier '{token}' is a business alias, not a physical column in '{target_table}'."
                errors.append(err_msg)
                suggestions.append({
                    "invalid_term": token,
                    "suggested_term": correct_col,
                    "reason": f"Map alias '{token}' to physical column '{correct_col}' (use: {correct_col} AS {token})"
                })
            elif tok_low in ["nc", "natco", "nat_co"]:
                # Exact abbreviation check for natco
                correct_col = valid_col_lookup.get("natco_code", "natco_code")
                err_msg = f"Column '{token}' does not exist in table '{target_table}'."
                errors.append(err_msg)
                suggestions.append({
                    "invalid_term": token,
                    "suggested_term": correct_col,
                    "reason": f"Did you mean '{correct_col}'?"
                })
            else:
                # Fuzzy match against valid columns
                matches = difflib.get_close_matches(tok_low, valid_col_lookup.keys(), n=2, cutoff=0.6)
                if matches:
                    best_match = valid_col_lookup[matches[0]]
                    err_msg = f"Column '{token}' does not exist in table '{target_table}'."
                    errors.append(err_msg)
                    suggestions.append({
                        "invalid_term": token,
                        "suggested_term": best_match,
                        "reason": f"Did you mean '{best_match}'?"
                    })
                else:
                    # Generic unrecognized column in target table
                    if len(token) > 2 and token.lower() not in ["sql", "tbl", "cte", "sub", "res", "tmp"]:
                        err_msg = f"Unknown column or token '{token}' not found in table '{target_table}'."
                        errors.append(err_msg)
                        suggestions.append({
                            "invalid_term": token,
                            "suggested_term": "Check table schema dictionary",
                            "reason": f"Available columns in {target_table}: {', '.join(list(valid_columns.keys())[:15])}..."
                        })

        # 2. Check dialect-specific anti-patterns
        if "athena" in dialect.lower() or "presto" in dialect.lower():
            # Check for SAFE_DIVIDE (BigQuery-only)
            if re.search(r"\bSAFE_DIVIDE\s*\(", sql, re.IGNORECASE):
                errors.append("Athena/Presto does not support 'SAFE_DIVIDE()'. Use standard division with 'NULLIF(divisor, 0)'.")
                suggestions.append({
                    "invalid_term": "SAFE_DIVIDE(a, b)",
                    "suggested_term": "CAST(a AS DOUBLE) / NULLIF(b, 0)",
                    "reason": "Athena Presto division syntax requirement."
                })

            # Check for IFNULL
            if re.search(r"\bIFNULL\s*\(", sql, re.IGNORECASE):
                errors.append("Athena/Presto does not support 'IFNULL()'. Use 'COALESCE()'.")
                suggestions.append({
                    "invalid_term": "IFNULL(a, b)",
                    "suggested_term": "COALESCE(a, b)",
                    "reason": "Athena Presto compatibility."
                })

            # Check for invalid date_diff with integer offset
            if re.search(r"\bdate_diff\s*\(\s*['\"][^'\"]+['\"]\s*,\s*\d+", sql, re.IGNORECASE):
                errors.append("In Athena/Presto, 'date_diff(unit, timestamp1, timestamp2)' takes TWO timestamps and returns an integer difference. To subtract days/hours, use date arithmetic like 'current_timestamp - interval '14' day' or 'CURRENT_DATE - INTERVAL '14' DAY'.")
                suggestions.append({
                    "invalid_term": "date_diff('day', N, current_timestamp)",
                    "suggested_term": "current_timestamp - interval 'N' day",
                    "reason": "Athena/Presto date arithmetic syntax."
                })

            # Check for invalid from_iso8601_timestamp on native timestamp columns (_timestamp)
            if re.search(r"\bfrom_iso8601_timestamp\s*\(\s*_timestamp\s*\)", sql, re.IGNORECASE):
                errors.append("In Athena/Presto table 'es_events_v2', column '_timestamp' is already of type TIMESTAMP(3). Calling 'from_iso8601_timestamp(_timestamp)' throws FUNCTION_NOT_FOUND because it only accepts VARCHAR strings. Use 'date_format(_timestamp, ...)' or 'date_trunc(..., _timestamp)' directly.")
                suggestions.append({
                    "invalid_term": "from_iso8601_timestamp(_timestamp)",
                    "suggested_term": "_timestamp",
                    "reason": "_timestamp is natively TIMESTAMP(3), not VARCHAR."
                })

            # Check for hallucinated drop-off event strings (e.g. action = 'dropped_at_...')
            if re.search(r"\baction\s*=\s*['\"][^'\"]*drop[^'\"]*['\"]", sql, re.IGNORECASE):
                errors.append("Funnel drop-off is never an emitted action string (e.g. 'dropped_at_...'). Compute drop-offs using session-level conditional aggregation: 'MAX(CASE WHEN step_1 THEN 1 ELSE 0 END) = 1 AND MAX(CASE WHEN step_2 THEN 1 ELSE 0 END) = 0'.")
                suggestions.append({
                    "invalid_term": "action = 'dropped_...'",
                    "suggested_term": "Two-stage cohort filtering (MAX(CASE WHEN step1) = 1 AND MAX(CASE WHEN step2) = 0)",
                    "reason": "Event telemetry logs user interactions, not derived drop-off states."
                })

        # 3. Check for mandatory custom constraints compliance
        if custom_instructions and custom_instructions.strip():
            # Extract quoted literals (e.g. 'DE', 'fixed', '2026-01-01')
            quoted_literals = re.findall(r"['\"]([^'\"]+)['\"]", custom_instructions)
            for ql in quoted_literals:
                ql_clean = ql.strip()
                if len(ql_clean) >= 2 and ql_clean.lower() not in ["and", "or", "not", "in", "like", "is", "null"]:
                    if ql_clean.lower() not in sql.lower():
                        errors.append(f"Missing Mandatory Constraint Filter: The literal value '{ql_clean}' specified in custom constraints was NOT found in the SQL query.")
                        suggestions.append({
                            "invalid_term": "Omitted Constraint Filter",
                            "suggested_term": f"Filter with '{ql_clean}' (from custom constraints)",
                            "reason": f"Analyst explicitly requested: {custom_instructions[:80]}"
                        })

        # 4. Build diagnostic re-prompt if errors exist
        diagnostic_prompt = ""
        if errors:
            diagnostic_lines = [
                "⚠️ DETERMINISTIC SCHEMA & COMPILER AUDIT FAILED:",
                f"The generated SQL contains {len(errors)} error(s) against table '{target_table}':"
            ]
            for i, err in enumerate(errors, 1):
                diagnostic_lines.append(f"  {i}. {err}")
            
            if suggestions:
                diagnostic_lines.append("\nRequired Corrections:")
                for s in suggestions:
                    diagnostic_lines.append(f"  • Replace '{s['invalid_term']}' -> '{s['suggested_term']}' ({s['reason']})")

            diagnostic_lines.append("\nPlease output the corrected SQL query strictly using the valid physical columns from the schema dictionary.")
            diagnostic_prompt = "\n".join(diagnostic_lines)

        is_valid = len(errors) == 0
        return ValidationResult(
            is_valid=is_valid,
            errors=errors,
            warnings=warnings,
            suggestions=suggestions,
            diagnostic_prompt=diagnostic_prompt
        )
