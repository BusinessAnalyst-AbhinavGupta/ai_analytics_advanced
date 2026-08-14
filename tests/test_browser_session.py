"""Offline tests for BrowserSessionExecutor (injectable OS runner — no live Chrome)."""
from __future__ import annotations

import json
import os
import re
import unittest

from analytics_platform.config import Settings
from analytics_platform.execution.base import ExecutionContext
from analytics_platform.execution.browser_session import (
    BrowserSessionExecutor,
    SessionUnavailable,
    build_osascript_command,
    make_live_executor,
    make_osascript_runner,
)


def probe_payload(**overrides):
    d = {"host": "metabase.acme.internal", "metabase": True, "login": False, "title": "Metabase"}
    d.update(overrides)
    return json.dumps(d)


def exec_payload(ok=True, rows=None, cols=None, error=""):
    return json.dumps({"ok": ok, "rows": rows or [], "cols": cols or [], "error": error})


def make_runner(probe, exec_resp):
    """Stateful fake of the reset -> kick -> read protocol used by _run_roundtrip."""
    state = {"payload": "", "ready": False}

    def runner(js: str) -> str:
        if "'reset'" in js:                 # RESET_JS
            state["payload"] = ""
            state["ready"] = False
            return "reset"
        if "__mb.ready ?" in js:            # READ_STATE_JS
            return state["payload"] if state["ready"] else ""
        # a kick: probe kick contains location.hostname; execute kick contains fetch
        state["payload"] = probe if "location.hostname" in js else exec_resp
        state["ready"] = True
        return "kick"

    return runner


