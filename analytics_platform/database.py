"""SQLite persistence (stdlib sqlite3) for app state + the Company Brain.

Chosen for a zero-dependency, portable MVP. The store is deliberately split by
tenant via scoped WHERE clauses everywhere a caller queries by tenant_id. In a
later phase this can be swapped for PostgreSQL without changing call sites.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional

_LOCK = threading.RLock()
_LOG = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY, name TEXT, region TEXT, llm_provider TEXT,
    retention_days INTEGER, status TEXT, created_at TEXT, purpose TEXT
);
CREATE TABLE IF NOT EXISTS scheduler_state (
    key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS api_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, tenant_id TEXT, method TEXT, path TEXT, status INTEGER,
    duration_ms REAL, actor TEXT, meta TEXT
);
CREATE TABLE IF NOT EXISTS auth_principals (
    id TEXT PRIMARY KEY, tenant_id TEXT, role TEXT, name TEXT, email TEXT,
    scopes TEXT, created_at TEXT
);
-- Distinctly named from the tenant-plane `audit_log` below (not a shared
-- name across schemas -- that would reintroduce the "which audit_log?"
-- ambiguity the schema split exists to remove). Most audit events are
-- tenant activity and belong in the tenant's own database; full tenant
-- deletion (retention.py) is the one record that must outlive the tenant's
-- own (removed) file, so it is written here instead (Task 5).
CREATE TABLE IF NOT EXISTS tenant_lifecycle_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, tenant_id TEXT, actor TEXT, role TEXT, action TEXT,
    resource TEXT, outcome TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_logs_ts ON api_logs(ts);
CREATE INDEX IF NOT EXISTS idx_api_logs_tenant ON api_logs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tenant_lifecycle_log_tenant ON tenant_lifecycle_log(tenant_id);
"""

TENANT_SCHEMA = """
CREATE TABLE IF NOT EXISTS db_owner (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    tenant_id TEXT NOT NULL,
    bound_at TEXT NOT NULL
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
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    node_id UNINDEXED,
    tenant_id UNINDEXED,
    title,
    summary,
    tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS knowledge_vectors (
    node_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    updated_at TEXT
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
CREATE TABLE IF NOT EXISTS llm_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, tenant_id TEXT, trace_id TEXT, seq INTEGER,
    stage TEXT, kind TEXT, payload TEXT,
    duration_ms REAL, tokens_in INTEGER, tokens_out INTEGER, ok INTEGER
);
CREATE INDEX IF NOT EXISTS idx_llm_traces_trace ON llm_traces(tenant_id, trace_id, seq);
CREATE TABLE IF NOT EXISTS stakeholder_answers (
    id TEXT PRIMARY KEY, tenant_id TEXT, question TEXT, user_id TEXT,
    category TEXT, answer TEXT, answer_mode TEXT, status TEXT,
    trace_id TEXT, created_at TEXT, source_node_ids TEXT, citations TEXT,
    facts TEXT, caveats TEXT, freshness REAL, tokens_in INTEGER,
    tokens_out INTEGER, cost REAL, escalated INTEGER, queries_run TEXT
);
CREATE TABLE IF NOT EXISTS stakeholder_feedback (
    id TEXT PRIMARY KEY, tenant_id TEXT, answer_id TEXT, user_id TEXT,
    rating TEXT, comment TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS stakeholder_conversations (
    id TEXT PRIMARY KEY, tenant_id TEXT, title TEXT, starred INTEGER DEFAULT 0,
    created_at TEXT, updated_at TEXT
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
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, tenant_id TEXT, actor TEXT, role TEXT, action TEXT,
    resource TEXT, outcome TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS company_profile_history (
    id TEXT PRIMARY KEY, tenant_id TEXT, version INTEGER,
    snapshot TEXT, changed_by TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS analyst_configs (
    tenant_id TEXT PRIMARY KEY, config TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS analyst_config_history (
    id TEXT PRIMARY KEY, tenant_id TEXT, version INTEGER,
    snapshot TEXT, changed_by TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS kpis (
    id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT, description TEXT,
    sql_query TEXT, frequency TEXT, is_active INTEGER, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_kn_tenant ON knowledge_nodes(tenant_id);
CREATE INDEX IF NOT EXISTS idx_runs_tenant ON analysis_runs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tel_tenant ON telemetry(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tel_trace ON telemetry(trace_id);
CREATE INDEX IF NOT EXISTS idx_sa_tenant ON stakeholder_answers(tenant_id);
CREATE INDEX IF NOT EXISTS idx_sconv_tenant ON stakeholder_conversations(tenant_id);
CREATE INDEX IF NOT EXISTS idx_rd_tenant ON research_docs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_log(tenant_id);
CREATE INDEX IF NOT EXISTS idx_cph_tenant ON company_profile_history(tenant_id);
CREATE INDEX IF NOT EXISTS idx_acfg_tenant ON analyst_configs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_ach_tenant ON analyst_config_history(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kpis_tenant ON kpis(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kv_tenant ON knowledge_vectors(tenant_id);
"""

