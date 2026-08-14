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

    def test_explicit_db_path_without_data_dir(self):
        s = Settings(db_path="/custom/path/custom.db", data_dir="")
        self.assertEqual(s.resolve_db_path(), "/custom/path/custom.db")

    def test_data_dir_overrides_db_path(self):
        s = Settings(data_dir="/tenants/tenant_a", db_path="/other/db.db")
        self.assertEqual(s.resolve_db_path(), os.path.join("/tenants/tenant_a", "platform.db"))

    @patch.dict(os.environ, {"ANALYTICS_DATA_DIR": "/env/tenant_b"})
    def test_from_env_loads_analytics_data_dir(self):
        s = Settings.from_env()
        self.assertEqual(s.data_dir, "/env/tenant_b")
        self.assertEqual(s.resolve_db_path(), os.path.join("/env/tenant_b", "platform.db"))

    def test_from_env_defaults_embedding_fields_when_unset(self):
        # Regression: these three fields existed on the dataclass but from_env()
        # never read them, so an operator could not configure embeddings without
        # editing source. Defaults must still match the dataclass field defaults.
        with patch.dict(os.environ, {}, clear=True):
            s = Settings.from_env()
        self.assertTrue(s.embedding_enabled)
        self.assertEqual(s.embedding_model, "BAAI/bge-small-en-v1.5")
        self.assertEqual(
            s.embedding_query_prefix,
            "Represent this sentence for searching relevant passages: ")

    @patch.dict(os.environ, {"ANALYTICS_EMBEDDING_ENABLED": "0"})
    def test_from_env_can_disable_embeddings(self):
        self.assertFalse(Settings.from_env().embedding_enabled)

    @patch.dict(os.environ, {"ANALYTICS_EMBEDDING_MODEL": "some/other-model"})
    def test_from_env_loads_analytics_embedding_model(self):
        self.assertEqual(Settings.from_env().embedding_model, "some/other-model")

    @patch.dict(os.environ, {"ANALYTICS_EMBEDDING_QUERY_PREFIX": "prefix: "})
    def test_from_env_loads_analytics_embedding_query_prefix(self):
        self.assertEqual(Settings.from_env().embedding_query_prefix, "prefix: ")


if __name__ == "__main__":
    unittest.main()
