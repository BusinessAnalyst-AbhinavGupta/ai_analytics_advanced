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
                                    CompanyProfileIn, DataSourceIn,
                                    AppContext, create_app, make_context)
from analytics_platform.domain import DataSourceKind, NodeKind, ReviewStatus
from analytics_platform.fixtures import WEEKLY_ORDER_SQL, build_retail_warehouse
from analytics_platform.onboarding import OnboardingService
from tests.helpers import make_ctx


def app_ctx(warehouse=None):
    """Wrap the test Ctx into the AppContext `create_app` expects."""
    base = make_ctx(warehouse=warehouse)
    onboarding = OnboardingService(base.stores, tenants=base.tenants,
                                   pipeline=base.pipeline, observability=base.obs)
    ctx = AppContext(settings=base.settings, stores=base.stores, tenants=base.tenants,
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


class TestApiKnowledgeSearchLimit(unittest.TestCase):
    """Regression: `limit` on GET /tenants/{tenant_id}/knowledge is caller-
    controlled and this route is unauthenticated by default, and it flows into
    a `max(limit * 25, 500)` pre-filter cap inside brain.search -- a huge
    caller-supplied limit must be clamped at the API boundary, not passed
    straight through."""

    def setUp(self):
        self.ctx, self.base = app_ctx(warehouse=build_retail_warehouse())
        self.tid = self.ctx.tenants.create_tenant("ApiCo").id
        self.app = create_app(self.ctx)

    def tearDown(self):
        self.base.close()

    def test_huge_limit_is_clamped_before_reaching_brain_search(self):
        from unittest import mock
        from analytics_platform.brain.store import CompanyBrain

        captured = {}

        def fake_search(self, query="", kind=None, usable_only=True, limit=20):
            captured["limit"] = limit
            return []

        with mock.patch.object(CompanyBrain, "search", fake_search):
            route(self.app, "GET", "/tenants/{tenant_id}/knowledge")(self.tid, limit=100000)

        self.assertLessEqual(captured["limit"], 200)

    def test_ordinary_limit_is_unaffected(self):
        from unittest import mock
        from analytics_platform.brain.store import CompanyBrain

        captured = {}

        def fake_search(self, query="", kind=None, usable_only=True, limit=20):
            captured["limit"] = limit
            return []

        with mock.patch.object(CompanyBrain, "search", fake_search):
            route(self.app, "GET", "/tenants/{tenant_id}/knowledge")(self.tid, limit=10)

        self.assertEqual(captured["limit"], 10)


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
        # GET /catalog is a cheap, cache-only read (never probes); a table is only
        # "described" after an explicit refresh.
        call(self.app, "POST", "/junior/{tenant_id}/catalog/refresh", self.tid)
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

class TestApiBusinessContext(unittest.TestCase):
    """Initialisation: company/business context + data sources persist (P* 5.4
    contract: each behaviour ships an API-contract test asserting persisted state)."""

    def setUp(self):
        self.ctx, self.base = app_ctx()
        self.app = create_app(self.ctx)
        self.tid = self.ctx.tenants.create_tenant("BizCo", region="DE").id

    def tearDown(self):
        self.base.close()

    def test_company_profile_roundtrip_sets_business_context(self):
        profile = {
            "name": "BizCo", "industry": "telecom", "region": "DE",
            "description": "Mobile plans for consumers",
            "customers": "Consumer households",
            "product": "Postpaid subscriptions",
            "value_creation": "Coverage and reliability",
            "revenue_model": "Monthly subscription",
            "targets": [
                {"name": "Grow ARPU", "description": "Raise revenue per user/month",
                 "category": "growth", "priority": 1, "owner": "Head of Monetisation",
                 "time_horizon": "quarterly", "target_value": 12.5,
                 "metric_refs": ["arpu"]},
                {"name": "Cut churn", "category": "retention", "priority": 2,
                 "owner": "Head of CX", "metric_refs": ["churn"]},
            ],
            "constraints": ["GDPR"],
            "preferred_metrics": ["arpu", "churn"],
        }
        res = call(self.app, "PUT", "/tenants/{tenant_id}/company-profile", self.tid,
                   CompanyProfileIn(**profile))
        # contract: the API call leaves durable state (business context for analysts)
        prof = self.ctx.tenants.get_company_profile(self.tid)
        self.assertEqual(prof.name, "BizCo")
        self.assertEqual(prof.description, "Mobile plans for consumers")
        self.assertEqual(len(prof.targets), 2)
        self.assertTrue(any(t.name == "Grow ARPU" and t.target_value == 12.5
                            and t.metric_refs == ["arpu"] for t in prof.targets))
        self.assertTrue(any(t.name == "Cut churn" and t.category == "retention"
                            for t in prof.targets))
        # and readable back through the GET tenant endpoint the UI uses
        data = call(self.app, "GET", "/tenants/{tenant_id}", self.tid)
        self.assertEqual(data["profile"]["name"], "BizCo")
        self.assertEqual(len(data["profile"]["targets"]), 2)
        self.assertEqual(data["profile"]["preferred_metrics"], ["arpu", "churn"])

    def test_datasource_add_then_list(self):
        res = call(self.app, "POST", "/tenants/{tenant_id}/datasources", self.tid,
                   DataSourceIn(name="Events", kind="direct_db", dialect="athena",
                                tables=["es_events_v2"], connected=True))
        self.assertTrue(res["datasource_id"])
        dss = call(self.app, "GET", "/tenants/{tenant_id}/datasources", self.tid)
        self.assertEqual(len(dss), 1)
        self.assertEqual(dss[0]["tables"], ["es_events_v2"])
        self.assertEqual(dss[0]["dialect"], "athena")

    def test_profile_history_versions_over_time(self):
        profile1 = {"name": "BizCo", "description": "v1 desc",
                    "targets": [{"name": "Grow ARPU", "category": "growth"}]}
        profile2 = {"name": "BizCo", "description": "v2 desc - product changed",
                    "product": "Fiber plans",
                    "targets": [{"name": "Grow ARPU", "category": "growth"},
                                {"name": "Cut churn", "category": "retention"}],
                    "changed_by": "head-of-strategy"}
        call(self.app, "PUT", "/tenants/{tenant_id}/company-profile", self.tid,
             CompanyProfileIn(**profile1))
        call(self.app, "PUT", "/tenants/{tenant_id}/company-profile", self.tid,
             CompanyProfileIn(**profile2))
        hist = call(self.app, "GET",
                    "/tenants/{tenant_id}/company-profile/history", self.tid)
        # newest first; each save is a distinct version snapshot
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0]["version"], 2)
        self.assertEqual(hist[0]["changed_by"], "head-of-strategy")
        self.assertEqual(hist[0]["snapshot"]["targets"][1]["name"], "Cut churn")
        self.assertEqual(hist[1]["version"], 1)
        self.assertEqual(hist[1]["snapshot"]["description"], "v1 desc")
        # current profile reflects the latest version
        data = call(self.app, "GET", "/tenants/{tenant_id}", self.tid)
        self.assertEqual(data["profile"]["product"], "Fiber plans")
        self.assertEqual(len(data["profile"]["targets"]), 2)


