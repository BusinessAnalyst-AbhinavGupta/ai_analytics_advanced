"""AnalyticalWorkspace -- DuckDB over the per-tenant Parquet extract cache.

Every materialised extract is registered as a view over its Parquet file, so a
follow-up turn can re-cut, filter, join, or aggregate locally instead of going
back through a human's browser tab to Athena. The split is deliberate: **set
operations here, statistics and chart specs in the Python sandbox** -- and both
read the same Parquet files, so the two paths can never disagree about the data.

DuckDB is embedded and read-only over Parquet here. It never reaches the
network: known-extension autoinstall and autoload are both turned off, so httpfs
cannot appear, and every statement still goes through the same QueryPolicy that
governs warehouse SQL.

Note on `enable_external_access`: it is deliberately NOT set. It would block
reading the local Parquet files this engine exists to query, so it cannot be the
network guard -- disabling extension autoloading is.
"""
from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import pandas as pd

from ..config import PolicySettings
from .extract_store import SAFE_ID, ExtractStore
from .policy import QueryPolicy

logger = logging.getLogger(__name__)

WORKSPACE_QUERY_TIMEOUT_S = 30.0
WORKSPACE_RESULT_ROW_CAP = 100_000     # what a local query may return into memory

# Statements that would take this engine outside "read Parquet on this disk".
_FORBIDDEN_RE = re.compile(r"^\s*(INSTALL|LOAD|ATTACH|DETACH|COPY|EXPORT|IMPORT|PRAGMA|SET)\b",
                           re.IGNORECASE)


@dataclass
class WorkspaceResult:
    ok: bool
    data: Optional[pd.DataFrame] = None
    error: str = ""
    row_count: int = 0
    truncated: bool = False
    sql: str = ""


