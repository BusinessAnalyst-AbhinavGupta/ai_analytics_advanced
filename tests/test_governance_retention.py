"""P8 — Commercial hardening: retention purge + full tenant deletion (GDPR)."""
from __future__ import annotations

import unittest

from analytics_platform.domain import now_iso
from tests.helpers import make_ctx
from tests.test_governance_auth import enabled_ctx


class TestRetention(unittest.TestCase):
    def setUp(self):
        self.ctx, self.base = enabled_ctx()
        self.tid = self.ctx.tenants.create_tenant("RetCo", retention_days=90).id
        self.ctx.store.execute(
            "INSERT INTO telemetry (ts,tenant_id,stage,actor,status) VALUES (?,?,?,?,?)",
            ("2020-01-01T00:00:00Z", self.tid, "old", "x", "OK"))
        self.ctx.store.execute(
            "INSERT INTO telemetry (ts,tenant_id,stage,actor,status) VALUES (?,?,?,?,?)",
            (now_iso(), self.tid, "fresh", "x", "OK"))

    def tearDown(self):
        self.base.close()

    def test_purge_dry_run_then_real(self):
        review = self.ctx.retention.review()
        expiring = {t["tenant_id"]: t for t in review["tenants"]}[self.tid]["expiring"]
        self.assertGreaterEqual(expiring["telemetry"], 1)

        dry = self.ctx.retention.purge_expired(dry_run=True)
        self.assertTrue(dry["dry_run"])
        still = self.ctx.store.query_one(
            "SELECT COUNT(*) c FROM telemetry WHERE tenant_id=?", (self.tid,))["c"]
        self.assertEqual(still, 2)  # dry run touched nothing

        res = self.ctx.retention.purge_expired(dry_run=False)
        self.assertFalse(res["dry_run"])
        old = self.ctx.store.query_one(
            "SELECT COUNT(*) c FROM telemetry WHERE tenant_id=? AND ts=?",
            (self.tid, "2020-01-01T00:00:00Z"))["c"]
        self.assertEqual(old, 0)  # the expired row is gone

    def test_scoped_purge_only_that_tenant(self):
        other = self.ctx.tenants.create_tenant("Other", retention_days=3650).id
        self.ctx.store.execute(
            "INSERT INTO telemetry (ts,tenant_id,stage,actor,status) VALUES (?,?,?,?,?)",
            ("2020-01-01T00:00:00Z", other, "old", "x", "OK"))
        self.ctx.retention.purge_expired(tenant_id=self.tid, dry_run=False)
        still_other = self.ctx.store.query_one(
            "SELECT COUNT(*) c FROM telemetry WHERE tenant_id=?", (other,))["c"]
        self.assertEqual(still_other, 1)

    def test_delete_tenant_wipes_all_and_audits(self):
        self.ctx.pipeline.register_approved_query(self.tid, "SELECT 1", "seed q")
        d = self.ctx.retention.delete_tenant(self.tid)
        self.assertIn("knowledge_nodes", d["deleted_tables"])
        self.assertIsNone(self.ctx.tenants.get_tenant(self.tid))
        audit = self.ctx.store.query_one(
            "SELECT * FROM audit_log WHERE action='tenant.delete' AND tenant_id=?",
            (self.tid,))
        self.assertIsNotNone(audit)


if __name__ == "__main__":
    unittest.main()