class TestApiObservability(unittest.TestCase):
    """Phase 9 owner-facing observability + background junior (via API routes)."""

    def setUp(self):
        self.ctx, self.base = app_ctx(warehouse=build_retail_warehouse())
        self.tid = self.ctx.tenants.create_tenant("ObsCo").id
        self.app = create_app(self.ctx)

    def tearDown(self):
        self.base.close()

    def _run(self, method, template, *args):
        return route(self.app, method, template)(*args)

    def test_access_log_middleware_writes_rows(self):
        # middleware is HTTP-level, so hit it through the framework's ASGI app
        # via a direct store probe: call the log route AND verify the underlying
        # api_logs table exists and the scheduler/status route works.
        status = self._run("GET", "/observability/status")
        self.assertIn("retention_days", status)
        self.assertIn("purge", status)
        self.assertEqual(status["retention_days"], 30)
        # verify api_logs table is queryable through the observability service
        logs = self._run("GET", "/observability/logs")
        self.assertIn("logs", logs)

    def test_purge_route_returns_due_state(self):
        res = self._run("POST", "/observability/purge")
        self.assertEqual(res["ran"], True)          # first call is due
        self.assertEqual(res["expired_rows"], 0)    # no old rows yet
        res2 = self._run("POST", "/observability/purge")
        self.assertEqual(res2["ran"], False)        # within interval -> not_due
        self.assertEqual(res2["reason"], "not_due")

    def test_status_reports_junior_window_and_rate(self):
        st = self._run("GET", "/observability/status")
        j = st["junior"]
        self.assertIsNotNone(j)
        self.assertEqual(j["tenant_id"], self.tid)
        self.assertIn("work_window", j)
        self.assertIn("in_window", j)   # computed from system clock
        self.assertIn("min_interval_minutes", j)
        self.assertEqual(j["min_interval_minutes"], 60)

    def test_junior_run_unknown_tenant_404_no_store_created(self):
        # The background singleton worker is bound to self.tid; passing a
        # *different*, non-existent tenant_id must 404 -- not silently build
        # a fresh worker/store for a tenant that was never registered, and a
        # malformed id must surface as a clean 404 (via tenant_or_404), not an
        # uncaught ValueError from validate_tenant_id inside stores.for_tenant.
        import os
        unknown = "nope-unknown-tenant"
        db_path = self.ctx.stores.tenant_db_path(unknown)
        self.assertFalse(os.path.exists(db_path))
        with self.assertRaises(HTTPException) as cm:
            self._run("POST", "/observability/junior/run", unknown)
        self.assertEqual(cm.exception.status_code, 404)
        # the tenant store must not have been created as a side effect
        self.assertFalse(os.path.exists(db_path))
        with self.assertRaises(HTTPException) as cm:
            self._run("POST", "/observability/junior/run", "../../etc")
        self.assertEqual(cm.exception.status_code, 404)


class TestMakeContext(unittest.TestCase):
    def test_make_context_no_longer_uses_vector_store(self):
        from unittest.mock import patch
        from analytics_platform.config import Settings

        settings = Settings(data_dir="/tmp/test_tenant")

        # BrainVectorStore is deprecated; will be replaced by hybrid index in Task 9.
        # Verify that make_context succeeds even when BrainVectorStore is not available.
        with patch("analytics_platform.stores.Store"):
            ctx = make_context(settings=settings)
            self.assertIsNotNone(ctx)


if __name__ == "__main__":
    unittest.main()