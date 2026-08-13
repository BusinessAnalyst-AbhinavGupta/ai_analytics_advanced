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


    def test_parser_resolves_review(self):
        args = build_parser().parse_args(["review", "t1"])
        from analytics_platform.cli import cmd_review
        self.assertIs(args.func, cmd_review)

    def test_parser_resolves_junior(self):
        args = build_parser().parse_args(["junior", "t1"])
        from analytics_platform.cli import cmd_junior
        self.assertIs(args.func, cmd_junior)

    def _temp_ctx(self):
        import os
        import tempfile
        from unittest import mock
        from analytics_platform.api import make_context
        from analytics_platform.domain import NodeKind
        self._tmp = tempfile.TemporaryDirectory()
        # ANALYTICS_DATA_DIR (not ANALYTICS_DB_PATH) is what the post-split
        # resolvers key off: Settings.resolve_control_db_path() and
        # resolve_tenants_root() both derive from `data_dir`. Setting
        # ANALYTICS_DB_PATH here isolated nothing — it left every run writing
        # data/control.db and tenants/<id>/tenant.db into the real repo tree.
        patcher = mock.patch.dict(os.environ,
                                  {"ANALYTICS_DATA_DIR": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        ctx = make_context()
        self.addCleanup(ctx.stores.close_all)
        return ctx, NodeKind

    def test_cmd_review_summary_lists_queue(self):
        from analytics_platform.cli import cmd_review
        ctx, NodeKind = self._temp_ctx()
        tid = ctx.tenants.create_tenant("T").id
        ctx.pipeline.brain(tid).create(NodeKind.QUERY, "q1")
        ctx.pipeline.brain(tid).create(NodeKind.IDIOM, "i1")
        rc = cmd_review(argparse.Namespace(tenant_id=tid, kind="", limit=50,
            approve="", reject="", bulk_approve=False, bulk_reject=False,
            by="senior", notes="", conflicts=False, conflict_limit=20, quiet=False))
        self.assertEqual(rc, 0)

    def test_cmd_review_approve_updates_status(self):
        from analytics_platform.cli import cmd_review
        from analytics_platform.domain import ReviewStatus
        ctx, NodeKind = self._temp_ctx()
        tid = ctx.tenants.create_tenant("T").id
        brain = ctx.pipeline.brain(tid)
        n = brain.create(NodeKind.QUERY, "q1")
        rc = cmd_review(argparse.Namespace(tenant_id=tid, kind="", limit=50,
            approve=n.id, reject="", bulk_approve=False, bulk_reject=False,
            by="senior", notes="", conflicts=False, conflict_limit=20, quiet=True))
        self.assertEqual(rc, 0)
        self.assertEqual(brain.get(n.id).status, ReviewStatus.APPROVED)


class _ShimJunior:
    """Record the executor handed to JuniorEngine without running stage/catalog/etc."""

    instances = []

    def __init__(self, *a, **k):
        self.kwargs = k
        _ShimJunior.instances.append(self)

    def stage(self, *a, **k):
        return {}

    def refresh_catalog(self, *a, **k):
        return {"tables_known": 0, "tables_described": 0, "tables": []}

    def suggest_questions(self, *a, **k):
        return {"count": 0, "suggestions": []}


def _junior_ctx(metabase_live: bool):
    """A minimal ctx for cmd_junior so executor selection is what we assert."""
    from unittest import mock
    ctx = mock.Mock()
    ctx.settings = mock.Mock(metabase_live=metabase_live)
    ctx.store = object()
    ctx.tenants = mock.Mock()
    ctx.observability = object()
    ctx.executor = "offline-executor"
    return ctx


class TestCliJunior(unittest.TestCase):
    def setUp(self):
        _ShimJunior.instances = []

    def test_uses_live_executor_when_metabase_live(self):
        from unittest import mock
        from analytics_platform.cli import cmd_junior
        ctx = _junior_ctx(metabase_live=True)
        live_ex = "live-executor"
        with mock.patch("analytics_platform.cli.make_context", return_value=ctx), \
                mock.patch("analytics_platform.junior.JuniorEngine", _ShimJunior), \
                mock.patch("analytics_platform.execution.browser_session.make_live_executor",
                           return_value=live_ex) as mle:
            rc = cmd_junior(argparse.Namespace(tenant_id="t1", limit=50))
        self.assertEqual(rc, 0)
        mle.assert_called_once()
        self.assertIs(_ShimJunior.instances[0].kwargs["executor"], live_ex)

    def test_uses_offline_executor_when_not_live(self):
        from unittest import mock
        from analytics_platform.cli import cmd_junior
        ctx = _junior_ctx(metabase_live=False)
        with mock.patch("analytics_platform.cli.make_context", return_value=ctx), \
                mock.patch("analytics_platform.junior.JuniorEngine", _ShimJunior), \
                mock.patch("analytics_platform.execution.browser_session.make_live_executor") as mle:
            rc = cmd_junior(argparse.Namespace(tenant_id="t1", limit=50))
        self.assertEqual(rc, 0)
        mle.assert_not_called()
        self.assertIs(_ShimJunior.instances[0].kwargs["executor"], "offline-executor")


if __name__ == "__main__":
    unittest.main()