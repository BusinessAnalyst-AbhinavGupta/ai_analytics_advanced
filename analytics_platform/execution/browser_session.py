"""BrowserSessionExecutor: production execution via the open, authenticated Chrome tab.

Reality: Metabase is reachable ONLY through the human's logged-in browser session
(the cookie lives in Chrome). We execute by injecting JavaScript into the active
Metabase tab via AppleScript (same technique as core/table_fetcher.py). Crucially:

  * The cookie never leaves the browser: we use the page's same-origin `fetch`
    to call Metabase's /api/dataset with the browser's own session.
  * We never copy credentials/cookies into logs, the DB, or an LLM.
  * If a login page is detected we FAIL-WITH-PAUSE (SessionStatus=needs_login),
    never silently proceed.
  * `expected_host` verification happens BEFORE every execute (anti-tenant-bleed).

The OS-level runner is injectable (`osascript_runner`), so the whole gate/execute
logic is unit-testable offline (see tests/test_browser_session.py). Real runners:
  - `osascript_runner` : AppleScript -> active Chrome tab (the production path)
  - a Selenium/Playwright driver can be plugged behind the same interface later.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import pandas as pd

from .base import ExecutionContext, QueryResult, QueryExecutor, SessionStatus

Runner = Callable[[str], str]


def osascript_runner(js: str, timeout_s: float = 30.0) -> str:
    """Run JS in the active tab of Google Chrome via AppleScript; return stdout."""
    cmd = [
        "osascript", "-e",
        "tell application \"Google Chrome\" to execute front window's active tab javascript "
        + _shell_quote(js),
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s).stdout.strip()


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
  const isLoggedOut = !!document.body.innerText.includes('Sign in to Metabase');
  return JSON.stringify({host: loc, metabase: isMeta, login: isLogin || isLoggedOut,
                         title: document.title});
})();
"""


@dataclass
class BrowserExecutorConfig:
    database_id: Any = None
    expected_host: str = ""
    timeout_s: float = 30.0
    max_rows: int = 50000


class BrowserSessionExecutor(QueryExecutor):
    def __init__(self, metabase_base_url: str = "", database_id: Any = None,
                 expected_host: str = "", runner: Optional[Runner] = None,
                 timeout_s: float = 30.0, max_rows: int = 50000):
        self.base_url = metabase_base_url.rstrip("/")
        self.config = BrowserExecutorConfig(database_id=database_id,
                                            expected_host=expected_host,
                                            timeout_s=timeout_s, max_rows=max_rows)
        self._runner = runner or osascript_runner  # default = production AppleScript

    @classmethod
    def from_env(cls, runner: Optional[Runner] = None) -> "BrowserSessionExecutor":
        """Build a live executor from ANALYTICS_MB_* env vars (offline-testable)."""
        import os
        raw_id = os.environ.get("ANALYTICS_MB_DATABASE_ID", "").strip()
        database_id: Any = raw_id
        if raw_id.isdigit():
            database_id = int(raw_id)
        return cls(
            metabase_base_url=os.environ.get("ANALYTICS_MB_HOST", ""),
            database_id=database_id or None,
            expected_host=os.environ.get("ANALYTICS_MB_EXPECTED_HOST", ""),
            runner=runner,
        )

    def supports(self, ctx: ExecutionContext) -> bool:
        return True

    # -- internal -------------------------------------------------------------
    def _run(self, js: str) -> Optional[str]:
        try:
            return self._runner(js)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Chrome/AppleScript unavailable: {e}") from e

    # -- session gate ---------------------------------------------------------
    def session_status(self, tenant_id: str) -> SessionStatus:
        try:
            out = self._run(PROBE_JS)
            info = json.loads(out) if out else {}
        except Exception as e:  # noqa: BLE001
            return SessionStatus(state="unknown", tenant_id=tenant_id, browser_ok=False,
                                 detail=str(e))
        return self._evaluate_probe(info, tenant_id)

    def _evaluate_probe(self, info: Dict[str, Any], tenant_id: str) -> SessionStatus:
        host = str(info.get("host", "")).lower()
        if info.get("login"):
            return SessionStatus(state="needs_login", tenant_id=tenant_id, browser_ok=True,
                                 detail="Detected login/expired Metabase session (pause; ask human to log in)")
        if not info.get("metabase"):
            return SessionStatus(state="unknown", tenant_id=tenant_id, browser_ok=True,
                                 detail=f"Active tab is not Metabase ({host})")
        if self.config.expected_host and self.config.expected_host not in host:
            return SessionStatus(state="unknown", tenant_id=tenant_id, browser_ok=True,
                                 detail=f"Unexpected Metabase host {host}")
        return SessionStatus(state="valid", tenant_id=tenant_id, browser_ok=True,
                             detail=f"Metabase session OK on {host}")

    def ensure_session(self, tenant_id: str) -> SessionStatus:
        """Fail-with-pause guard: raise if session is not usable for execution."""
        s = self.session_status(tenant_id)
        if s.state != "valid":
            raise SessionUnavailable(s)
        return s

    # -- health ---------------------------------------------------------------
    def health_check(self) -> Dict[str, Any]:
        s = self.session_status(tenant_id="")
        return {"session_state": s.state, "browser_ok": s.browser_ok, "detail": s.detail}

    # -- execution via same-origin fetch (cookie stays in browser) -------------
    def execute(self, sql: str, ctx: ExecutionContext) -> QueryResult:
        if self.config.database_id is None:
            return QueryResult(ok=False, error="BrowserSessionExecutor requires a database_id")
        # Pre-execution session + host guard (fail-with-pause, never run blind).
        try:
            self.ensure_session(ctx.tenant_id)
        except SessionUnavailable as e:
            return QueryResult(ok=False, error=f"needs_login:{e.status.detail}")

        payload = json.dumps({
            "database": self.config.database_id,
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
            out = self._run(js)
            res = json.loads(out) if out else {}
        except Exception as e:  # noqa: BLE001
            return QueryResult(ok=False, error=f"Browser execution failed: {e}")
        if not res.get("ok"):
            return QueryResult(ok=False, error=res.get("error", "metabase error"))
        rows = res.get("rows", [])
        cols = res.get("cols", [])
        if len(rows) > self.config.max_rows:
            rows = rows[: self.config.max_rows]
        df = pd.DataFrame(rows, columns=cols)
        return QueryResult(ok=True, data=df, row_count=len(df), columns=cols)

    def cancel(self, execution_id: str) -> bool:
        return True


class SessionUnavailable(Exception):
    def __init__(self, status: SessionStatus):
        super().__init__(f"Session not usable: {status.state}")
        self.status = status


def make_live_executor(settings: Optional["Settings"] = None) -> BrowserSessionExecutor:
    """Build a live BrowserSessionExecutor from Settings (defaults to env)."""
    if settings is None:
        from ..config import Settings
        settings = Settings.from_env()
    raw_id = settings.metabase_database_id or ""
    database_id: Any = raw_id
    if isinstance(raw_id, str) and raw_id.isdigit():
        database_id = int(raw_id)
    return BrowserSessionExecutor(
        metabase_base_url=settings.metabase_base_url,
        database_id=database_id or None,
        expected_host=settings.metabase_expected_host,
    )


__all__ = ["BrowserSessionExecutor", "SessionUnavailable", "BrowserExecutorConfig",
           "osascript_runner", "PROBE_JS", "make_live_executor"]