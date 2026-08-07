import os
import sqlite3
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

DEFAULT_DB_PATH = os.path.join("checkpoints", "runs.db")

def get_db_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path: str = DEFAULT_DB_PATH):
    """Initializes the runs database and creates tables if they do not exist."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                sql_query TEXT NOT NULL,
                journey_stage_or_page TEXT,
                service_line TEXT,
                category TEXT,
                natco TEXT,
                tags TEXT,
                status TEXT NOT NULL,
                error_message TEXT,
                checkpoint_path TEXT,
                canonical_id TEXT
            )
        """)
        conn.commit()

def create_run(
    run_id: str,
    sql_query: str,
    journey_stage_or_page: str = "",
    service_line: str = "",
    category: str = "",
    natco: str = "",
    tags: str = "",
    status: str = "RUNNING",
    error_message: Optional[str] = None,
    checkpoint_path: Optional[str] = None,
    canonical_id: Optional[str] = None,
    db_path: str = DEFAULT_DB_PATH
) -> Dict[str, Any]:
    """Creates a new run entry in the database."""
    init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO runs (
                run_id, timestamp, sql_query, journey_stage_or_page,
                service_line, category, natco, tags,
                status, error_message, checkpoint_path, canonical_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id, now, sql_query, journey_stage_or_page,
            service_line, category, natco, tags,
            status, error_message, checkpoint_path, canonical_id
        ))
        conn.commit()
    return get_run(run_id, db_path)

def update_run(
    run_id: str,
    status: Optional[str] = None,
    error_message: Optional[str] = None,
    checkpoint_path: Optional[str] = None,
    canonical_id: Optional[str] = None,
    db_path: str = DEFAULT_DB_PATH
) -> Optional[Dict[str, Any]]:
    """Updates an existing run's status or metadata."""
    init_db(db_path)
    existing = get_run(run_id, db_path)
    if not existing:
        return None

    new_status = status if status is not None else existing["status"]
    new_error = error_message if error_message is not None else existing["error_message"]
    new_checkpoint = checkpoint_path if checkpoint_path is not None else existing["checkpoint_path"]
    new_canonical = canonical_id if canonical_id is not None else existing["canonical_id"]

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE runs
            SET status = ?, error_message = ?, checkpoint_path = ?, canonical_id = ?
            WHERE run_id = ?
        """, (new_status, new_error, new_checkpoint, new_canonical, run_id))
        conn.commit()

    return get_run(run_id, db_path)

def get_all_runs(db_path: str = DEFAULT_DB_PATH) -> List[Dict[str, Any]]:
    """Returns all runs ordered by timestamp descending."""
    init_db(db_path)
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM runs ORDER BY timestamp DESC")
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def get_run(run_id: str, db_path: str = DEFAULT_DB_PATH) -> Optional[Dict[str, Any]]:
    """Retrieves a single run by run_id."""
    init_db(db_path)
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def delete_run(run_id: str, db_path: str = DEFAULT_DB_PATH):
    """Deletes a run entry from the database."""
    init_db(db_path)
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        conn.commit()
