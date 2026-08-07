"""API endpoint tests for triage + junior (dependency-free, offline).

No httpx/TestClient is used (not in the venv); we build a real `create_app(ctx)`
over a temp Store and invoke the registered route handlers directly — the exact
closures the framework would call — so wiring (paths/methods), tenant gating
(404), and the endpoint->service behaviour are all exercised.
"""
from __future__ import annotations

import unittest

from fastapi import HTTPException

from analytics_platform.api import (TriageBulkIn, TriageDedupeIn, TriageIdsIn,
                                    AppContext, create_app)
from analytics_platform.domain import DataSourceKind, NodeKind, ReviewStatus
from analytics_platform.fixtures import WEEKLY_ORDER_SQL, build_retail_warehouse
from analytics_platform.onboarding import OnboardingService
from tests.helpers import make_ctx


def app_ctx(warehouse=None):
    """Wrap the test Ctx into the AppContext `create_app` expects."""
    base = make_ctx(warehouse=warehouse)
    onboarding = OnboardingService(base.store, tenants=base.tenants,
                                   pipeline=base.pipeline, observability=base.obs)
    ctx = AppContext(settings=base.settings, store=base.store, tenants=base.tenants,
                     observability=base.obs, pipeline=base.pipeline, executor=base.executor,
                     onboarding=onboarding)
    return ctx, base


def route(app, method, template):
    """Return the FastAPI route handler for a path *template* (e.g. /triage/{tenant_id}/summary),
    asserting it exists on the given method."""
    for r in app.routes:
        if getattr(r, "path", "") == template and method in getattr(r, "methods", []):
            return r.endpoint
    raise AssertionError(f"route not found: {method} {template}")


def call(app, method, template, tenant, *body):
    """Invoke a route handler the way the framework would."""
    return route(app, method, template)(tenant, *body)


class TestApiTriage(unittest.TestCase):
    def setUp(self):
        self.ctx, self.base = app_ctx(warehouse=build_retail_warehouse())
        self.tid = self.ctx.tenants.create_tenant("ApiCo").id
        self.app = create_app(self.ctx)

    def tearDown(self):
        self.base.close()

    def _seed(self, n=2):
        brain = self.ctx.pipeline.brain(self.tid)
        ids = []
        for i in range(n):
            x = brain.create(NodeKind.QUERY, f"q{i}",
                             payload={"sql": WEEKLY_ORDER_SQL, "dialect": "athena"})
            ids.append(x.id)
        return ids

    def test_summary(self):
        self._seed()
        s = call(self.app, "GET", "/triage/{tenant_id}/summary", self.tid)
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["actionable"], 2)

    def test_queue(self):
        self._seed(2)
        q = call(self.app, "GET", "/triage/{tenant_id}/queue", self.tid, "QUERY")
        self.assertEqual(len(q), 2)

    def test_bulk_approve(self):
        ids = self._seed(2)
        res = call(self.app, "POST", "/triage/{tenant_id}/bulk", self.tid,
                   TriageBulkIn(kind="QUERY", action="approve"))
        self.assertEqual(len(res["approved"]), 2)
        self.assertEqual(self.ctx.pipeline.brain(self.tid).get(ids[0]).status,
                         ReviewStatus.APPROVED)

    def test_approve_individual_and_conflicts(self):
        ids = self._seed(2)
        res = call(self.app, "POST", "/triage/{tenant_id}/approve", self.tid,
                   TriageIdsIn(ids=[ids[0]]))
        self.assertEqual(res["approved"], [ids[0]])
        self.assertIsInstance(call(self.app, "GET", "/triage/{tenant_id}/conflicts",
                                   self.tid), list)

    def test_dedupe_via_api(self):
        """API-contract test: the route mutates persisted state (reject/supersede)."""
        brain = self.ctx.pipeline.brain(self.tid)
        keep = brain.create(NodeKind.BUSINESS_RULE, "dup rule",
                            status=ReviewStatus.APPROVED)
        drop_appr = brain.create(NodeKind.BUSINESS_RULE, "dup rule",
                                 status=ReviewStatus.APPROVED)
        drop_cand = brain.create(NodeKind.BUSINESS_RULE, "dup rule")  # CANDIDATE
        self.assertEqual(len(call(self.app, "GET", "/triage/{tenant_id}/conflicts",
                                  self.tid)), 1)
        res = call(self.app, "POST", "/triage/{tenant_id}/dedupe", self.tid,
                   TriageDedupeIn(keep=keep.id, drop=[drop_appr.id, drop_cand.id],
                                  by="senior"))
        self.assertEqual(set(res["superseded"]), {drop_appr.id})
        self.assertEqual(res["rejected"], [drop_cand.id])
        self.assertEqual(call(self.app, "GET", "/triage/{tenant_id}/conflicts",
                              self.tid), [])
        # kept node is untouched and still APPROVED
        self.assertEqual(brain.get(keep.id).status, ReviewStatus.APPROVED)

    def test_unknown_tenant_404(self):
        for method, template in [("GET", "/triage/{tenant_id}/summary"),
                                 ("GET", "/triage/{tenant_id}/queue"),
                                 ("POST", "/triage/{tenant_id}/approve"),
                                 ("POST", "/triage/{tenant_id}/bulk"),
                                 ("POST", "/triage/{tenant_id}/dedupe")]:
            with self.assertRaises(HTTPException) as cm:
                if template.endswith("/approve"):
                    call(self.app, method, template, "nope", TriageIdsIn(ids=[]))
                elif template.endswith("/bulk"):
                    call(self.app, method, template, "nope", TriageBulkIn())
                elif template.endswith("/dedupe"):
                    call(self.app, method, template, "nope",
                         TriageDedupeIn(keep="k", drop=[]))
                else:
                    call(self.app, method, template, "nope")
            self.assertEqual(cm.exception.status_code, 404, template)


