"""Control-plane and tenant-plane data land in the right database."""
from __future__ import annotations

import os
import tempfile
import unittest

from analytics_platform.stores import TenantStoreProvider
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


if __name__ == "__main__":
    unittest.main()
