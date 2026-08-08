"""SQLite persistence (stdlib sqlite3) for app state + the Company Brain.

Chosen for a zero-dependency, portable MVP. The store is deliberately split by
tenant via scoped WHERE clauses everywhere a caller queries by tenant_id. In a
later phase this can be swapped for PostgreSQL without changing call sites.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional

_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY, name TEXT, region TEXT, llm_provider TEXT,
    retention_days INTEGER, status TEXT, created_at TEXT, purpose TEXT
);
CREATE TABLE IF NOT EXISTS company_profiles (
    tenant_id TEXT PRIMARY KEY,
    name TEXT, industry TEXT, region TEXT, description TEXT,
    customers TEXT, product TEXT, value_creation TEXT, revenue_model TEXT,
    constraints TEXT, risks TEXT, competitors TEXT, preferred_metrics TEXT,
    targets TEXT
);
CREATE TABLE IF NOT EXISTS data_sources (
    id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT, kind TEXT, dialect TEXT,
    connected INTEGER, tables TEXT, config TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS knowledge_nodes (
    id TEXT PRIMARY KEY, tenant_id TEXT, kind TEXT, status TEXT, version INTEGER,
    title TEXT, summary TEXT, payload TEXT,
    confidence TEXT, evidence_ref TEXT, source_ref TEXT,
    created_at TEXT, updated_at TEXT, created_by TEXT, reviewed_by TEXT,
    review_notes TEXT, supersedes TEXT
);
CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY, tenant_id TEXT, text TEXT, mode_budget TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS analysis_runs (
    id TEXT PRIMARY KEY, tenant_id TEXT, trace_id TEXT, question_id TEXT,
    question_text TEXT, sql TEXT, dialect TEXT, executor TEXT, status TEXT,
    answer_mode TEXT, review_status TEXT, generated_at TEXT, execution_ms REAL,
    row_count INTEGER, profile_summary TEXT, rule_triggers TEXT, answer TEXT,
    facts TEXT, hypotheses TEXT, uncertainties TEXT, next_actions TEXT,
    insights TEXT, assumptions TEXT,
    level TEXT, category TEXT, supportive_of TEXT,
    cost_estimate REAL, policy_reasons TEXT, source_node_ids TEXT
);
CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, tenant_id TEXT, trace_id TEXT, stage TEXT, actor TEXT,
    resource TEXT, status TEXT, duration_ms REAL, bytes_in INTEGER,
    tokens_in INTEGER, tokens_out INTEGER, meta TEXT
);
CREATE TABLE IF NOT EXISTS stakeholder_answers (
    id TEXT PRIMARY KEY, tenant_id TEXT, question TEXT, user_id TEXT,
    category TEXT, answer TEXT, answer_mode TEXT, status TEXT,
    trace_id TEXT, created_at TEXT, source_node_ids TEXT, citations TEXT,
    facts TEXT, caveats TEXT, freshness REAL, tokens_in INTEGER,
    tokens_out INTEGER, cost REAL, escalated INTEGER
);
CREATE TABLE IF NOT EXISTS stakeholder_feedback (
    id TEXT PRIMARY KEY, tenant_id TEXT, answer_id TEXT, user_id TEXT,
    rating TEXT, comment TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS research_sources (
    id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT, url TEXT,
    kind TEXT, credibility TEXT, policy TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS research_docs (
    id TEXT PRIMARY KEY, tenant_id TEXT, query TEXT, url TEXT, title TEXT,
    source_id TEXT, credibility TEXT, snippet TEXT, claims TEXT, origin TEXT,
    status TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS auth_principals (
    id TEXT PRIMARY KEY, tenant_id TEXT, role TEXT, name TEXT, email TEXT,
    scopes TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, tenant_id TEXT, actor TEXT, role TEXT, action TEXT,
    resource TEXT, outcome TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS company_profile_history (
    id TEXT PRIMARY KEY, tenant_id TEXT, version INTEGER,
    snapshot TEXT, changed_by TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS api_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, tenant_id TEXT, method TEXT, path TEXT, status INTEGER,
    duration_ms REAL, actor TEXT, meta TEXT
);
CREATE TABLE IF NOT EXISTS scheduler_state (
    key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS analyst_configs (
    tenant_id TEXT PRIMARY KEY, config TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS analyst_config_history (
    id TEXT PRIMARY KEY, tenant_id TEXT, version INTEGER,
    snapshot TEXT, changed_by TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_logs_ts ON api_logs(ts);
CREATE INDEX IF NOT EXISTS idx_api_logs_tenant ON api_logs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kn_tenant ON knowledge_nodes(tenant_id);
CREATE INDEX IF NOT EXISTS idx_runs_tenant ON analysis_runs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tel_tenant ON telemetry(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tel_trace ON telemetry(trace_id);
CREATE INDEX IF NOT EXISTS idx_sa_tenant ON stakeholder_answers(tenant_id);
CREATE INDEX IF NOT EXISTS idx_rd_tenant ON research_docs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_log(tenant_id);
CREATE INDEX IF NOT EXISTS idx_cph_tenant ON company_profile_history(tenant_id);
CREATE INDEX IF NOT EXISTS idx_acfg_tenant ON analyst_configs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_ach_tenant ON analyst_config_history(tenant_id);
"""


def get_conn(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with _LOCK:
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Non-destructive column additions so an existing DB picks up new fields.

    CREATE TABLE IF NOT EXISTS never adds columns to an already-created table, so
    add the CP-12 columns to `analysis_runs` when they are missing."""
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(analysis_runs)").fetchall()}
        for col in ("insights", "assumptions"):
            if col not in cols:
                conn.execute(f"ALTER TABLE analysis_runs ADD COLUMN {col} TEXT")
        # CP-15: two-tier junior (low/high) + supporting-workpaper linkage
        for col in ("level", "category", "supportive_of"):
            if col not in cols:
                conn.execute(f"ALTER TABLE analysis_runs ADD COLUMN {col} TEXT")
        conn.commit()
    except Exception:  # noqa: BLE001 - migration must never block startup
        pass


def dump_json(obj: Any) -> str:
    return json.dumps(obj, default=str)


def load_json(raw: Optional[str], default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


class Store:
    """Thin wrapper: safe single write + serialization helpers."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = get_conn(db_path)
        init_db(self.conn)

    def connect(self) -> sqlite3.Connection:
        return self.conn

    def query_all(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        with _LOCK:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with _LOCK:
            cur = self.conn.execute(sql, params)
            return cur.fetchone()

    def execute(self, sql: str, params: tuple = ()) -> None:
        with _LOCK:
            self.conn.execute(sql, params)
            self.conn.commit()

    def execute_many(self, statements: List[tuple]) -> None:
        with _LOCK:
            for sql, params in statements:
                self.conn.execute(sql, params)
            self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def rows_to_dicts(self, rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
        return [dict(r) for r in rows]