# The pre-split single-file schema. Retained ONLY so `adopt-db` can open a legacy
# database. Never pass this to a Store that will be written to — a file with both
# planes is exactly the co-mingling this design removes.
SCHEMA_LEGACY_ALL = CONTROL_SCHEMA + TENANT_SCHEMA


def get_conn(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection, schema: str) -> None:
    """Apply one plane's schema. `schema` is required: defaulting it to the
    combined legacy schema is how a database ends up holding both planes."""
    with _LOCK:
        conn.executescript(schema)
        conn.commit()
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Non-destructive column additions so an existing DB picks up new fields.

    CREATE TABLE IF NOT EXISTS never adds columns to an already-created table, so
    add the CP-12 columns to `analysis_runs` when they are missing."""
    def _has(table: str) -> bool:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone())

    try:
        if _has("analysis_runs"):
            cols = {row[1] for row in conn.execute("PRAGMA table_info(analysis_runs)").fetchall()}
            for col in ("insights", "assumptions"):
                if col not in cols:
                    conn.execute(f"ALTER TABLE analysis_runs ADD COLUMN {col} TEXT")
            # CP-15: two-tier junior (low/high) + supporting-workpaper linkage
            for col in ("level", "category", "supportive_of"):
                if col not in cols:
                    conn.execute(f"ALTER TABLE analysis_runs ADD COLUMN {col} TEXT")

        # CP-15: Stakeholder queries_run migration
        if _has("stakeholder_answers"):
            sa_cols = {row[1] for row in conn.execute("PRAGMA table_info(stakeholder_answers)").fetchall()}
            if "queries_run" not in sa_cols:
                conn.execute("ALTER TABLE stakeholder_answers ADD COLUMN queries_run TEXT")
            if "conversation_id" not in sa_cols:
                conn.execute("ALTER TABLE stakeholder_answers ADD COLUMN conversation_id TEXT")
            if "python_cells" not in sa_cols:
                conn.execute("ALTER TABLE stakeholder_answers ADD COLUMN python_cells TEXT")
            if "produced_df_label" not in sa_cols:
                conn.execute("ALTER TABLE stakeholder_answers ADD COLUMN produced_df_label TEXT")
            if "extract_meta" not in sa_cols:
                conn.execute("ALTER TABLE stakeholder_answers ADD COLUMN extract_meta TEXT")
            if "analysis" not in sa_cols:
                conn.execute("ALTER TABLE stakeholder_answers ADD COLUMN analysis TEXT")

        # Brain retrieval: both recall legs must exist or search silently degrades.
        # Only tenant databases carry them — a control store legitimately has neither,
        # so key the check off knowledge_nodes rather than asserting unconditionally.
        is_tenant_db = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='knowledge_nodes'").fetchone())
        if is_tenant_db:
            have = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('knowledge_fts','knowledge_vectors')").fetchall()}
            missing = {"knowledge_fts", "knowledge_vectors"} - have
            if missing:
                raise RuntimeError(
                    f"Brain retrieval tables missing after schema init: "
                    f"{sorted(missing)}. SQLite may lack FTS5 support.")
        conn.commit()
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - column migrations must not block startup
        _LOG.warning("schema migration step failed: %s", exc, exc_info=True)


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
    """Thin wrapper: safe single write + serialization helpers.

    A Store is one SQLite file. Tenant stores hold one company's data and nothing
    else; the control store holds the cross-tenant registry.

    `schema` is REQUIRED. It used to default to `SCHEMA_LEGACY_ALL`, so
    `Store("anything.db")` silently produced a file holding both planes at once —
    the exact co-mingling this design exists to prevent. Every production caller
    now goes through `TenantStoreProvider`, which always passes `CONTROL_SCHEMA`
    or `TENANT_SCHEMA` explicitly, so nothing needs the permissive default and
    keeping it only leaves the footgun loaded.
    """

    def __init__(self, db_path: str, schema: str):
        self.db_path = db_path
        self.schema = schema
        self.conn = get_conn(db_path)
        init_db(self.conn, self.schema)

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