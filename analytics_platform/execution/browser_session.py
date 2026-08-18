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
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from .base import ExecutionContext, QueryResult, QueryExecutor, SessionStatus

Runner = Callable[[str], str]


def build_osascript_command(js: str, host: str = "") -> List[str]:
    """AppleScript to run `js` in a Chrome tab whose URL contains `host`.

    When `host` is empty it falls back to the front window's active tab (the old
    behaviour). Chrome's `execute javascript` returns its value, so we must
    `return` it — a bare `execute ... javascript ...` statement discards it and
    osascript would print nothing. Returns the osascript argv list (pure).
    """
    quoted = _shell_quote(js)
    if host:
        safe_host = host.replace('"', "")
        body = (
            "set found to false\n"
            "repeat with w in windows\n"
            "  repeat with t in tabs of w\n"
            f"    if URL of t contains \"{safe_host}\" then\n"
            "      set found to true\n"
            f"      return (execute t javascript {quoted})\n"
            "    end if\n"
            "  end repeat\n"
            "end repeat\n"
            "if not found then return (execute front window's active tab javascript "
            + quoted + ")\n"
        )
        return ["osascript", "-e", f"tell application \"Google Chrome\"\n{body}\nend tell"]
    return [
        "osascript", "-e",
        "tell application \"Google Chrome\" to execute front window's active tab javascript "
        + quoted,
    ]


def osascript_runner(js: str, timeout_s: float = 30.0, host: str = "") -> str:
    """Run JS in Chrome (optionally in a tab whose URL contains `host`); return stdout."""
    cmd = build_osascript_command(js, host)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s).stdout.strip()


def make_osascript_runner(host: str = "", timeout_s: float = 30.0) -> Runner:
    """Build a `Runner(js)` bound to a target Metabase host (default runner)."""
    def run(js: str) -> str:
        return osascript_runner(js, timeout_s=timeout_s, host=host)
    return run


def _shell_quote(js: str) -> str:
    # escape for AppleScript string literal
    return '"' + js.replace("\\", "\\\\").replace('"', '\\"') + '"'


# Chrome's AppleScript `execute javascript` returns SYNCHRONOUSLY and does NOT
# await promises — an `(async ()=>...)()` return value comes back empty. So, like
# the proven scripts/download_base_tables.py, we stash async results in a window
# global (__mb) and read them back with separate synchronous calls.
#
# Every kick is tagged with a nonce, stamped into `window.__mbNonce` SYNCHRONOUSLY
# before any async work starts. A roundtrip whose client-side poll times out
# doesn't cancel the in-flight browser-side fetch -- it can still resolve later,
# after a *newer* roundtrip has already reset and reclaimed the shared __mb slot.
# Without the nonce check, that orphaned completion silently overwrites the
# newer roundtrip's real result with stale data (found live: two distinct
# `execute()` calls in a row, the second one came back holding the first
# query's rows). Each completion handler only writes if `window.__mbNonce`
# still equals the nonce it captured at kick time -- a stale write's nonce
# will already have been replaced by the next roundtrip's kick, so it's
# silently dropped instead of clobbering the current result.
READ_STATE_JS = "(window.__mb && window.__mb.ready ? window.__mb.payload : '')"
RESET_JS = "window.__mb = {payload:null, ready:false}; 'reset'"


def _build_probe_kick_js(nonce: str) -> str:
    """Session-probe kick, guarded by `nonce` (see module docstring above)."""
    n = json.dumps(nonce)
    return (
        "window.__mb = {payload:null, ready:false}; window.__mbNonce = " + n + ";"
        "(function(){const myNonce=" + n + ";try{(async()=>{try{"
        "const loc=location.hostname.toLowerCase();"
        "const isMeta=loc.includes('metabase');"
        "const isLogin=!!document.querySelector('#login-group')"
        "||/login/i.test(location.pathname)||document.title.toLowerCase().includes('sign in');"
        "const isLoggedOut=!!document.body.innerText.includes('Sign in to Metabase');"
        "const result=JSON.stringify({host:loc,metabase:isMeta,login:isLogin||isLoggedOut,title:document.title});"
        "if(window.__mbNonce===myNonce){window.__mb.payload=result;window.__mb.ready=true;}"
        "}catch(e){if(window.__mbNonce===myNonce){window.__mb.payload=JSON.stringify({error:String(e)});window.__mb.ready=true;}}"
        "})();return 'kick';}catch(e){window.__mb={payload:JSON.stringify({error:String(e)}),ready:true};return 'kick';}})();"
    )


def _build_execute_kick_js(payload: Any, nonce: Optional[str] = None) -> str:
    """JS that kicks off the same-origin `/api/dataset` fetch, stashing into
    __mb, guarded by `nonce` (see module docstring above).

    `fetch` requires a string body, so the JSON payload is embedded as a JS
    string literal (`body:"{...}"`), never as a raw object (which fetch would
    send as "[object Object]").
    """
    if nonce is None:
        nonce = uuid.uuid4().hex
    body_text = payload if isinstance(payload, str) else json.dumps(payload)
    body_literal = json.dumps(body_text)   # JS string literal containing the JSON
    n = json.dumps(nonce)
    return (
        "window.__mb = {payload:null, ready:false}; window.__mbNonce = " + n + ";"
        "(function(){const myNonce=" + n + ";try{(async()=>{try{"
        "const r=await fetch('/api/dataset',{method:'POST',"
        "headers:{'content-type':'application/json'},body:"
        + body_literal +
        "});const j=await r.json();"
        "let result;"
        "if(j.error){result=JSON.stringify({ok:false,error:j.error||'metabase_error'});}"
        "else{const cols=(j.data&&j.data.cols||[]).map(c=>c.name);"
        "const rows=(j.data&&j.data.rows||[]).map(r=>r.slice());"
        "result=JSON.stringify({ok:true,cols:cols,rows:rows});}"
        "if(window.__mbNonce===myNonce){window.__mb.payload=result;window.__mb.ready=true;}"
        "}catch(e){if(window.__mbNonce===myNonce){window.__mb.payload=JSON.stringify({ok:false,error:String(e)});window.__mb.ready=true;}}"
        "})();return 'kick';}catch(e){window.__mb={payload:JSON.stringify({ok:false,error:String(e)}),ready:true};return 'kick';}})();"
    )


