"""Task 9 -- DuckDB over the durable Parquet cache.

Once extracts are Parquet on disk, the follow-up questions that used to cost a
Metabase round trip are set operations: filter, group, join two extracts on a
shared key, window. DuckDB does those over Parquet directly, in one line of SQL
the analyst is already fluent in. Python is not displaced -- statistics,
decomposition, and chart specs stay in the sandbox, reading the same files.
"""
from __future__ import annotations

import tempfile
import unittest

import pandas as pd

from analytics_platform.execution.extract_store import ExtractMeta, ExtractStore
from analytics_platform.execution.workspace import (WORKSPACE_RESULT_ROW_CAP,
                                                    AnalyticalWorkspace)


def _meta(label="df_1", **kw):
    d = dict(label=label, description="q", grain=["session_id"], columns=[],
             dtypes={}, row_count=0, truncated=False, sql="SELECT 1",
             created_at="2026-08-15T00:00:00Z")
    d.update(kw)
    return ExtractMeta(**d)


class TestAnalyticalWorkspace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExtractStore(self._tmp.name)
        self.ws = AnalyticalWorkspace(self.store)

    def tearDown(self):
        self.ws.close_all()
        self._tmp.cleanup()

    def put(self, label, df, tenant="acme", conv="c1"):
        self.store.put(tenant, conv, _meta(label), df)

    def test_a_registered_extract_is_queryable_as_a_view(self):
        self.put("df_1", pd.DataFrame({"session_id": ["a", "b"], "revenue": [1, 2]}))
        self.ws.register("acme", "c1", "df_1")
        res = self.ws.query("acme", "c1", "SELECT SUM(revenue) AS total FROM df_1")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.data["total"][0], 3)

    def test_two_extracts_can_be_joined_locally(self):
        self.put("df_1", pd.DataFrame({"session_id": ["a", "b"], "revenue": [10, 5]}))
        self.put("df_2", pd.DataFrame({"session_id": ["a", "b"],
                                       "device": ["ios", "android"]}))
        res = self.ws.query("acme", "c1",
                            "SELECT d.device, SUM(f.revenue) AS r FROM df_1 f "
                            "JOIN df_2 d USING (session_id) GROUP BY d.device")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(set(res.data["device"]), {"android", "ios"})

    def test_a_cold_workspace_rebuilds_its_views_from_disk(self):
        """The whole point of durability: a fresh process must find the extracts."""
        self.put("df_1", pd.DataFrame({"session_id": ["a"]}))
        ws2 = AnalyticalWorkspace(ExtractStore(self._tmp.name))   # never saw the put
        try:
            res = ws2.query("acme", "c1", "SELECT COUNT(*) AS n FROM df_1")
            self.assertTrue(res.ok, res.error)
            self.assertEqual(res.data["n"][0], 1)
        finally:
            ws2.close_all()

    def test_views_lists_what_is_registered(self):
        self.put("df_1", pd.DataFrame({"a": [1]}))
        self.put("df_2", pd.DataFrame({"a": [1]}))
        self.assertEqual(sorted(self.ws.views("acme", "c1")), ["df_1", "df_2"])

    def test_an_extract_added_after_connect_can_be_registered(self):
        self.put("df_1", pd.DataFrame({"a": [1]}))
        self.ws.connect("acme", "c1")
        self.put("df_2", pd.DataFrame({"a": [2]}))
        self.assertTrue(self.ws.register("acme", "c1", "df_2"))
        self.assertTrue(self.ws.query("acme", "c1", "SELECT * FROM df_2").ok)

    def test_registering_a_missing_extract_returns_false(self):
        self.assertFalse(self.ws.register("acme", "c1", "df_9"))

    def test_tenants_cannot_see_each_others_views(self):
        self.put("df_1", pd.DataFrame({"a": [1]}), tenant="acme")
        res = self.ws.query("globex", "c1", "SELECT * FROM df_1")
        self.assertFalse(res.ok)
        self.assertIn("df_1", res.error)

    def test_conversations_cannot_see_each_others_views(self):
        self.put("df_1", pd.DataFrame({"a": [1]}), conv="c1")
        self.assertFalse(self.ws.query("acme", "c2", "SELECT * FROM df_1").ok)

    def test_a_write_statement_is_rejected_by_policy(self):
        self.put("df_1", pd.DataFrame({"a": [1]}))
        self.assertFalse(self.ws.query("acme", "c1", "DROP VIEW df_1").ok)
        self.assertFalse(self.ws.query("acme", "c1", "DELETE FROM df_1").ok)

    def test_extension_loading_is_rejected(self):
        """No INSTALL/LOAD: this engine is embedded and read-only over Parquet,
        and must never reach the network."""
        self.put("df_1", pd.DataFrame({"a": [1]}))
        for sql in ("INSTALL httpfs", "LOAD httpfs", "ATTACH 'other.db'"):
            self.assertFalse(self.ws.query("acme", "c1", sql).ok, sql)

    def test_external_access_over_the_network_is_unavailable(self):
        """httpfs cannot be autoloaded, so a remote read fails rather than
        silently fetching. Note enable_external_access=false is NOT usable here:
        it would also block reading the local Parquet files this engine exists
        to query."""
        self.put("df_1", pd.DataFrame({"a": [1]}))
        res = self.ws.query("acme", "c1",
                            "SELECT * FROM read_csv_auto('https://example.com/x.csv')")
        self.assertFalse(res.ok)

    def test_a_broken_query_returns_an_error_not_a_raise(self):
        self.put("df_1", pd.DataFrame({"a": [1]}))
        res = self.ws.query("acme", "c1", "SELECT nope FROM df_1")
        self.assertFalse(res.ok)
        self.assertTrue(res.error)

    def test_a_huge_result_is_truncated_and_says_so(self):
        self.put("df_1", pd.DataFrame({"n": range(WORKSPACE_RESULT_ROW_CAP + 10)}))
        res = self.ws.query("acme", "c1", "SELECT * FROM df_1")
        self.assertTrue(res.ok, res.error)
        self.assertTrue(res.truncated)
        self.assertEqual(res.row_count, WORKSPACE_RESULT_ROW_CAP)

    def test_a_result_at_exactly_the_cap_is_not_called_truncated(self):
        self.put("df_1", pd.DataFrame({"n": range(WORKSPACE_RESULT_ROW_CAP)}))
        res = self.ws.query("acme", "c1", "SELECT * FROM df_1")
        self.assertFalse(res.truncated)

    def test_the_result_carries_the_sql_that_produced_it(self):
        self.put("df_1", pd.DataFrame({"a": [1]}))
        res = self.ws.query("acme", "c1", "SELECT * FROM df_1")
        self.assertIn("df_1", res.sql)

    def test_parquet_paths_are_exposed_for_the_sandbox(self):
        """Both paths read the same files, so DuckDB and Python see identical data."""
        self.put("df_1", pd.DataFrame({"a": [1]}))
        self.assertTrue(self.ws.parquet_paths("acme", "c1")["df_1"].endswith("df_1.parquet"))

    def test_an_unsafe_label_never_reaches_an_identifier_position(self):
        with self.assertRaises(ValueError):
            self.ws.register("acme", "c1", "df_1; DROP TABLE x")

    def test_close_drops_the_connection_and_it_rebuilds_on_demand(self):
        self.put("df_1", pd.DataFrame({"a": [1]}))
        self.ws.query("acme", "c1", "SELECT * FROM df_1")
        self.ws.close("acme", "c1")
        self.assertTrue(self.ws.query("acme", "c1", "SELECT * FROM df_1").ok)

    def test_closing_an_unknown_conversation_is_a_noop(self):
        self.ws.close("acme", "never")

    def test_a_query_against_an_empty_workspace_errors_rather_than_raising(self):
        res = self.ws.query("acme", "c1", "SELECT 1 AS n FROM df_1")
        self.assertFalse(res.ok)


if __name__ == "__main__":
    unittest.main()
