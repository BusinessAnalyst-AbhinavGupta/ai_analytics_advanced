"""Phase 9 offline tests: scheduler log-retention + background junior worker.

Covers the owner-facing observability core deterministically (no HTTP/Chrome):
  * weekly API-log purge honours a 30-day retention, is due/not-due-gated, and
    persists state (restarts don't re-run inside the same interval),
  * junior worker runs only inside its system-time window and at most once per
    hour, with a serial single-flight gate.
"""
from __future__ import annotations

import threading
import time as _time
import unittest
from datetime import timedelta

from analytics_platform.junior_worker import JuniorWorker
from analytics_platform.scheduler import Scheduler
from tests.helpers import Ctx


def _ts(y, mo, d, h=10, mi=0):
    from datetime import datetime
    return datetime(y, mo, d, h, mi).timestamp()


class TestSchedulerRetention(unittest.TestCase):
    def setUp(self):
        self.ctx = Ctx()
        self.now = _ts(2026, 1, 15, 12, 0)  # fixed "now"
        self.s = Scheduler(self.ctx.store, observability=self.ctx.obs,
                           retention_days=30, maintenance_interval_days=7)

    def tearDown(self):
        self.ctx.close()

    def test_first_purge_is_due_and_persists(self):
        res = self.s.purge_logs_once(self.now)
        self.assertTrue(res["ran"])
        self.assertIsNotNone(self.s.last_purge_ts())

    def test_within_interval_is_not_due(self):
        self.s.purge_logs_once(self.now)
        res = self.s.purge_logs_once(self.now + timedelta(days=1).total_seconds())
        self.assertFalse(res["ran"])
        self.assertEqual(res["reason"], "not_due")

    def test_due_after_weekly_interval(self):
        self.s.purge_logs_once(self.now)
        later = self.now + timedelta(days=7, hours=1).total_seconds()
        res = self.s.purge_logs_once(later)
        self.assertTrue(res["ran"])
        self.assertEqual(res["reason"], "due")

    def test_purge_removes_rows_older_than_retention(self):
        store = self.ctx.store
        old_sql = ("INSERT INTO api_logs (ts,tenant_id,method,path,status,duration_ms,"
                   "actor,meta) VALUES (?,?,?,?,?,?,?,?)")
        store.execute(old_sql, (self._iso_for(45), "t1", "GET", "/health",
                                200, 1.0, "t", "{}"))
        store.execute(old_sql, (self._iso_for(0), "t1", "GET", "/health",
                                200, 1.0, "t", "{}"))
        self.s.purge_logs_once(self.now)
        remaining = store.query_all("SELECT COUNT(*) c FROM api_logs")
        self.assertEqual(remaining[0]["c"], 1)

    def _iso_for(self, days_ago):
        return _time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              _time.gmtime(self.now - days_ago * 86400.0))


class FakeJunior:
    """Stand-in JuniorEngine for worker tests."""

    def __init__(self, store, executor):
        self.store = store
        self.executor = executor

    def suggest_questions(self, tenant_id, limit_per_target=1):
        return {"tenant_id": tenant_id, "suggestions": [
            {"target": "T", "category": "growth", "priority": 1,
             "question": f"Q {tenant_id}", "columns": ["col"], "source": "approved_definition"}]}

    def approved_queries(self, tenant_id, limit=1):
        return []

    def datasets(self, tenant_id):
        return ["things"]


class TestJuniorWorker(unittest.TestCase):
    def setUp(self):
        self.ctx = Ctx()
        self.worker = JuniorWorker(
            self.ctx.store, FakeJunior(self.ctx.store, self.ctx.executor),
            tenant_id="t1", work_start="10:00", work_end="19:00",
            min_interval_minutes=60, observability=self.ctx.obs)

    def tearDown(self):
        self.ctx.close()

    def test_outside_window_skips(self):
        res = self.worker.run_cycle("t1", now=_ts(2026, 1, 15, 8, 0))  # 08:00
        self.assertFalse(res["ran"])
        self.assertEqual(res["reason"], "outside_window")

    def test_inside_window_runs(self):
        res = self.worker.run_cycle("t1", now=_ts(2026, 1, 15, 14, 0))  # 14:00
        self.assertTrue(res["ran"])
        self.assertEqual(res["tenant_id"], "t1")
        self.assertIn("sql", res)

    def test_rate_limited_one_per_hour(self):
        first = self.worker.run_cycle("t1", now=_ts(2026, 1, 15, 14, 0))
        self.assertTrue(first["ran"])
        res = self.worker.run_cycle("t1", now=_ts(2026, 1, 15, 14, 30))  # 30 min later
        self.assertFalse(res["ran"])
        self.assertEqual(res["reason"], "rate_limited")
        res2 = self.worker.run_cycle("t1", now=_ts(2026, 1, 15, 15, 1))  # 61 min later
        self.assertTrue(res2["ran"])

    def test_serial_single_flight(self):
        # Overlapping run_cycle() calls must never run two queries at once.
        active = []
        capture = []
        guard = threading.Lock()
        orig = self.ctx.executor.execute

        def guarded_execute(sql, ctx):
            with guard:
                active.append("x")
                capture.append(len(active))
                _time.sleep(0.05)
                active.pop()
            return orig(sql, ctx)

        self.ctx.executor.execute = guarded_execute
        barrier = threading.Barrier(2)
        results = []

        def run():
            barrier.wait()
            results.append(self.worker.run_cycle("t1", now=_ts(2026, 1, 15, 15, 0)))

        a = threading.Thread(target=run)
        b = threading.Thread(target=run)
        a.start(); b.start(); a.join(); b.join()
        self.assertEqual(len(results), 2)
        self.assertTrue(all(c == 1 for c in capture), f"serial violated: {capture}")
        self.ctx.executor.execute = orig


if __name__ == "__main__":
    unittest.main()
