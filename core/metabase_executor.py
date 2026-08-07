"""
Metabase Query Executor — Executes SQL queries against Metabase via the
authenticated Chrome browser session using osascript → JavaScript injection.

Safety & Non-Intrusiveness:
1. Targets ONLY the specific tab containing Metabase (URL contains 'metabase').
   Never executes on active/front tabs of other websites (SSO portals, Jira, etc.).
2. Uses credentials: 'include' to preserve active SSO cookies.
3. Never touches window.location, navigation, or document storage.
4. Cleanly distinguishes MetabaseUnreachableError vs MetabaseQueryExecutionError.
"""
import os
import time
import json
import base64
import subprocess
import tempfile
import logging
import pandas as pd
from typing import Tuple, Optional, Callable, Dict, Any

logger = logging.getLogger(__name__)


class MetabaseError(Exception):
    """Base exception for all Metabase operations."""
    pass


class MetabaseUnreachableError(MetabaseError):
    """Raised when Metabase or Chrome is not reachable. NOT eligible for SQL auto-healing."""
    def __init__(self, message: str):
        super().__init__(message)
        self.is_reachable = False


class MetabaseQueryExecutionError(MetabaseError):
    """Raised when Metabase executed the query but the SQL engine failed. ELIGIBLE for SQL auto-healing."""
    def __init__(self, error_message: str, status_code: int = 400, raw_error: str = ""):
        super().__init__(error_message)
        self.is_reachable = True
        self.error_message = error_message
        self.status_code = status_code
        self.raw_error = raw_error