class TestBrowserSession(unittest.TestCase):
    def test_valid_session(self):
        ex = BrowserSessionExecutor(database_id=1, expected_host="metabase.acme",
                                    runner=make_runner(probe_payload(), exec_payload()))
        st = ex.session_status("t")
        self.assertEqual(st.state, "valid")
        ex.ensure_session("t")  # no raise

    def test_login_page_pauses(self):
        probe = probe_payload(login=True)
        ex = BrowserSessionExecutor(database_id=1, runner=make_runner(probe, exec_payload()))
        self.assertEqual(ex.session_status("t").state, "needs_login")
        with self.assertRaises(SessionUnavailable):
            ex.ensure_session("t")

    def test_not_metabase(self):
        probe = probe_payload(metabase=False, host="app.example.com")
        ex = BrowserSessionExecutor(database_id=1, runner=make_runner(probe, exec_payload()))
        self.assertEqual(ex.session_status("t").state, "unknown")

    def test_wrong_host_blocked(self):
        probe = probe_payload(host="metabase.evil.com")
        ex = BrowserSessionExecutor(database_id=1, expected_host="metabase.acme",
                                    runner=make_runner(probe, exec_payload()))
        self.assertEqual(ex.session_status("t").state, "unknown")

    def test_execute_runs_after_session_gate(self):
        resp = exec_payload(rows=[[1, "a"]], cols=["id", "name"])
        ex = BrowserSessionExecutor(database_id=1, expected_host="metabase.acme",
                                    runner=make_runner(probe_payload(), resp))
        r = ex.execute("SELECT 1", ExecutionContext(tenant_id="t"))
        self.assertTrue(r.ok)
        self.assertEqual(r.row_count, 1)
        self.assertEqual(r.columns, ["id", "name"])

    def test_execute_blocks_on_login(self):
        ex = BrowserSessionExecutor(database_id=1,
                                    runner=make_runner(probe_payload(login=True), exec_payload()))
        r = ex.execute("SELECT 1", ExecutionContext(tenant_id="t"))
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith("needs_login"))

    def test_execute_metabase_error(self):
        ex = BrowserSessionExecutor(database_id=1,
                                    runner=make_runner(probe_payload(),
                                                       exec_payload(ok=False, error="wham")))
        r = ex.execute("SELECT 1", ExecutionContext(tenant_id="t"))
        self.assertFalse(r.ok)
        self.assertIn("wham", r.error)

    def test_execute_requires_database_id(self):
        ex = BrowserSessionExecutor(
            runner=make_runner(probe_payload(), exec_payload(rows=[[1]], cols=["x"])))
        r = ex.execute("SELECT 1", ExecutionContext(tenant_id="t"))
        self.assertFalse(r.ok)
        self.assertIn("database_id", r.error)

    def test_max_rows_cap(self):
        rows = [[i] for i in range(60)]
        ex = BrowserSessionExecutor(database_id=1, max_rows=10,
                                    runner=make_runner(probe_payload(),
                                                       exec_payload(rows=rows, cols=["x"])))
        r = ex.execute("SELECT 1", ExecutionContext(tenant_id="t"))
        self.assertEqual(r.row_count, 10)

    def test_concurrent_roundtrips_are_serialized(self):
        """window.__mb is one shared slot in the real browser tab -- e.g. a
        background session-health poll and a real query, or two concurrent
        requests, could otherwise have one roundtrip's RESET_JS wipe out
        another's in-flight kick before it polls its own result.

        Directly proves mutual exclusion rather than hoping a data race
        manifests under timing luck: tracks how many roundtrips are ever
        simultaneously between "kick" and publishing their result, across
        several concurrent execute() calls. That count must never exceed 1.
        """
        import threading
        import time as _time

        counter_lock = threading.Lock()  # guards the counter itself, not the
                                         # executor's lock under test
        state = {"payload": "", "ready": False, "in_flight": 0, "max_seen": 0}

        def runner(js: str) -> str:
            if "'reset'" in js:
                state["payload"], state["ready"] = "", False
                return "reset"
            if "__mb.ready ?" in js:
                return state["payload"] if state["ready"] else ""
            # A kick (session probe or query execute) -- represents work in
            # flight in the tab. Hold this window open with a sleep so any
            # unserialized second roundtrip's kick has a chance to overlap.
            with counter_lock:
                state["in_flight"] += 1
                state["max_seen"] = max(state["max_seen"], state["in_flight"])
            _time.sleep(0.02)
            with counter_lock:
                state["in_flight"] -= 1
            payload = probe_payload() if "location.hostname" in js else exec_payload(rows=[[1]], cols=["n"])
            state["payload"], state["ready"] = payload, True
            return "kick"

        ex = BrowserSessionExecutor(database_id=1, expected_host="metabase.acme", runner=runner)

        def run_one() -> None:
            ex.execute("SELECT 1", ExecutionContext(tenant_id="t"))

        threads = [threading.Thread(target=run_one) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(state["max_seen"], 1,
                         f"{state['max_seen']} roundtrips were in flight at the same "
                         "time -- window.__mb access is not actually serialized")

    def test_stale_late_response_does_not_clobber_a_newer_query(self):
        """A roundtrip that times out client-side doesn't cancel the browser-side
        fetch -- it can still resolve later, after a newer roundtrip has already
        reset and claimed the shared window.__mb slot. Found live: two distinct
        execute() calls in a row, the second one came back holding the first
        query's rows. Each kick is nonce-tagged; a completion handler only
        writes if window.__mbNonce still matches the nonce it captured at kick
        time, so a late duplicate write from an old roundtrip must be dropped
        instead of overwriting the current one."""
        import re

        state = {"mbNonce": None, "payload": "", "ready": False}
        pending = []  # queued (nonce, response_json) "async" completions

        def extract_nonce(js: str) -> str:
            m = re.search(r'__mbNonce\s*=\s*"([^"]+)"', js)
            return m.group(1) if m else ""

        def flush_pending():
            while pending:
                n, resp = pending.pop(0)
                if state["mbNonce"] == n:
                    state["payload"], state["ready"] = resp, True

        def runner(js: str) -> str:
            if "'reset'" in js:
                state["payload"], state["ready"] = "", False
                return "reset"
            if "__mb.ready ?" in js:
                flush_pending()
                return state["payload"] if state["ready"] else ""
            nonce = extract_nonce(js)
            state["mbNonce"] = nonce
            resp = (probe_payload() if "location.hostname" in js
                    else exec_payload(rows=[[nonce]], cols=["marker"]))
            pending.append((nonce, resp))
            return "kick"

        ex = BrowserSessionExecutor(database_id=1, expected_host="metabase.acme", runner=runner)

        r1 = ex.execute("SELECT 1", ExecutionContext(tenant_id="t"))
        self.assertTrue(r1.ok)
        nonce1 = r1.data.iloc[0]["marker"]

        r2 = ex.execute("SELECT 2", ExecutionContext(tenant_id="t"))
        self.assertTrue(r2.ok)
        nonce2 = r2.data.iloc[0]["marker"]
        self.assertNotEqual(nonce1, nonce2)

        # Query 1's fetch resolves a second time (a late/duplicate completion),
        # arriving only now -- after query 2 has already claimed the slot.
        pending.append((nonce1, exec_payload(rows=[["STALE"]], cols=["marker"])))
        flush_pending()

        self.assertEqual(json.loads(state["payload"])["rows"][0][0], nonce2,
                         "a late write from an earlier, already-superseded "
                         "roundtrip overwrote the current result")


class TestBrowserFromEnv(unittest.TestCase):
    ENV = ("ANALYTICS_MB_LIVE", "ANALYTICS_MB_HOST", "ANALYTICS_MB_DATABASE_ID",
           "ANALYTICS_MB_EXPECTED_HOST")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV}
        for k in self.ENV:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_from_env_defaults_absent(self):
        ex = BrowserSessionExecutor.from_env()
        self.assertIsNone(ex.config.database_id)
        self.assertEqual(ex.config.expected_host, "")
        self.assertEqual(ex.base_url, "")

    def test_from_env_parses_int_database_id(self):
        os.environ["ANALYTICS_MB_DATABASE_ID"] = "42"
        os.environ["ANALYTICS_MB_EXPECTED_HOST"] = "metabase.acme.internal"
        os.environ["ANALYTICS_MB_HOST"] = "https://metabase.acme.internal"
        ex = BrowserSessionExecutor.from_env()
        self.assertEqual(ex.config.database_id, 42)
        self.assertEqual(ex.config.expected_host, "metabase.acme.internal")
        self.assertEqual(ex.base_url, "https://metabase.acme.internal")

    def test_make_live_executor_from_settings(self):
        s = Settings(metabase_database_id="7", metabase_expected_host="mb.example.com",
                     metabase_base_url="https://mb.example.com")
        ex = make_live_executor(s)
        self.assertEqual(ex.config.database_id, 7)
        self.assertEqual(ex.config.expected_host, "mb.example.com")

    def test_make_live_executor_defaults_env(self):
        os.environ["ANALYTICS_MB_DATABASE_ID"] = "99"
        os.environ["ANALYTICS_MB_EXPECTED_HOST"] = "mb.env.example"
        ex = make_live_executor()
        self.assertEqual(ex.config.database_id, 99)
        self.assertEqual(ex.config.expected_host, "mb.env.example")

    def test_settings_live_gate_from_env(self):
        self.assertFalse(Settings.from_env().metabase_live)
        os.environ["ANALYTICS_MB_LIVE"] = "1"
        self.assertTrue(Settings.from_env().metabase_live)

    def test_from_env_executes_offline_with_stub_runner(self):
        os.environ["ANALYTICS_MB_DATABASE_ID"] = "5"
        ex = BrowserSessionExecutor.from_env(
            runner=make_runner(probe_payload(), exec_payload(rows=[[1]], cols=["x"])))
        r = ex.execute("SELECT 1", ExecutionContext(tenant_id="t"))
        self.assertTrue(r.ok)
        self.assertEqual(r.row_count, 1)