class TestApiJunior(unittest.TestCase):
    def setUp(self):
        self.ctx, self.base = app_ctx(warehouse=build_retail_warehouse())
        self.tid = self.ctx.tenants.create_tenant("JuniorApiCo").id
        self.app = create_app(self.ctx)
        # a mapped table the catalog can describe via the offline warehouse,
        # and an approved query the engine can reproduce
        self.ctx.tenants.add_datasource(self.tid, "Events", DataSourceKind.DIRECT_DB,
                                        dialect="athena", tables=["events"])
        b = self.ctx.pipeline.brain(self.tid)
        n = b.create(NodeKind.QUERY, "orders by month",
                     payload={"sql": WEEKLY_ORDER_SQL, "dialect": "athena"})
        b.submit(n.id, by="senior")
        b.approve(n.id, by="senior")

    def tearDown(self):
        self.base.close()

    def test_stage_catalog_questions(self):
        st = call(self.app, "GET", "/junior/{tenant_id}/stage", self.tid)
        self.assertGreaterEqual(st["reproduction"]["reproduced"], 1)
        cat = call(self.app, "GET", "/junior/{tenant_id}/catalog", self.tid)
        self.assertEqual(cat["tables_known"], 1)
        self.assertEqual(cat["tables_described"], 1)
        q = call(self.app, "GET", "/junior/{tenant_id}/questions", self.tid)
        self.assertGreaterEqual(q["count"], 1)

    def test_reproduce(self):
        r = call(self.app, "GET", "/junior/{tenant_id}/reproduce", self.tid)
        self.assertEqual(r["attempted"], 1)
        self.assertEqual(r["reproduced"], 1)

    def test_unknown_tenant_404(self):
        for template in ["/junior/{tenant_id}/stage", "/junior/{tenant_id}/catalog",
                         "/junior/{tenant_id}/questions", "/junior/{tenant_id}/reproduce"]:
            with self.assertRaises(HTTPException) as cm:
                call(self.app, "GET", template, "nope")
            self.assertEqual(cm.exception.status_code, 404, template)


if __name__ == "__main__":
    unittest.main()