"""Live Metabase E2E — GATED by ANALYTICS_MB_LIVE=1.

Normal test runs skip this module entirely (no Chrome / no OS injection). To run
for real on your machine:
  1) Log into Metabase in Chrome (make a Metabase tab the ACTIVE tab).
  2) export ANALYTICS_MB_LIVE=1 ANALYTICS_MB_DATABASE_ID=<id> \\
         ANALYTICS_MB_EXPECTED_HOST=<host>
  3) .venv/bin/python -m unittest discover -s tests -k MetabaseLive -v

Invariants exercised: session must be `valid` (fail-with-pause on login), the
query is read-only, and the cookie never leaves the browser (same-origin fetch).
"""
from __future__ import annotations

import os
import unittest

ANALYTICS_MB_LIVE = os.environ.get("ANALYTICS_MB_LIVE") == "1"
_MAYBE_SQL = os.environ.get("ANALYTICS_MB_TEST_SQL", "SELECT 1 AS n")


@unittest.skipUnless(ANALYTICS_MB_LIVE,
                     "Live Metabase test: export ANALYTICS_MB_LIVE=1 + have "
                     "Metabase logged in on the active Chrome tab")
class TestMetabaseLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from analytics_platform.execution.browser_session import make_live_executor
        cls.executor = make_live_executor()
        if cls.executor.config.database_id is None:
            raise unittest.SkipTest("ANALYTICS_MB_DATABASE_ID not set")

    def test_session_is_valid(self):
        s = self.executor.session_status(tenant_id="live")
        self.assertEqual(s.state, "valid", s.detail)
        self.executor.ensure_session("live")  # must not raise

    def test_executes_read_only_query_via_browser(self):
        from analytics_platform.execution.base import ExecutionContext
        r = self.executor.execute(_MAYBE_SQL,
                                  ExecutionContext(tenant_id="live", dialect="athena"))
        self.assertTrue(r.ok, r.error)
        self.assertGreaterEqual(r.row_count, 1)
        self.assertTrue(r.columns)
        self.assertTrue(r.data is not None)


if __name__ == "__main__":
    unittest.main()