class MetabaseExecutor:
    """Executes arbitrary SQL on Metabase via the user's authenticated Chrome session."""

    DEFAULT_DATABASE_ID = 59
    DEFAULT_TIMEOUT = 1200  # 1200 seconds (20 minutes) for long-running analytics queries
    CHUNK_SIZE = 500_000  # 500 KB per osascript chunk

    # ── Chrome / AppleScript helpers ──────────────────────────────

    @staticmethod
    def is_chrome_running() -> bool:
        try:
            return subprocess.run(
                ["pgrep", "-x", "Google Chrome"],
                capture_output=True, text=True,
            ).returncode == 0
        except Exception:
            return False

    @staticmethod
    def _execute_chrome_js(js_code: str) -> str:
        """
        Executes JavaScript specifically on the Chrome tab that is on Metabase.
        Never touches front tabs of other applications, SSO portals, or websites.
        """
        b64 = base64.b64encode(js_code.encode("utf-8")).decode("ascii")
        apple = f'''
        tell application "Google Chrome"
            set foundTab to false
            set resultStr to ""
            repeat with w in windows
                repeat with t in tabs of w
                    if URL of t contains "metabase" then
                        set resultStr to (execute t javascript "try {{ eval(atob('{b64}')); }} catch (e) {{ 'ERROR: ' + e.toString(); }}")
                        set foundTab to true
                        exit repeat
                    end if
                end repeat
                if foundTab then exit repeat
            end repeat
            if not foundTab then
                error "NO_METABASE_TAB_FOUND"
            end if
            return resultStr
        end tell
        '''
        proc = subprocess.run(["osascript", "-e", apple], capture_output=True, text=True)
        if proc.returncode != 0:
            err_msg = proc.stderr.strip() or "AppleScript execution error"
            if "NO_METABASE_TAB_FOUND" in err_msg:
                raise MetabaseUnreachableError(
                    "No active tab with URL containing 'metabase' was found in Google Chrome. "
                    "Please open Metabase (https://metabase.om.yo-digital.com) in Chrome."
                )
            raise MetabaseUnreachableError(
                f"Could not communicate with Google Chrome: {err_msg}"
            )
        return proc.stdout.strip()

    # ── Public API ────────────────────────────────────────────────

    def execute_query(
        self,
        sql: str,
        database_id: int = DEFAULT_DATABASE_ID,
        timeout: int = DEFAULT_TIMEOUT,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> pd.DataFrame:
        """
        Run *sql* on Metabase and return the result as a DataFrame.

        Raises
        ------
        MetabaseUnreachableError: If Chrome is closed, tab not found, not logged in, or network fails.
        MetabaseQueryExecutionError: If Metabase is reachable but query fails on the database engine.
        """
        if not self.is_chrome_running():
            raise MetabaseUnreachableError(
                "Google Chrome is not running. Please open Chrome with your "
                "Telekom profile and navigate to Metabase (https://metabase.om.yo-digital.com) before running queries."
            )

        def _log(msg: str):
            if progress_cb:
                progress_cb(msg)
            logger.info(msg)

        _log("📡 Dispatching SQL to Metabase tab in Chrome …")

        # 1. Inject async fetch into the verified Metabase Chrome tab
        escaped_sql = sql.replace("\\", "\\\\").replace("`", "\\`").replace("'", "\\'").replace('"', '\\"').replace("\n", "\\n")
        dispatch_js = f"""
        window.__mb_exec_status = "RUNNING";
        window.__mb_exec_data   = null;
        window.__mb_exec_error  = null;
        (async function() {{
            try {{
                const payload = {{
                    database: {database_id},
                    "lib/type": "mbql/query",
                    stages: [{{
                        "lib/type": "mbql.stage/native",
                        native: "{escaped_sql}",
                        "template-tags": []
                    }}]
                }};
                const res = await fetch('/api/dataset/csv', {{
                    method: 'POST',
                    credentials: 'include',
                    headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
                    body: new URLSearchParams({{ query: JSON.stringify(payload) }})
                }});
                if (!res.ok) {{
                    const body = await res.text();
                    window.__mb_exec_error  = JSON.stringify({{ status: res.status, body: body }});
                    window.__mb_exec_status = 'ERROR:' + res.status;
                    return;
                }}
                window.__mb_exec_data   = await res.text();
                window.__mb_exec_status = 'SUCCESS:' + window.__mb_exec_data.length;
            }} catch(e) {{
                window.__mb_exec_error  = JSON.stringify({{ status: 0, exception: e.toString() }});
                window.__mb_exec_status = 'EXCEPTION';
            }}
        }})();
        window.__mb_exec_status;
        """
        try:
            res_disp = self._execute_chrome_js(dispatch_js)
            if res_disp.startswith("ERROR:"):
                raise MetabaseUnreachableError(
                    f"JavaScript execution in Metabase tab failed: {res_disp}."
                )
        except MetabaseUnreachableError:
            raise
        except Exception as e:
            raise MetabaseUnreachableError(f"Could not inject query into Chrome: {e}")

        # 2. Poll for completion
        _log("⏳ Waiting for Metabase query execution …")
        deadline = time.time() + timeout
        status = "RUNNING"
        while time.time() < deadline:
            time.sleep(2)
            try:
                status = self._execute_chrome_js("window.__mb_exec_status")
            except Exception:
                continue

            if status.startswith("SUCCESS"):
                break
            if status.startswith("ERROR") or status == "EXCEPTION":
                raw_err_json = "{}"
                try:
                    raw_err_json = self._execute_chrome_js("window.__mb_exec_error || '{}'")
                except Exception:
                    pass
                
                # Parse structured error
                status_code = 0
                error_body = ""
                try:
                    err_parsed = json.loads(raw_err_json)
                    status_code = err_parsed.get("status", 0)
                    error_body = err_parsed.get("body") or err_parsed.get("exception", raw_err_json)
                except Exception:
                    error_body = raw_err_json

                # Classify reachability vs. query execution error
                if status_code in (401, 403):
                    raise MetabaseUnreachableError(
                        f"Metabase Authentication Required ({status_code}). Please log into Metabase in Chrome."
                    )
                elif status_code == 404:
                    raise MetabaseUnreachableError(
                        "Metabase endpoint `/api/dataset/csv` not found (404)."
                    )
                elif status_code == 0 or "Failed to fetch" in error_body or "NetworkError" in error_body:
                    raise MetabaseUnreachableError(
                        f"Metabase network connection error in Chrome: {error_body}. "
                        "Check your VPN and verify Metabase is reachable."
                    )
                else:
                    # Metabase was reached and executed the query, but SQL engine failed
                    clean_msg = error_body
                    try:
                        mb_err_obj = json.loads(error_body)
                        if isinstance(mb_err_obj, dict):
                            clean_msg = mb_err_obj.get("error") or mb_err_obj.get("message") or error_body
                    except Exception:
                        pass

                    raise MetabaseQueryExecutionError(
                        error_message=clean_msg,
                        status_code=status_code,
                        raw_error=error_body
                    )

        if not status.startswith("SUCCESS"):
            raise MetabaseUnreachableError(
                f"Metabase query timed out after {timeout}s without returning a response."
            )

        # 3. Stream CSV from browser memory → temp file
        total_len = int(self._execute_chrome_js(
            "window.__mb_exec_data ? window.__mb_exec_data.length.toString() : '0'"
        ))
        if total_len == 0:
            raise MetabaseQueryExecutionError(
                "Query returned 0 bytes / empty response body from Metabase.",
                status_code=200
            )

        _log(f"📥 Streaming {total_len:,} bytes from Chrome …")

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8",
            dir=os.path.join(os.path.dirname(__file__), "..", "data", "eda_reports"),
        )
        try:
            for offset in range(0, total_len, self.CHUNK_SIZE):
                chunk = self._execute_chrome_js(
                    f"window.__mb_exec_data.substring({offset}, {offset + self.CHUNK_SIZE})"
                )
                tmp.write(chunk)
            tmp.close()

            _log("✅ CSV downloaded. Loading into DataFrame …")
            df = pd.read_csv(tmp.name, low_memory=False)
            return df
        finally:
            # Clean up memory on window object
            try:
                self._execute_chrome_js("window.__mb_exec_data = null; window.__mb_exec_status = 'IDLE'; window.__mb_exec_error = null;")
            except Exception:
                pass
            if os.path.exists(tmp.name):
                try:
                    os.remove(tmp.name)
                except Exception:
                    pass

    def is_query_valid(
        self,
        sql: str,
        database_id: int = DEFAULT_DATABASE_ID,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> Tuple[bool, str, int]:
        """Lightweight check: execute and return (success, message, row_count)."""
        try:
            df = self.execute_query(sql, database_id=database_id, timeout=timeout)
            return True, f"OK — {len(df):,} rows × {len(df.columns)} columns", len(df)
        except MetabaseQueryExecutionError as e:
            return False, f"SQL Execution Error: {e.error_message}", 0
        except MetabaseUnreachableError as e:
            return False, f"Metabase Unreachable: {e}", 0
        except Exception as e:
            return False, str(e), 0
