"""Config-panel API tests (R5/R6): analyst-config GET/PUT + history, junior
hypotheses + senior-status routes, and the offline provider-model listing.
Invokes FastAPI route handlers directly (no httpx/TestClient).
"""
from __future__ import annotations

import unittest

from analytics_platform.api import create_app, ensure_services
from analytics_platform.llm.client import list_provider_models
from tests.helpers import make_ctx
from analytics_platform.api import AppContext
from analytics_platform.onboarding import OnboardingService


def _full_app(warehouse=None):
    base = make_ctx()
    onboarding = OnboardingService(base.store, tenants=base.tenants,
                                   pipeline=base.pipeline, observability=base.obs)
    ctx = AppContext(settings=base.settings, store=base.store, tenants=base.tenants,
                     observability=base.obs, pipeline=base.pipeline,
                     executor=base.executor, onboarding=onboarding)
    ctx = ensure_services(ctx)
    return ctx, base


def _call(app, method, template, tenant, *body):
    for r in app.routes:
        if getattr(r, "path", "") == template and method in getattr(r, "methods", []):
            return r.endpoint(tenant, *body)
    raise AssertionError(f"route not found: {method} {template}")


class TestConfigPanelApi(unittest.TestCase):
    def setUp(self):
        self.ctx, self.base = _full_app()
        self.tid = self.ctx.tenants.create_tenant("CfgCo").id
        self.app = create_app(self.ctx)

    def tearDown(self):
        self.base.close()

    def test_get_put_config_round_trip_and_history(self):
        from analytics_platform.api import AnalystConfigIn, AnalystToggleIn
        _call(self.app, "GET", "/tenants/{tenant_id}/analyst-config", self.tid)
        _call(self.app, "PUT", "/tenants/{tenant_id}/analyst-config", self.tid,
              AnalystConfigIn(junior=AnalystToggleIn(enabled=False,
                                                     provider="openrouter",
                                                     model="openrouter/deepseek-v4"),
                              junior_depth=0, human_signoff_days=3,
                              changed_by="human"))
        cfg = _call(self.app, "GET", "/tenants/{tenant_id}/analyst-config", self.tid)
        self.assertFalse(cfg["junior"]["enabled"])
        self.assertEqual(cfg["junior_depth"], 0)
        self.assertEqual(cfg["human_signoff_days"], 3)
        hist = _call(self.app, "GET", "/tenants/{tenant_id}/analyst-config/history",
                     self.tid, 20)
        self.assertTrue(hist)

    def test_junior_hypotheses_and_senior_status_routes(self):
        j = _call(self.app, "GET", "/junior/{tenant_id}/hypotheses", self.tid, 4)
        self.assertIn("hypotheses", j)
        s = _call(self.app, "GET", "/tenants/{tenant_id}/senior/status", self.tid)
        self.assertIn("junior_depth", s)
        q = _call(self.app, "GET", "/senior/{tenant_id}/queue", self.tid, 5)
        self.assertIsInstance(q, list)

    def test_junior_depth_route(self):
        from analytics_platform.api import JuniorDepthIn
        res = _call(self.app, "POST", "/tenants/{tenant_id}/senior/junior-depth",
                    self.tid, JuniorDepthIn(action="up", by="human"))
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(res["depth"], 1)

    def test_llm_models_offline_empty(self):
        self.assertEqual(list_provider_models(provider=""), [])
        self.assertEqual(list_provider_models(provider="null"), [])


class TestJuniorWorkerGate(unittest.TestCase):
    """R6: toggling the junior analyst off must stop the background worker."""

    def setUp(self):
        self.ctx, self.base = _full_app()
        self.tid = self.ctx.tenants.create_tenant("GateCo").id
        self.ctx.tenants.set_company_profile(self.tid, {
            "name": "GateCo",
            "targets": [{"name": "Grow orders", "category": "growth", "priority": 1}]})
        from tests.helpers import make_ctx
        _ = make_ctx

    def tearDown(self):
        self.base.close()

    def test_junior_disabled_blocks_run_cycle(self):
        from analytics_platform.junior_worker import JuniorWorker
        worker = JuniorWorker(self.base.store, self.ctx.junior, tenant_id=self.tid)
        # enabled (default) -> runs a cycle inside the window
        self.ctx.tenants.set_analyst_config(self.tid, {"junior": {"enabled": True}})
        res = worker.run_cycle(tenant_id=self.tid, now=1900000000.0)
        # turn it off -> worker refuses regardless of window/rate
        self.ctx.tenants.set_analyst_config(self.tid, {"junior": {"enabled": False}})
        res = worker.run_cycle(tenant_id=self.tid, now=1900000000.0)
        self.assertFalse(res["ran"])
        self.assertEqual(res["reason"], "junior_disabled")

    def test_daily_cap_enforced_and_persists(self):
        from analytics_platform.junior_worker import JuniorWorker
        # Always-in-window window so only the hourly + daily caps gate the test.
        def mk_worker():
            return JuniorWorker(self.base.store, self.ctx.junior, tenant_id=self.tid,
                                daily_cap=2, min_interval_minutes=60,
                                work_start="00:00", work_end="23:59")
        worker = mk_worker()
        now = 1720000000.0          # 2024-07-03T12:26Z
        r1 = worker.run_cycle(tenant_id=self.tid, now=now)
        self.assertTrue(r1["ran"], r1)
        r2 = worker.run_cycle(tenant_id=self.tid, now=now + 3600)
        self.assertTrue(r2["ran"], r2)
        # 2 already this UTC day -> 3rd is blocked by the persisted daily cap
        r3 = worker.run_cycle(tenant_id=self.tid, now=now + 3600 * 2)
        self.assertFalse(r3["ran"])
        self.assertEqual(r3["reason"], "daily_cap")
        # a *brand-new* worker instance still sees the cap (persisted in DB)
        r4 = mk_worker().run_cycle(tenant_id=self.tid, now=now + 3600 * 3)
        self.assertFalse(r4["ran"])
        self.assertEqual(r4.get("reason"), "daily_cap")
        # next UTC day the cap resets (new daily key)
        r5 = mk_worker().run_cycle(tenant_id=self.tid, now=now + 86400)
        self.assertTrue(r5["ran"], r5)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()