"""Executor protocol + result/session models.

This is the pivot point for the open-browser Metabase constraint: the platform
talks to `QueryExecutor`, and `BrowserSessionExecutor` is the default v1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

import pandas as pd


@dataclass
class SessionStatus:
    state: str                 # "valid" | "expired" | "needs_login" | "unknown"
    tenant_id: str = ""
    browser_ok: bool = True
    last_checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    detail: str = ""


@dataclass
class ExecutionContext:
    tenant_id: str
    question: str = ""
    trace_id: str = ""
    row_limit: int = 50000
    timeout_s: float = 120.0
    dialect: str = "athena"
    extract_metadata: bool = True


@dataclass
class QueryResult:
    ok: bool
    data: Optional[pd.DataFrame] = None
    error: str = ""
    execution_ms: float = 0.0
    db_query_id: str = ""
    row_count: int = 0
    columns: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    # The rows returned are a PREFIX of the rows that matched, not all of them.
    # Every downstream population claim (a total, a share, "the top N") is wrong
    # on a truncated result, so this travels with the data rather than being
    # inferred by string-matching `warnings`.
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "error": self.error, "execution_ms": self.execution_ms,
            "db_query_id": self.db_query_id, "row_count": self.row_count,
            "columns": self.columns, "warnings": self.warnings,
            "truncated": self.truncated,
        }


@runtime_checkable
class QueryExecutor(Protocol):
    def supports(self, ctx: ExecutionContext) -> bool: ...

    def session_status(self, tenant_id: str) -> SessionStatus: ...

    def execute(self, sql: str, ctx: ExecutionContext) -> QueryResult: ...

    def cancel(self, execution_id: str) -> bool: ...