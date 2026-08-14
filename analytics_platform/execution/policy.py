"""Deterministic query-policy engine. Runs BEFORE any execution.

Enforces read-only, single-statement, allowed tables, a row limit, and (optionally)
a required date filter on designated fact tables. Auto-healing re-passes the same
policy on every attempt.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import sqlglot

from ..config import PolicySettings
from ..domain import PolicyDecision


class QueryPolicy:
    def __init__(self, settings: Optional[PolicySettings] = None):
        self.settings = settings or PolicySettings()

    def validate(self, raw_sql: str, *,
                 allowed_tables: Optional[List[str]] = None,
                 row_limit: Optional[int] = None,
                 dialect: str = "athena") -> PolicyDecision:
        reasons: List[str] = []
        sql = raw_sql.strip().rstrip(";")

        # 1. read-only
        head = re.sub(r"\s+", " ", sql).upper()
        if not self.settings.allow_ddl_dml:
            for banned in ("INSERT", "UPDATE ", "DELETE ", "CREATE ", "DROP ", "ALTER ",
                           "TRUNCATE ", "GRANT ", "REVOKE ", "MERGE ", "CALL "):
                if head.startswith(banned):
                    reasons.append(f"Non-read-only statement '{banned.strip()}' is blocked.")
        if head.startswith("EXPLAIN "):
            reasons.append("EXPLAIN is only permitted through the allow-list; skipping.")

        # 1b. unresolved templating -- {{Date}}-style placeholders are a Metabase
        # UI convention, not valid SQL. They parse-fail or silently misbehave at
        # the executor, so catch them here with a clear reason instead.
        template_placeholders = re.findall(r"\{\{\s*[\w.]+\s*\}\}", sql)
        if template_placeholders:
            reasons.append(
                f"Unresolved template placeholder(s) {sorted(set(template_placeholders))} -- "
                "not valid SQL; substitute a concrete value or remove the filter.")

        # 2. single statement
        if self.settings.block_multi_statement:
            stripped = sql
            if ";" in stripped:
                # allow a single trailing semicolon
                if stripped.count(";") > 1 or not stripped.endswith(";"):
                    reasons.append("Multiple statements in one query are blocked.")

        # 3. parse to inspect tables
        try:
            ast = sqlglot.parse_one(sql, read=dialect) if sql else None
        except Exception:
            reasons.append(f"SQL failed to parse for dialect '{dialect}'.")
            ast = None

        tables: List[str] = []
        if ast is not None:
            for node in ast.walk():
                if isinstance(node, sqlglot.exp.Table):
                    t = node.name
                    if t and re.match(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$", t):
                        tables.append(t)
        self._tables = list(dict.fromkeys(tables))

        # 4. allowed tables (normalise lower)
        norm_tables = {t.split(".")[-1].lower() for t in self._tables}
        if allowed_tables:
            allow_norm = {t.split(".")[-1].lower() for t in allowed_tables}
            denied = norm_tables - allow_norm
            if denied:
                reasons.append(f"Table(s) not in allow-list: {sorted(denied)}.")

        if not self._tables:
            reasons.append("No tables referenced (or parse failed).")

        # 5. required date filter on designated fact tables
        whole = sql.lower()
        for t in self.settings.require_date_filter_tables:
            if t.lower() in whole and "where" not in whole:
                reasons.append(f"Fact table '{t}' requires a WHERE date filter.")
                break

        limit = row_limit if row_limit is not None else self.settings.default_row_limit

        # 6. row limiting — inject LIMIT where it is absent and it is a plain SELECT
        final_sql = sql
        has_limit = bool(re.search(r"\blimit\s+\d+", sql, re.IGNORECASE))
        if not has_limit and not re.search(r"\bwith\b", sql, re.IGNORECASE):
            try:
                parsed = sqlglot.parse_one(sql, read=dialect)
                if isinstance(parsed, sqlglot.exp.Select) and parsed.args.get("limit") is None:
                    final_sql = f"{sql.strip().rstrip(';')}\nLIMIT {int(limit)}"
            except Exception:
                pass

        if not reasons:
            return PolicyDecision(allowed=True, reasons=[], approved_sql=final_sql)
        return PolicyDecision(allowed=False, reasons=reasons, approved_sql="")

    @property
    def referenced_tables(self) -> List[str]:
        return getattr(self, "_tables", [])