class AnalyticalWorkspace:
    def __init__(self, store: ExtractStore,
                 policy_settings: Optional[PolicySettings] = None) -> None:
        self.store = store
        # The transport ceiling bounds one AppleScript round trip to Metabase.
        # This engine is in-process over local Parquet -- there is no transport --
        # so it is disabled here rather than being tripped by a perfectly ordinary
        # local re-cut. WORKSPACE_RESULT_ROW_CAP is what bounds a local result,
        # and it is enforced again by the wrapper in query().
        self.policy = QueryPolicy(replace(policy_settings or PolicySettings(),
                                          max_transport_rows=0,
                                          default_row_limit=WORKSPACE_RESULT_ROW_CAP + 1))
        self._connections: Dict[Tuple[str, str], "duckdb.DuckDBPyConnection"] = {}
        self._lock = threading.Lock()

    # -- connections ---------------------------------------------------------
    def connect(self, tenant_id: str, conversation_id: str) -> "duckdb.DuckDBPyConnection":
        """One in-memory connection per (tenant, conversation), created lazily.

        In-memory on purpose: the Parquet files are the durable state, the DuckDB
        database is not, so a lost connection is rebuilt by re-registering. Every
        extract on disk is registered **before** the connection is returned -- a
        query against a fresh connection must not silently see zero views.
        """
        key = (tenant_id, conversation_id)
        with self._lock:
            conn = self._connections.get(key)
            if conn is not None:
                return conn
            conn = duckdb.connect(":memory:")
            for statement in ("SET autoinstall_known_extensions=false",
                              "SET autoload_known_extensions=false"):
                try:
                    conn.execute(statement)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("could not harden DuckDB workspace (%s): %s",
                                   statement, exc)
            self._connections[key] = conn
        for meta in self.store.list_metas(tenant_id, conversation_id):
            self._register(conn, tenant_id, conversation_id, meta.label)
        return conn

    def _register(self, conn, tenant_id: str, conversation_id: str, label: str) -> bool:
        if not SAFE_ID.match(label or ""):
            raise ValueError(f"unsafe extract label: {label!r}")
        path = self.store.path(tenant_id, conversation_id, label)
        try:
            # The path is passed as a Python value to the relation API and never
            # interpolated into SQL text. (DuckDB cannot prepare a CREATE VIEW, so
            # a bound parameter is not available -- this is stricter anyway.)
            conn.read_parquet(path).create_view(label, replace=True)
            return True
        except Exception as exc:  # noqa: BLE001 - a bad extract degrades, never crashes
            logger.warning("could not register extract %s for %s/%s: %s",
                           label, tenant_id, conversation_id, exc)
            return False

    def register(self, tenant_id: str, conversation_id: str, label: str) -> bool:
        conn = self.connect(tenant_id, conversation_id)
        if self.store.meta(tenant_id, conversation_id, label) is None:
            return False
        return self._register(conn, tenant_id, conversation_id, label)

    def views(self, tenant_id: str, conversation_id: str) -> List[str]:
        conn = self.connect(tenant_id, conversation_id)
        try:
            return sorted(r[0] for r in conn.execute(
                "SELECT table_name FROM information_schema.tables").fetchall())
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not list workspace views: %s", exc)
            return []

    def parquet_paths(self, tenant_id: str, conversation_id: str) -> Dict[str, str]:
        """label -> path, handed to the Python sandbox so it loads the same files."""
        return self.store.parquet_paths(tenant_id, conversation_id)

    def close(self, tenant_id: str, conversation_id: str) -> None:
        with self._lock:
            conn = self._connections.pop((tenant_id, conversation_id), None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def close_all(self) -> None:
        for key in list(self._connections):
            self.close(*key)

    # -- query ---------------------------------------------------------------
    def query(self, tenant_id: str, conversation_id: str, sql: str) -> WorkspaceResult:
        """Run local SQL through the same policy every warehouse query gets.

        A bad local query is a repairable LLM error, not a 500 -- every failure
        comes back as ok=False with the engine's own message so the repair loop
        has something to work with.
        """
        if _FORBIDDEN_RE.match(sql or ""):
            return WorkspaceResult(
                ok=False, sql=sql,
                error="only SELECT statements may run in the analytical workspace; "
                      "this engine is embedded and read-only over Parquet.")

        views = self.views(tenant_id, conversation_id)
        decision = self.policy.validate(sql, allowed_tables=views or None,
                                        row_limit=WORKSPACE_RESULT_ROW_CAP + 1,
                                        dialect="duckdb")
        if decision.denied:
            return WorkspaceResult(ok=False, sql=sql,
                                   error="; ".join(decision.reasons))

        conn = self.connect(tenant_id, conversation_id)
        approved = decision.approved_sql
        # Fetch one more than the cap so a full result is distinguishable from a
        # truncated one, rather than being reported as complete at exactly N. The
        # policy's own injected LIMIT is set to the same cap+1 for the same reason
        # -- if it injected exactly the cap, every oversized result would arrive
        # pre-trimmed and look complete.
        guarded = (f"SELECT * FROM (\n{approved}\n) AS _ws "
                   f"LIMIT {WORKSPACE_RESULT_ROW_CAP + 1}")
        try:
            df = self._run_with_timeout(conn, guarded)
        except FuturesTimeout:
            # duckdb releases the GIL during execution, so the worker may still be
            # running: abandon the connection rather than leaving a half-cancelled
            # one in the pool for the next turn to inherit.
            self.close(tenant_id, conversation_id)
            return WorkspaceResult(
                ok=False, sql=approved,
                error=f"local query exceeded {WORKSPACE_QUERY_TIMEOUT_S}s and was abandoned")
        except Exception as exc:  # noqa: BLE001
            return WorkspaceResult(ok=False, sql=approved, error=str(exc))

        truncated = len(df) > WORKSPACE_RESULT_ROW_CAP
        if truncated:
            df = df.head(WORKSPACE_RESULT_ROW_CAP)
        return WorkspaceResult(ok=True, data=df, row_count=len(df),
                               truncated=truncated, sql=approved)

    def _run_with_timeout(self, conn, sql: str) -> pd.DataFrame:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: conn.execute(sql).df()).result(
                timeout=WORKSPACE_QUERY_TIMEOUT_S)


__all__ = ["AnalyticalWorkspace", "WorkspaceResult", "WORKSPACE_QUERY_TIMEOUT_S",
           "WORKSPACE_RESULT_ROW_CAP"]
