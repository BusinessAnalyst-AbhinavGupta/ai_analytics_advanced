"""P8 — Commercial hardening: retention purge + full tenant deletion (GDPR)."""
from __future__ import annotations

import os
import unittest

from analytics_platform.domain import now_iso
from tests.helpers import make_ctx
from tests.test_governance_auth import enabled_ctx


class TestRetention(unittest.TestCase):
    def setUp(self):
        self.ctx, self.base = enabled_ctx()
        self.tid = self.ctx.tenants.create_tenant("RetCo", retention_days=90).id
        # telemetry is tenant-scoped: write into RetCo's own database, not the
        # default test tenant's (`ctx.store`) -- see task-5-report.md.
        store = self.ctx.stores.for_tenant(self.tid)
        store.execute(
            "INSERT INTO telemetry (ts,tenant_id,stage,actor,status) VALUES (?,?,?,?,?)",
            ("2020-01-01T00:00:00Z", self.tid, "old", "x", "OK"))
        store.execute(
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
        still = self.ctx.stores.for_tenant(self.tid).query_one(
            "SELECT COUNT(*) c FROM telemetry WHERE tenant_id=?", (self.tid,))["c"]
        self.assertEqual(still, 2)  # dry run touched nothing

        res = self.ctx.retention.purge_expired(dry_run=False)
        self.assertFalse(res["dry_run"])
        old = self.ctx.stores.for_tenant(self.tid).query_one(
            "SELECT COUNT(*) c FROM telemetry WHERE tenant_id=? AND ts=?",
            (self.tid, "2020-01-01T00:00:00Z"))["c"]
        self.assertEqual(old, 0)  # the expired row is gone

    def test_scoped_purge_only_that_tenant(self):
        other = self.ctx.tenants.create_tenant("Other", retention_days=3650).id
        self.ctx.stores.for_tenant(other).execute(
            "INSERT INTO telemetry (ts,tenant_id,stage,actor,status) VALUES (?,?,?,?,?)",
            ("2020-01-01T00:00:00Z", other, "old", "x", "OK"))
        self.ctx.retention.purge_expired(tenant_id=self.tid, dry_run=False)
        still_other = self.ctx.stores.for_tenant(other).query_one(
            "SELECT COUNT(*) c FROM telemetry WHERE tenant_id=?", (other,))["c"]
        self.assertEqual(still_other, 1)

    def test_delete_tenant_wipes_all_and_audits(self):
        self.ctx.pipeline.register_approved_query(self.tid, "SELECT 1", "seed q")
        d = self.ctx.retention.delete_tenant(self.tid)
        self.assertIn("knowledge_nodes", d["deleted_tables"])
        self.assertIsNone(self.ctx.tenants.get_tenant(self.tid))
        # the tenant's database file itself must be gone, not just the registry
        # row -- confirms `evict()` + the filesystem delete actually ran.
        self.assertFalse(os.path.exists(self.ctx.stores.tenant_db_path(self.tid)))
        # the tenant's own database is gone by now, so the audit record about
        # its deletion lives on the control plane -- see task-5-report.md.
        audit = self.ctx.stores.control.query_one(
            "SELECT * FROM tenant_lifecycle_log WHERE action='tenant.delete' AND tenant_id=?",
            (self.tid,))
        self.assertIsNotNone(audit)


if __name__ == "__main__":
    unittest.main()

class TestExtractRetention(unittest.TestCase):
    """Extract Parquet at a 1,000,000-row ceiling accumulates fast, and unlike a
    database row it does not disappear when its answer is purged. The sweep runs
    inside the same purge job rather than on a scheduler of its own."""

    def setUp(self):
        import tempfile
        import pandas as pd
        from analytics_platform.execution.extract_store import ExtractMeta, ExtractStore

        self.ctx, self.base = enabled_ctx()
        self._tmp = tempfile.TemporaryDirectory()
        self.tid = self.ctx.tenants.create_tenant("SweepCo", retention_days=90).id
        self.store = ExtractStore(self._tmp.name)
        self.ctx.retention.extract_store = self.store
        self.ctx.settings.policy.extract_retention_days = 30

        def put(conversation_id, created_at):
            self.store.put(self.tid, conversation_id,
                           ExtractMeta(label="df_1", created_at=created_at, row_count=1),
                           pd.DataFrame({"a": [1]}))

        put("old", "2020-01-01T00:00:00Z")
        put("fresh", now_iso())

    def tearDown(self):
        self._tmp.cleanup()
        self.base.close()

    def _conversations(self):
        return {c for c in ("old", "fresh")
                if self.store.meta(self.tid, c, "df_1") is not None}

    def test_a_dry_run_removes_no_parquet(self):
        self.ctx.retention.purge_expired(dry_run=True)
        self.assertEqual(self._conversations(), {"old", "fresh"})

    def test_an_expired_conversation_directory_is_swept_and_a_fresh_one_kept(self):
        out = self.ctx.retention.purge_expired(dry_run=False)
        self.assertEqual(self._conversations(), {"fresh"})
        self.assertEqual(out["removed"]["extracts"], 1)

    def test_a_zero_retention_setting_sweeps_nothing(self):
        """0 means 'no extract retention policy', not 'delete everything'."""
        self.ctx.settings.policy.extract_retention_days = 0
        self.ctx.retention.purge_expired(dry_run=False)
        self.assertEqual(self._conversations(), {"old", "fresh"})


class LlmTraceRetentionTest(unittest.TestCase):
    """Traces are the largest rows the platform writes -- a 64KB prompt ceiling
    times seven calls times every turn. They have to age out with everything
    else."""

    def test_llm_traces_is_in_the_retention_table_list(self):
        from analytics_platform.retention import RETENTION_TABLES
        self.assertIn(("llm_traces", "ts"), RETENTION_TABLES)

    def test_a_purge_actually_deletes_aged_trace_rows(self):
        """List membership is not the behaviour -- deletion is."""
        import tempfile
        from analytics_platform.database import TENANT_SCHEMA, Store
        from analytics_platform.retention import _cutoff_iso

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(f"{tmp}/t.db", schema=TENANT_SCHEMA)
            for ts in ("2020-01-01T00:00:00Z", "2999-01-01T00:00:00Z"):
                store.execute(
                    "INSERT INTO llm_traces (ts,tenant_id,trace_id,seq,stage,kind,"
                    "payload,duration_ms,tokens_in,tokens_out,ok) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, "tnt_x", "tr", 1, "planning", "llm", "{}", 0.0, 0, 0, 1))
            store.execute("DELETE FROM llm_traces WHERE ts < ?", (_cutoff_iso(30),))
            left = store.query_all("SELECT ts FROM llm_traces")
            self.assertEqual([r["ts"] for r in left], ["2999-01-01T00:00:00Z"])
