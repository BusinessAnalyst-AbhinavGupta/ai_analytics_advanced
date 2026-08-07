"""P8 — Commercial hardening: RBAC + cross-tenant isolation + billing/metering."""
from __future__ import annotations

import unittest

from analytics_platform.api import AppContext, OnboardingService, create_app, ensure_services
from analytics_platform.auth import AuthError, AuthGate, Role, issue
from tests.helpers import make_ctx
from tests.test_api import route


def enabled_ctx(secret="test-secret"):
    base = make_ctx()
    base.settings.auth_enabled = True
    base.settings.auth_secret = secret
    onboarding = OnboardingService(base.store, tenants=base.tenants,
                                   pipeline=base.pipeline, observability=base.obs)
    ctx = AppContext(settings=base.settings, store=base.store, tenants=base.tenants,
                     observability=base.obs, pipeline=base.pipeline, executor=base.executor,
                     onboarding=onboarding)
    return ensure_services(ctx), base


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.ctx, self.base = enabled_ctx()
        self.app = create_app(self.ctx)      # ensure_services wires auth/billing/retention
        self.a = self.ctx.tenants.create_tenant("AuthA").id
        self.b = self.ctx.tenants.create_tenant("AuthB").id

    def tearDown(self):
        self.base.close()

    def test_default_is_permissive(self):
        default_ctx = make_ctx()
        gate = AuthGate(default_ctx.settings)  # auth_enabled False
        p = gate.require(None, "anything", [Role.STAKEHOLDER])
        self.assertEqual(p["role"], Role.SERVICE.value)
        default_ctx.store.close()

    def test_missing_token_denied_when_enabled(self):
        with self.assertRaises(AuthError) as cm:
            self.ctx.auth.require(None, self.a, [Role.OWNER])
        self.assertEqual(cm.exception.status, 401)

    def test_role_insufficient(self):
        tok = issue(self.ctx.settings.auth_secret, self.a, Role.STAKEHOLDER.value)
        with self.assertRaises(AuthError) as cm:
            self.ctx.auth.require(tok, self.a, [Role.OWNER])   # billing is owner-only
        self.assertEqual(cm.exception.status, 403)

    def test_cross_tenant_denied(self):
        tok = issue(self.ctx.settings.auth_secret, self.a, Role.STAKEHOLDER.value)
        with self.assertRaises(AuthError) as cm:
            self.ctx.auth.require(tok, self.b, [Role.STAKEHOLDER])
        self.assertEqual(cm.exception.status, 403)

    def test_owner_cross_tenant_allowed(self):
        tok = issue(self.ctx.settings.auth_secret, self.a, Role.OWNER.value, scopes=["all"])
        p = self.ctx.auth.require(tok, self.b, [Role.STAKEHOLDER])
        self.assertEqual(p["role"], Role.OWNER.value)

    def test_route_level_enforcement(self):
        owner = issue(self.ctx.settings.auth_secret, self.a, Role.OWNER.value, scopes=["all"])
        usage = route(self.app, "GET", "/billing/{tenant_id}/usage")(self.a, authorization=owner)
        self.assertIn("spans", usage)
        stale = issue(self.ctx.settings.auth_secret, self.b, Role.STAKEHOLDER.value)
        handler = route(self.app, "GET", "/billing/{tenant_id}/usage")
        with self.assertRaises(Exception):
            handler(self.a, authorization=stale)


class TestBilling(unittest.TestCase):
    def setUp(self):
        self.ctx, self.base = enabled_ctx()
        self.tid = self.ctx.tenants.create_tenant("BillCo").id
        self.ctx.observability.event(tenant_id=self.tid, stage="llm.generate", actor="junior",
                                     tokens_in=1000, tokens_out=2000)

    def tearDown(self):
        self.base.close()

    def test_per_tenant_usage_and_cost(self):
        u = self.ctx.billing.usage(self.tid)
        self.assertEqual(u["tokens_in"], 1000)
        self.assertEqual(u["tokens_out"], 2000)
        # (1000/1000)*0.30 + (2000/1000)*1.20
        self.assertAlmostEqual(u["cost_usd"]["total"], 0.3 + 2.4, places=6)
        self.assertGreaterEqual(u["by_stage"][0]["count"], 1)

    def test_platform_report(self):
        r = self.ctx.billing.platform_report()
        self.assertGreaterEqual(len(r["tenants"]), 1)


if __name__ == "__main__":
    unittest.main()