@dataclass
class BrowserExecutorConfig:
    database_id: Any = None
    expected_host: str = ""
    timeout_s: float = 30.0
    # None means "take the bound from ctx.row_limit". This is a MEMORY GUARD on
    # an already-received payload, not a transport limit -- see execute().
    max_rows: Optional[int] = None


class BrowserSessionExecutor(QueryExecutor):
    def __init__(self, metabase_base_url: str = "", database_id: Any = None,
                 expected_host: str = "", runner: Optional[Runner] = None,
                 timeout_s: float = 30.0, max_rows: Optional[int] = None):
        self.base_url = metabase_base_url.rstrip("/")
        self.config = BrowserExecutorConfig(database_id=database_id,
                                            expected_host=expected_host,
                                            timeout_s=timeout_s, max_rows=max_rows)
        self._default_runner = runner is None
        self._timeout_s = timeout_s
        self._runner = runner or make_osascript_runner(
            host=self._metabase_host_fragment(), timeout_s=timeout_s)
        # window.__mb (Chrome-tab-side) is a single shared stash for every
        # roundtrip -- session_status() and execute() both use it. This
        # instance is a process-wide singleton reused across concurrent HTTP
        # requests (FastAPI runs sync handlers in a threadpool), so two
        # roundtrips overlapping resets/reads of the SAME global would corrupt
        # each other's result. Serialize all of them through one lock.
        self._roundtrip_lock = threading.Lock()

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

    def _metabase_host_fragment(self) -> str:
        """Host strand used to find the Metabase tab in Chrome (URL contains)."""
        from urllib.parse import urlsplit
        if self.base_url:
            host = urlsplit(self.base_url).hostname
            if host:
                return host
        return self.config.expected_host

    def rebind_runner(self) -> None:
        """Recompute the default runner after `expected_host`/`base_url` changed."""
        if self._default_runner:
            self._runner = make_osascript_runner(
                host=self._metabase_host_fragment(), timeout_s=self._timeout_s)

    def supports(self, ctx: ExecutionContext) -> bool:
        return True

    # -- internal -------------------------------------------------------------
    def _run(self, js: str) -> Optional[str]:
        try:
            return self._runner(js)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Chrome/AppleScript unavailable: {e}") from e

    def _run_roundtrip(self, kick_js: str, timeout_s: float) -> str:
        """Kick async JS that stashes results in `window.__mb`, then poll the global
        synchronously (Chrome's `execute javascript` never awaits promises).

        Locked: window.__mb is one shared slot in the browser tab, so a second
        roundtrip's RESET_JS would otherwise wipe out a first roundtrip still
        polling for its own result.
        """
        import time as _time
        with self._roundtrip_lock:
            self._run(RESET_JS)
            self._run(kick_js)
            deadline = _time.monotonic() + max(timeout_s, 1.0)
            last = ""
            while _time.monotonic() < deadline:
                out = self._run(READ_STATE_JS) or ""
                if out:
                    last = out
                    break
                _time.sleep(0.1)
        return last

    # -- session gate ---------------------------------------------------------
    def session_status(self, tenant_id: str) -> SessionStatus:
        try:
            out = self._run_roundtrip(_build_probe_kick_js(uuid.uuid4().hex), self._timeout_s)
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

        payload = {
            "database": self.config.database_id,
            "type": "native",
            "native": {"query": sql},
            "parameters": [],
        }
        try:
            out = self._run_roundtrip(_build_execute_kick_js(payload), self._timeout_s)
            res = json.loads(out) if out else {}
        except Exception as e:  # noqa: BLE001
            return QueryResult(ok=False, error=f"Browser execution failed: {e}")
        if not res.get("ok"):
            return QueryResult(ok=False, error=res.get("error", "metabase error"))
        rows = res.get("rows", [])
        cols = res.get("cols", [])
        # WARNING TO THE NEXT READER: this slice runs AFTER the entire payload has
        # already crossed the AppleScript boundary -- Metabase's whole response was
        # JSON.stringify'd into window.__mb.payload and returned as one `osascript`
        # string, which was then json.loads'd above. It is therefore a MEMORY
        # GUARD, not a transport limit: raising it does not make that boundary
        # carry more, it only stops discarding what already arrived. The only
        # thing that bounds the *warehouse* is the LIMIT QueryPolicy injects,
        # which is why cubes are sized to fit and ID-grain rows are keyset-paged.
        bounds = [b for b in (self.config.max_rows, ctx.row_limit) if b]
        cap = min(bounds) if bounds else None
        warnings: List[str] = []
        if cap is not None and len(rows) > cap:
            rows = rows[:cap]
            # Callers set ExtractMeta.truncated off this. A silently shorter frame
            # is how a partial result becomes a confidently wrong total.
            warnings.append(f"result truncated at {cap} rows")
        df = pd.DataFrame(rows, columns=cols)
        return QueryResult(ok=True, data=df, row_count=len(df), columns=cols,
                           warnings=warnings)

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
           "osascript_runner", "build_osascript_command", "make_osascript_runner",
           "READ_STATE_JS", "RESET_JS", "_build_probe_kick_js",
           "_build_execute_kick_js", "make_live_executor"]