class TestOsascriptTargeting(unittest.TestCase):
    def test_build_command_targets_host_tab_with_js(self):
        cmd = build_osascript_command("console.log('x')", "metabase.om.yo-digital.com")
        script = " ".join(cmd)
        self.assertIn("URL of t contains \"metabase.om.yo-digital.com\"", script)
        # the JS result must be RETURNED, not discarded by a bare statement
        self.assertIn("return (execute t javascript", script)
        self.assertIn("console.log('x')", script)
        # fallback remains so a missing tab still reports instead of silently failing
        self.assertIn("return (execute front window's active tab", script)

    def test_build_command_no_host_uses_active_tab(self):
        cmd = build_osascript_command("console.log('x')")
        script = " ".join(cmd)
        self.assertIn("front window's active tab", script)
        self.assertNotIn("URL of t contains", script)

    def test_make_osascript_runner_is_callable(self):
        from analytics_platform.execution.browser_session import Runner
        rn = make_osascript_runner(host="mb.example")
        self.assertTrue(callable(rn))

    def test_execute_kick_embeds_body_as_string_literal(self):
        from analytics_platform.execution.browser_session import _build_execute_kick_js
        js = _build_execute_kick_js({"database": 59, "type": "native",
                                     "native": {"query": "SELECT 1"}})
        # fetch must receive a string body, not a raw object -> "[object Object]"
        self.assertNotIn("body:{", js)
        self.assertIn('body:"', js)
        self.assertIn("database", js)

    def test_executor_default_runner_targets_expected_host(self):
        ex = BrowserSessionExecutor(database_id=1, expected_host="mb.example.com")
        self.assertTrue(ex._default_runner)
        self.assertEqual(ex._metabase_host_fragment(), "mb.example.com")
        # CLI-style override then rebind
        ex.config.expected_host = "mb2.example.com"
        ex.rebind_runner()
        self.assertEqual(ex._metabase_host_fragment(), "mb2.example.com")

    def test_host_fragment_prefers_base_url_netloc(self):
        ex = BrowserSessionExecutor(database_id=1, metabase_base_url="https://mb.example.com:3000",
                                    expected_host="mb.example.com")
        self.assertEqual(ex._metabase_host_fragment(), "mb.example.com")


if __name__ == "__main__":
    unittest.main()