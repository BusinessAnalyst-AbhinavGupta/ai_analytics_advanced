"""Tests for analytics_platform/config.py (Settings & tenant isolation paths)."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from analytics_platform.config import Settings


class TestSettings(unittest.TestCase):
    def test_default_paths_without_data_dir(self):
        s = Settings(data_dir="")
        self.assertEqual(s.data_dir, "")
        self.assertEqual(s.resolve_db_path(), "data/platform.db")
        self.assertEqual(s.resolve_vector_path(), ".chroma_db")

    def test_explicit_db_path_without_data_dir(self):
        s = Settings(db_path="/custom/path/custom.db", data_dir="")
        self.assertEqual(s.resolve_db_path(), "/custom/path/custom.db")

    def test_data_dir_overrides_db_and_vector_paths(self):
        s = Settings(data_dir="/tenants/tenant_a", db_path="/other/db.db")
        self.assertEqual(s.resolve_db_path(), os.path.join("/tenants/tenant_a", "platform.db"))
        self.assertEqual(s.resolve_vector_path(), os.path.join("/tenants/tenant_a", ".chroma_db"))

    @patch.dict(os.environ, {"ANALYTICS_DATA_DIR": "/env/tenant_b"})
    def test_from_env_loads_analytics_data_dir(self):
        s = Settings.from_env()
        self.assertEqual(s.data_dir, "/env/tenant_b")
        self.assertEqual(s.resolve_db_path(), os.path.join("/env/tenant_b", "platform.db"))
        self.assertEqual(s.resolve_vector_path(), os.path.join("/env/tenant_b", ".chroma_db"))


if __name__ == "__main__":
    unittest.main()
