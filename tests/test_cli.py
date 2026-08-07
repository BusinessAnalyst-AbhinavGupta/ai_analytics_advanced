"""CLI tests (offline) — `browser` live-Metabase command wiring.

Uses a stubbed BrowserSessionExecutor (injected runner). No Chrome/OS calls happen.
"""
from __future__ import annotations

import argparse
import unittest
from unittest import mock

from analytics_platform.cli import build_parser, cmd_browser
from analytics_platform.execution.browser_session import BrowserSessionExecutor
from tests.test_browser_session import exec_payload, make_runner, probe_payload


def browser_args(**kw):
    base = dict(tenant_id="t1", sql="", database_id=None, expected_host="",
                host="", head=10)
    base.update(kw)
    return argparse.Namespace(**base)


class TestCliBrowser(unittest.TestCase):
    def test_parser_resolves_browser(self):
        args = build_parser().parse_args(["browser", "--sql", "SELECT 1"])
        self.assertIs(args.func, cmd_browser)
        self.assertEqual(args.sql, "SELECT 1")

    def test_session_ok_no_sql(self):
        stub = BrowserSessionExecutor(database_id=1, expected_host="metabase.acme",
                                      runner=make_runner(probe_payload(), exec_payload()))
        with mock.patch("analytics_platform.execution.browser_session.make_live_executor",
                        return_value=stub):
            rc = cmd_browser(browser_args())
        self.assertEqual(rc, 0)

    def test_executes_sql_when_valid(self):
        stub = BrowserSessionExecutor(database_id=1, expected_host="metabase.acme",
                                      runner=make_runner(
                                          probe_payload(),
                                          exec_payload(rows=[[1, "a"]],
                                                       cols=["id", "name"])))
        with mock.patch("analytics_platform.execution.browser_session.make_live_executor",
                        return_value=stub):
            rc = cmd_browser(browser_args(sql="SELECT 1"))
        self.assertEqual(rc, 0)

    def test_aborts_no_query_on_needs_login(self):
        stub = BrowserSessionExecutor(database_id=1,
                                      runner=make_runner(probe_payload(login=True),
                                                         exec_payload()))
        with mock.patch("analytics_platform.execution.browser_session.make_live_executor",
                        return_value=stub):
            rc = cmd_browser(browser_args(sql="SELECT 1"))
        self.assertEqual(rc, 1)  # fail-with-pause, never runs blind

    def test_cli_flags_override_env_config(self):
        stub = BrowserSessionExecutor(database_id=1, expected_host="env.example",
                                      runner=make_runner(probe_payload(), exec_payload()))
        args = browser_args(database_id=77, expected_host="metabase.acme",
                            host="https://cli.example")
        with mock.patch("analytics_platform.execution.browser_session.make_live_executor",
                        return_value=stub):
            rc = cmd_browser(args)
        self.assertEqual(rc, 0)
        self.assertEqual(stub.config.database_id, 77)
        self.assertEqual(stub.config.expected_host, "metabase.acme")
        self.assertEqual(stub.base_url, "https://cli.example")


if __name__ == "__main__":
    unittest.main()