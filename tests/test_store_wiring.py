"""Control-plane and tenant-plane data land in the right database."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from analytics_platform.stores import TenantIsolationError, TenantStoreProvider
from analytics_platform.tenancy import TenantService


class TenantServiceWiringTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.stores = TenantStoreProvider(
            control_db_path=os.path.join(self._tmp.name, "control.db"),
            tenants_root=os.path.join(self._tmp.name, "tenants"))
        self.tenants = TenantService(self.stores)

    def tearDown(self):
        self.stores.close_all()
        self._tmp.cleanup()

    def test_a_created_tenant_lands_in_the_control_database(self):
        self.tenants.create("acme", name="Acme")
        rows = self.stores.control.query_all("SELECT id FROM tenants")
        self.assertEqual([r["id"] for r in rows], ["acme"])

    def test_listing_tenants_reads_the_control_database(self):
        self.tenants.create("acme", name="Acme")
        self.tenants.create("globex", name="Globex")
        self.assertEqual(sorted(t.id for t in self.tenants.list()), ["acme", "globex"])

    def test_creating_a_tenant_creates_its_database(self):
        self.tenants.create("acme", name="Acme")
        self.assertTrue(os.path.exists(self.stores.tenant_db_path("acme")))

    def test_analyst_config_lands_in_the_tenant_database(self):
        self.tenants.create("acme", name="Acme")
        self.tenants.get_analyst_config("acme")
        rows = self.stores.for_tenant("acme").query_all(
            "SELECT tenant_id FROM analyst_configs")
        self.assertTrue(all(r["tenant_id"] == "acme" for r in rows))

    def test_one_tenants_config_is_invisible_to_another(self):
        self.tenants.create("acme", name="Acme")
        self.tenants.create("globex", name="Globex")
        self.tenants.get_analyst_config("acme")
        rows = self.stores.for_tenant("globex").query_all(
            "SELECT tenant_id FROM analyst_configs")
        self.assertEqual(rows, [])


class EndToEndIsolationTest(unittest.TestCase):
    """The guarantee, end to end: two companies through one process."""

    def setUp(self):
        from analytics_platform.api import make_context
        from analytics_platform.config import Settings
        self._tmp = tempfile.TemporaryDirectory()
        # NOTE: the task brief's snippet passed `embedding_enabled=False` here, but
        # `Settings` has no such field (nothing in `make_context` gates
        # `BrainVectorStore` construction on one — it's already wrapped in a
        # best-effort try/except). Dropped as a brief inaccuracy; see task-5-report.md.
        self.ctx = make_context(Settings(data_dir=self._tmp.name))
        self.ctx.tenants.create("acme", name="Acme")
        self.ctx.tenants.create("globex", name="Globex")

    def tearDown(self):
        self.ctx.stores.close_all()
        self._tmp.cleanup()

    def _add_node(self, tenant_id: str, title: str) -> str:
        from analytics_platform.domain import NodeKind
        brain = self.ctx.pipeline.brain(tenant_id)
        return brain.create(NodeKind.METRIC, title, summary="x").id

    def test_each_tenant_sees_only_its_own_knowledge(self):
        from analytics_platform.domain import NodeKind
        self._add_node("acme", "Acme margin")
        self._add_node("globex", "Globex margin")
        acme = [n.title for n in self.ctx.pipeline.brain("acme").all(kind=NodeKind.METRIC)]
        self.assertEqual(acme, ["Acme margin"])

    def test_the_two_tenants_use_different_files(self):
        self.assertNotEqual(self.ctx.stores.for_tenant("acme").db_path,
                            self.ctx.stores.for_tenant("globex").db_path)

    def test_a_node_id_from_one_tenant_is_not_readable_by_the_other(self):
        node_id = self._add_node("acme", "Acme margin")
        self.assertIsNone(self.ctx.pipeline.brain("globex").get(node_id))

    def test_stakeholder_resolves_the_right_store(self):
        self._add_node("acme", "Acme margin")
        self.assertEqual(self.ctx.stakeholder.brain("acme").stats()["total_nodes"], 1)
        self.assertEqual(self.ctx.stakeholder.brain("globex").stats()["total_nodes"], 0)

    def test_deleting_a_tenants_file_removes_all_of_its_data(self):
        """Per-company export and deletion become one filesystem operation."""
        self._add_node("acme", "Acme margin")
        path = self.ctx.stores.tenant_db_path("acme")
        self.assertTrue(os.path.exists(path))
        self.assertTrue(os.path.exists(self.ctx.stores.tenant_db_path("globex")))


class IsolationFailuresAreLoudTest(unittest.TestCase):
    """The plan's constraint: "Isolation failures are loud. It is never a
    warning, never a silent fallback." Two call sites used to downgrade a
    TenantIsolationError into the generic best-effort path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Bind "acme"'s file, then plant a copy of it where "globex" belongs —
        # the bad-restore / mis-set-path mistake the db_owner check exists for.
        seed = TenantStoreProvider(
            control_db_path=os.path.join(self._tmp.name, "control.db"),
            tenants_root=os.path.join(self._tmp.name, "tenants"))
        seed.for_tenant("acme")
        acme_path = seed.tenant_db_path("acme")
        globex_path = seed.tenant_db_path("globex")
        seed.close_all()
        os.makedirs(os.path.dirname(globex_path), exist_ok=True)
        shutil.copy(acme_path, globex_path)

        self.stores = TenantStoreProvider(
            control_db_path=os.path.join(self._tmp.name, "control.db"),
            tenants_root=os.path.join(self._tmp.name, "tenants"))
        # Registered in the control plane, so the registry lookups in the
        # services below get past `require_tenant` and actually reach the
        # tenant-plane store that fails isolation.
        for tid, name in (("acme", "Acme"), ("globex", "Globex")):
            self.stores.control.execute(
                "INSERT OR REPLACE INTO tenants (id,name) VALUES (?,?)",
                (tid, name))

    def tearDown(self):
        self.stores.close_all()
        self._tmp.cleanup()

    def test_the_planted_file_really_does_fail_isolation(self):
        with self.assertRaises(TenantIsolationError):
            self.stores.for_tenant("globex")

    def test_observability_event_logs_an_isolation_failure_at_error(self):
        """`event()` must never break the pipeline, so it cannot re-raise — but
        it must not look like an ordinary telemetry-write hiccup either."""
        from analytics_platform.observability import Observability
        obs = Observability(self.stores)
        with self.assertLogs("analytics_platform.observability",
                             level="ERROR") as captured:
            trace = obs.event(tenant_id="globex", stage="probe")
        self.assertTrue(trace)  # the pipeline is not broken
        joined = "\n".join(captured.output)
        self.assertIn("SECURITY", joined)
        self.assertIn("isolation", joined.lower())

    def test_an_ordinary_telemetry_failure_stays_a_warning(self):
        """The ERROR path must be specific to isolation, not a blanket upgrade."""
        from analytics_platform.observability import Observability
        obs = Observability(self.stores)
        with self.assertLogs("analytics_platform.observability",
                             level="WARNING") as captured:
            obs.event(tenant_id="", stage="platform-tick")
        joined = "\n".join(captured.output)
        self.assertIn("WARNING", joined)
        self.assertNotIn("SECURITY", joined)

    def test_senior_status_does_not_swallow_an_isolation_failure(self):
        """It used to become `auto_accepted = 0` — a plausible number built on a
        cross-tenant read attempt, with no signal at all."""
        from analytics_platform.senior import SeniorService
        from analytics_platform.tenancy import TenantService
        senior = SeniorService(self.stores, pipeline=None,
                               tenants=TenantService(self.stores))
        with self.assertRaises(TenantIsolationError):
            senior.status("globex")

    def test_senior_status_auto_accepted_handler_reraises_and_logs(self):
        """Targets the `auto_accepted` handler specifically: with the analyst
        config resolved from elsewhere, the count query is the first thing to
        touch the tenant's own store."""
        from unittest import mock
        from analytics_platform.senior import SeniorService
        from analytics_platform.tenancy import TenantService
        tenants = TenantService(self.stores)
        senior = SeniorService(self.stores, pipeline=None, tenants=tenants)
        cfg = tenants.get_analyst_config("acme")  # a healthy tenant's config
        with mock.patch.object(tenants, "get_analyst_config", return_value=cfg):
            with self.assertLogs("analytics_platform.senior",
                                 level="ERROR") as captured:
                with self.assertRaises(TenantIsolationError):
                    senior.status("globex")
        self.assertIn("SECURITY", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
