"""Tenancy + company profile + datasource tests."""
from __future__ import annotations

import unittest

from analytics_platform.domain import DataSourceKind
from tests.helpers import make_ctx


class TestTenancy(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()

    def tearDown(self):
        self.ctx.close()

    def test_create_and_get_tenant(self):
        t = self.ctx.tenants.create_tenant("Acme", region="DE")
        got = self.ctx.tenants.get_tenant(t.id)
        self.assertEqual(got.name, "Acme")
        self.assertEqual(got.id, t.id)

    def test_unknown_tenant_raises(self):
        with self.assertRaises(KeyError):
            self.ctx.tenants.require_tenant("tnt_missing")

    def test_company_profile_roundtrip_with_targets(self):
        t = self.ctx.tenants.create_tenant("Acme", region="DE")
        self.ctx.tenants.set_company_profile(t.id, {
            "name": "Acme", "industry": "ecommerce",
            "targets": [{"name": "Grow orders", "category": "growth", "priority": 1,
                          "metric_refs": ["orders"]}],
            "preferred_metrics": ["orders"],
        })
        p = self.ctx.tenants.get_company_profile(t.id)
        self.assertEqual(p.industry, "ecommerce")
        self.assertEqual(len(p.targets), 1)
        self.assertEqual(p.targets[0].category, "growth")
        self.assertIn("orders", p.preferred_metrics)

    def test_datasource_scoped_by_tenant(self):
        t1 = self.ctx.tenants.create_tenant("A")
        t2 = self.ctx.tenants.create_tenant("B")
        self.ctx.tenants.add_datasource(t1.id, "warehouse", DataSourceKind.DIRECT_DB,
                                        tables=["events"])
        self.ctx.tenants.add_datasource(t2.id, "other", DataSourceKind.METABASE_BROWSER)
        l1 = self.ctx.tenants.list_datasources(t1.id)
        l2 = self.ctx.tenants.list_datasources(t2.id)
        self.assertEqual([d["name"] for d in l1], ["warehouse"])
        self.assertEqual([d["name"] for d in l2], ["other"])


if __name__ == "__main__":
    unittest.main()