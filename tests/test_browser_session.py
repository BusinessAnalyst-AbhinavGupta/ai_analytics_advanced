"""Offline tests for BrowserSessionExecutor (injectable OS runner — no live Chrome)."""
from __future__ import annotations

import json
import unittest

from analytics_platform.execution.base import ExecutionContext
from analytics_platform.execution.browser_session import (
    BrowserSessionExecutor,
    SessionUnavailable,
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


if __name__ == "__main__":
    unittest.main()