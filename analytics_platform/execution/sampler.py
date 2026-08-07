"""SamplerExecutor: offline/local execution against pandas DataFrames via duckdb.

This is how the platform runs end-to-end without live Metabase/DB: register known
tables into an in-memory duckdb instance and execute the (policy-approved) SQL.
Used by tests, fixtures, and the synthetic-company demo.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import duckdb
import pandas as pd
import sqlglot

from .base import ExecutionContext, QueryResult, QueryExecutor, SessionStatus


class SamplerExecutor(QueryExecutor):
    def __init__(self, warehouse: Optional[Dict[str, pd.DataFrame]] = None):
        self._warehouse: Dict[str, pd.DataFrame] = dict(warehouse or {})
        self._conn: Optional[duckdb.DuckDBPyConnection] = None

    def _db(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            self._conn = duckdb.connect(database=":memory:")
        return self._conn

    def register(self, table: str, df: pd.DataFrame) -> None:
        self._warehouse[table] = df
        if self._conn is not None:
            try:
                self._db().register(table, df)
            except Exception:
                pass

    def register_warehouse(self, warehouse: Dict[str, pd.DataFrame]) -> None:
        for t, df in warehouse.items():
            self.register(t, df)

    def supports(self, ctx: ExecutionContext) -> bool:
        return True

    def session_status(self, tenant_id: str) -> SessionStatus:
        return SessionStatus(state="valid", tenant_id=tenant_id, browser_ok=True)

    def _refresh_registrations(self) -> None:
        db = self._db()
        for t, df in self._warehouse.items():
            try:
                db.register(t, df)
            except Exception:
                pass

    def execute(self, sql: str, ctx: ExecutionContext) -> QueryResult:
        self._refresh_registrations()
        t0 = time.perf_counter()
        # Transpile from the source dialect (e.g. athena/ansi) to duckdb so the
        # executor is dialect-agnostic (e.g. date_format -> STRFTIME). Best-effort:
        # on failure the query may already be duckdb-native (e.g. our novel demo).
        transpiled = sql
        if ctx.dialect and ctx.dialect.lower() not in ("duckdb", "", "native"):
            try:
                transpiled = sqlglot.transpile(sql, read=ctx.dialect.lower(), write="duckdb")[0]
            except Exception:
                transpiled = sql
        try:
            df = self._db().execute(transpiled).df()  # type: ignore[arg-type]
            if len(df) > ctx.row_limit:
                df = df.head(ctx.row_limit)
            ms = round((time.perf_counter() - t0) * 1000.0, 2)
            return QueryResult(ok=True, data=df, execution_ms=ms, row_count=len(df),
                               columns=list(df.columns))
        except Exception as e:  # noqa: BLE001
            ms = round((time.perf_counter() - t0) * 1000.0, 2)
            return QueryResult(ok=False, error=str(e), execution_ms=ms)

    def cancel(self, execution_id: str) -> bool:
        return True

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None