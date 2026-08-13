"""Per-tenant databases: schema split, provider routing, ownership enforcement."""
from __future__ import annotations

import os
import tempfile
import unittest

from analytics_platform.database import CONTROL_SCHEMA, TENANT_SCHEMA, Store

CONTROL_TABLES = {"tenants", "scheduler_state", "api_logs", "auth_principals"}
TENANT_TABLES = {
    "company_profiles", "data_sources", "knowledge_nodes", "questions",
    "analysis_runs", "telemetry", "stakeholder_answers", "stakeholder_feedback",
    "research_sources", "research_docs", "audit_log", "company_profile_history",
    "analyst_configs", "analyst_config_history", "kpis", "db_owner",
}


class SchemaSplitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _tables(self, store: Store) -> set:
        rows = store.query_all(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")
        return {r["name"] for r in rows}

    def test_control_store_has_the_control_tables(self):
        store = Store(os.path.join(self._tmp.name, "control.db"), schema=CONTROL_SCHEMA)
        self.assertTrue(CONTROL_TABLES.issubset(self._tables(store)))
        store.close()

    def test_control_store_has_no_tenant_tables(self):
        store = Store(os.path.join(self._tmp.name, "control.db"), schema=CONTROL_SCHEMA)
        self.assertEqual(self._tables(store) & {"knowledge_nodes", "analysis_runs"}, set())
        store.close()

    def test_tenant_store_has_the_tenant_tables(self):
        store = Store(os.path.join(self._tmp.name, "t.db"), schema=TENANT_SCHEMA)
        self.assertTrue(TENANT_TABLES.issubset(self._tables(store)))
        store.close()

    def test_tenant_store_has_no_tenants_registry(self):
        """A tenant database must not be able to see the list of other companies."""
        store = Store(os.path.join(self._tmp.name, "t.db"), schema=TENANT_SCHEMA)
        self.assertNotIn("tenants", self._tables(store))
        store.close()

    def test_the_two_schemas_do_not_overlap(self):
        self.assertEqual(CONTROL_TABLES & TENANT_TABLES, set())

    def test_bare_store_still_gets_the_combined_schema(self):
        """Every unmigrated caller in the codebase relies on this until Task 5."""
        store = Store(os.path.join(self._tmp.name, "d.db"))
        tables = self._tables(store)
        self.assertIn("knowledge_nodes", tables)  # tenant-plane table
        self.assertIn("tenants", tables)          # control-plane table
        store.close()


from analytics_platform.stores import TenantIsolationError, TenantStoreProvider


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.provider = TenantStoreProvider(
            control_db_path=os.path.join(self.root, "control.db"),
            tenants_root=os.path.join(self.root, "tenants"))

    def tearDown(self):
        self.provider.close_all()
        self._tmp.cleanup()

    def test_each_tenant_gets_its_own_file(self):
        a = self.provider.for_tenant("acme")
        b = self.provider.for_tenant("globex")
        self.assertNotEqual(a.db_path, b.db_path)

    def test_both_files_exist_on_disk(self):
        self.provider.for_tenant("acme")
        self.provider.for_tenant("globex")
        self.assertTrue(os.path.exists(self.provider.tenant_db_path("acme")))
        self.assertTrue(os.path.exists(self.provider.tenant_db_path("globex")))

    def test_the_store_is_cached_per_tenant(self):
        self.assertIs(self.provider.for_tenant("acme"),
                      self.provider.for_tenant("acme"))

    def test_writes_do_not_leak_between_tenants(self):
        """The isolation guarantee, stated as a test."""
        a = self.provider.for_tenant("acme")
        a.execute(
            "INSERT INTO knowledge_nodes (id,tenant_id,kind,status,version,title,"
            "summary,payload,confidence,evidence_ref,source_ref,created_at,"
            "updated_at,created_by,reviewed_by,review_notes,supersedes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("kn_1", "acme", "METRIC", "APPROVED", 1, "Secret metric", "",
             "{}", "{}", "", "", "2026-01-01", "2026-01-01", "s", "", "", ""))
        b = self.provider.for_tenant("globex")
        self.assertEqual(b.query_all("SELECT id FROM knowledge_nodes"), [])

    def test_a_tenant_database_cannot_be_opened_by_another_tenant(self):
        self.provider.for_tenant("acme")
        self.provider.close_all()
        # Point globex at acme's file, as a mis-set path would.
        rogue = TenantStoreProvider(
            control_db_path=os.path.join(self.root, "control.db"),
            tenants_root=os.path.join(self.root, "tenants"))
        rogue._path_override = self.provider.tenant_db_path("acme")
        with self.assertRaises(TenantIsolationError):
            rogue.for_tenant("globex")
        rogue.close_all()

    def test_ownership_is_recorded_on_first_open(self):
        store = self.provider.for_tenant("acme")
        row = store.query_one("SELECT tenant_id FROM db_owner WHERE singleton = 1")
        self.assertEqual(row["tenant_id"], "acme")

    def test_reopening_the_same_tenant_is_fine(self):
        self.provider.for_tenant("acme")
        self.provider.close_all()
        again = TenantStoreProvider(
            control_db_path=os.path.join(self.root, "control.db"),
            tenants_root=os.path.join(self.root, "tenants"))
        self.assertIsNotNone(again.for_tenant("acme"))
        again.close_all()

    def test_the_control_store_holds_the_registry(self):
        self.assertIsNotNone(
            self.provider.control.query_all("SELECT id FROM tenants"))

    def test_the_control_store_has_no_knowledge_nodes(self):
        with self.assertRaises(Exception):
            self.provider.control.query_all("SELECT id FROM knowledge_nodes")

    def test_known_tenants_lists_databases_on_disk(self):
        self.provider.for_tenant("acme")
        self.provider.for_tenant("globex")
        self.assertEqual(sorted(self.provider.known_tenants()), ["acme", "globex"])


class TenantIdSafetyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.provider = TenantStoreProvider(
            control_db_path=os.path.join(self._tmp.name, "control.db"),
            tenants_root=os.path.join(self._tmp.name, "tenants"))

    def tearDown(self):
        self.provider.close_all()
        self._tmp.cleanup()

    def test_path_traversal_is_refused(self):
        with self.assertRaises(ValueError):
            self.provider.for_tenant("../../etc")

    def test_a_separator_is_refused(self):
        with self.assertRaises(ValueError):
            self.provider.for_tenant("acme/globex")

    def test_an_empty_id_is_refused(self):
        with self.assertRaises(ValueError):
            self.provider.for_tenant("")

    def test_a_dot_id_is_refused(self):
        with self.assertRaises(ValueError):
            self.provider.for_tenant(".")

    def test_a_normal_id_is_accepted(self):
        self.assertIsNotNone(self.provider.for_tenant("tnt_d23cd823d4c6"))


if __name__ == "__main__":
    unittest.main()
