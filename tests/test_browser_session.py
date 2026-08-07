"""Offline tests for BrowserSessionExecutor (injectable OS runner — no live Chrome)."""
from __future__ import annotations

import json
import os
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
    """Runner returns the probe JSON for session probes, else the execute JSON."""
    def runner(js: str) -> str:
        if "location.hostname" in js:      # PROBE_JS
            return probe
        return exec_resp
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
        self.assertIn("execute t javascript", script)
        self.assertIn("console.log('x')", script)
        # fallback remains so a missing tab still reports instead of silently failing
        self.assertIn("if not found then execute front window's active tab", script)

    def test_build_command_no_host_uses_active_tab(self):
        cmd = build_osascript_command("console.log('x')")
        script = " ".join(cmd)
        self.assertIn("front window's active tab", script)
        self.assertNotIn("URL of t contains", script)

    def test_make_osascript_runner_is_callable(self):
        from analytics_platform.execution.browser_session import Runner
        rn = make_osascript_runner(host="mb.example")
        self.assertTrue(callable(rn))

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