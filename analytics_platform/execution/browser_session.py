"""BrowserSessionExecutor: production execution via the open, authenticated Chrome tab.

Reality: Metabase is reachable ONLY through the human's logged-in browser session
(the cookie lives in Chrome). We execute by injecting JavaScript into the active
Metabase tab via AppleScript (same technique as core/table_fetcher.py). Crucially:

  * The cookie never leaves the browser: we use the page's same-origin `fetch`
    to call Metabase's /api/dataset with the browser's own session.
  * We never copy credentials/cookies into logs, the DB, or an LLM.
  * If a login page is detected we FAIL-WITH-PAUSE (SessionStatus=needs_login),
    never silently proceed.

This is default-first but isolated behind the QueryExecutor protocol, so other
executors (API, direct DB) can be added without touching analytics code.
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import Any, Dict, List, Optional

import pandas as pd

from .base import ExecutionContext, QueryResult, QueryExecutor, SessionStatus


def _osascript_js(js: str) -> str:
    """Run JS in the active tab of Google Chrome via AppleScript; return stdout."""
    cmd = [
        "osascript", "-e",
        "tell application \"Google Chrome\" to execute front window's active tab javascript "
        + _shell_quote(js),
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()


def _shell_quote(js: str) -> str:
    # escape for AppleScript string literal
    return '"' + js.replace("\\", "\\\\").replace('"', '\\"') + '"'


PROBE_JS = """
(async () => {
  const loc = location.hostname.toLowerCase();
  const isMeta = loc.includes('metabase');
  const isLogin = !!document.querySelector('#login-group') ||
    /login/i.test(location.pathname) ||
    document.title.toLowerCase().includes('sign in');
  return JSON.stringify({host: loc, metabase: isMeta, login: isLogin,
                         title: document.title});
})();
"""


class BrowserSessionExecutor(QueryExecutor):
    def __init__(self, metabase_base_url: str = "", database_id: Any = None,
                 expected_host: str = ""):
        self.base_url = metabase_base_url.rstrip("/")
        self.database_id = database_id
        self.expected_host = expected_host

    def supports(self, ctx: ExecutionContext) -> bool:
        return True

    # -- session gate ----------------------------------------------------------
    def session_status(self, tenant_id: str) -> SessionStatus:
        try:
            out = _osascript_js(PROBE_JS)
            info = json.loads(out)
        except Exception as e:  # noqa: BLE001
            return SessionStatus(state="unknown", tenant_id=tenant_id, browser_ok=False,
                                 detail=f"Chrome/AppleScript unavailable: {e}")
        if not info.get("metabase"):
            if info.get("login"):
                return SessionStatus(state="needs_login", tenant_id=tenant_id, browser_ok=True,
                                     detail="Metabase login page open in active tab")
            return SessionStatus(state="unknown", tenant_id=tenant_id, browser_ok=True,
                                 detail=f"Active tab is not Metabase ({info.get('host')})")
        if info.get("login"):
            return SessionStatus(state="needs_login", tenant_id=tenant_id, browser_ok=True,
                                 detail="Detected Metabase login page (pause; ask human to log in)")
        if self.expected_host and self.expected_host not in info.get("host", ""):
            return SessionStatus(state="unknown", tenant_id=tenant_id, browser_ok=True,
                                 detail=f"Unexpected Metabase host {info.get('host')}")
        return SessionStatus(state="valid", tenant_id=tenant_id, browser_ok=True,
                             detail=f"Metabase session OK on {info.get('host')}")

    # -- execution via same-origin fetch (cookie stays in browser) --------------
    def execute(self, sql: str, ctx: ExecutionContext) -> QueryResult:
        if self.database_id is None:
            return QueryResult(ok=False, error="BrowserSessionExecutor requires a database_id")
        payload = json.dumps({
            "database": self.database_id,
            "type": "native",
            "native": {"query": sql},
            "parameters": [],
        })
        js = (
            "(async () => {"
            "  const r = await fetch('/api/dataset', {method:'POST',"
            "    headers:{'content-type':'application/json'},"
            f"    body: {json.dumps(payload)}"
            "  });"
            "  const j = await r.json();"
            "  if (j.error) return JSON.stringify({ok:false, error:j.error || 'metabase_error'});"
            "  const cols = (j.data && j.data.cols || []).map(c => c.name);"
            "  const rows = (j.data && j.data.rows || []).map(r => r.slice());"
            "  return JSON.stringify({ok:true, cols:cols, rows:rows});"
            "})();"
        )
        try:
            out = _osascript_js(js)
        except Exception as e:  # noqa: BLE001
            return QueryResult(ok=False, error=f"Browser execution failed: {e}")
        try:
            res = json.loads(out)
        except Exception:
            return QueryResult(ok=False, error="Could not parse Metabase response from browser")
        if not res.get("ok"):
            return QueryResult(ok=False, error=res.get("error", "metabase error"))
        df = pd.DataFrame(res.get("rows", []), columns=res.get("cols", []))
        return QueryResult(ok=True, data=df, row_count=len(df), columns=list(df.columns))

    def cancel(self, execution_id: str) -> bool:
        return True