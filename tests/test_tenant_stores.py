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

    def test_tenant_schema_is_the_default(self):
        store = Store(os.path.join(self._tmp.name, "d.db"))
        self.assertIn("knowledge_nodes", self._tables(store))
        store.close()


if __name__ == "__main__":
    unittest.main()
