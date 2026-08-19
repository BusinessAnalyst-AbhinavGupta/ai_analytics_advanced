"""Tests for ConversationDataCache -- the in-process, per-conversation
DataFrame cache the Python compute path reads from and writes to."""
from __future__ import annotations

import tempfile
import unittest

import pandas as pd

from analytics_platform.execution.dataframe_cache import ConversationDataCache
from analytics_platform.execution.extract_store import ExtractMeta, ExtractStore


def _meta(label="df_1", **kw):
    d = dict(label=label, description="q", grain=["session_id"],
             columns=["session_id"], dtypes={"session_id": "object"},
             row_count=1, truncated=False, sql="SELECT 1",
             created_at="2026-08-15T00:00:00Z")
    d.update(kw)
    return ExtractMeta(**d)


class TestConversationDataCache(unittest.TestCase):
    def setUp(self):
        self.cache = ConversationDataCache(max_conversations=2, max_frames_per_conversation=2)
        self.df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})

    def test_put_then_get_roundtrips_the_dataframe(self):
        self.cache.put("t1", "c1", "df_1", "orders by month", self.df)
        got = self.cache.get("t1", "c1", "df_1")
        pd.testing.assert_frame_equal(got, self.df)

    def test_get_missing_label_returns_none(self):
        self.assertIsNone(self.cache.get("t1", "c1", "df_1"))

    def test_tenant_isolation_same_conversation_id_different_tenant(self):
        self.cache.put("t1", "c1", "df_1", "orders", self.df)
        self.assertIsNone(self.cache.get("t2", "c1", "df_1"))

    def test_list_available_returns_schema_not_data(self):
        self.cache.put("t1", "c1", "df_1", "orders by month", self.df)
        available = self.cache.list_available("t1", "c1")
        self.assertEqual(len(available), 1)
        entry = available[0]
        self.assertEqual(entry["label"], "df_1")
        self.assertEqual(entry["description"], "orders by month")
        self.assertEqual(entry["columns"], ["a", "b"])
        self.assertEqual(entry["row_count"], 3)
        self.assertNotIn("df", entry)

    def test_list_available_empty_conversation_returns_empty_list(self):
        self.assertEqual(self.cache.list_available("t1", "c1"), [])

    def test_frames_beyond_cap_evict_oldest_first(self):
        self.cache.put("t1", "c1", "df_1", "first", self.df)
        self.cache.put("t1", "c1", "df_2", "second", self.df)
        self.cache.put("t1", "c1", "df_3", "third", self.df)  # cap is 2
        labels = {f["label"] for f in self.cache.list_available("t1", "c1")}
        self.assertEqual(labels, {"df_2", "df_3"})

    def test_conversations_beyond_cap_evict_oldest_first(self):
        self.cache.put("t1", "c1", "df_1", "first", self.df)
        self.cache.put("t1", "c2", "df_1", "second", self.df)
        self.cache.put("t1", "c3", "df_1", "third", self.df)  # cap is 2
        self.assertIsNone(self.cache.get("t1", "c1", "df_1"))
        self.assertIsNotNone(self.cache.get("t1", "c2", "df_1"))
        self.assertIsNotNone(self.cache.get("t1", "c3", "df_1"))

    def test_next_label_increments_and_skips_existing(self):
        self.assertEqual(self.cache.next_label("t1", "c1"), "df_1")
        self.cache.put("t1", "c1", "df_1", "first", self.df)
        self.assertEqual(self.cache.next_label("t1", "c1"), "df_2")

    def test_next_label_is_monotonic_across_frame_eviction(self):
        """Frame-level LRU eviction frees a label *name*. Reissuing it would let two
        different queries in one persisted conversation share a df_label, which is
        exactly what made the storyline Code Appendix attribute a Python turn to the
        wrong SQL turn. The counter is not derived from the live frames."""
        issued = [self.cache.next_label("t1", "c1")]
        self.cache.put("t1", "c1", issued[-1], "d", self.df)
        for _ in range(3):  # cap is 2, so the earliest frames get evicted
            label = self.cache.next_label("t1", "c1")
            issued.append(label)
            self.cache.put("t1", "c1", label, "d", self.df)
        self.assertEqual(issued, ["df_1", "df_2", "df_3", "df_4"])
        self.assertEqual(len(set(issued)), len(issued))
        self.assertEqual({f["label"] for f in self.cache.list_available("t1", "c1")},
                         {"df_3", "df_4"})

    def test_get_promotes_conversation_ahead_of_eviction(self):
        cache = ConversationDataCache(max_conversations=2, max_frames_per_conversation=2)
        cache.put("t1", "c1", "df_1", "first", self.df)
        cache.put("t1", "c2", "df_1", "second", self.df)
        cache.get("t1", "c1", "df_1")  # touch c1 so it's no longer the LRU entry
        cache.put("t1", "c3", "df_1", "third", self.df)  # should evict c2, not c1
        self.assertIsNotNone(cache.get("t1", "c1", "df_1"))
        self.assertIsNone(cache.get("t1", "c2", "df_1"))


class TestDurableConversationDataCache(unittest.TestCase):
    """Task 2 -- a cache miss now falls back to Parquet instead of forcing new SQL,
    which is what makes a reopened conversation still Python-capable."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExtractStore(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_get_falls_back_to_disk_after_memory_eviction(self):
        cache = ConversationDataCache(max_frames_per_conversation=1, store=self.store)
        df1 = pd.DataFrame({"session_id": ["a"], "revenue": [1]})
        df2 = pd.DataFrame({"session_id": ["b"], "revenue": [2]})
        cache.put("acme", "c1", "df_1", "q1", df1, meta=_meta("df_1"))
        cache.put("acme", "c1", "df_2", "q2", df2, meta=_meta("df_2"))  # evicts df_1
        pd.testing.assert_frame_equal(cache.get("acme", "c1", "df_1"), df1)

    def test_list_available_unions_memory_and_disk(self):
        cache = ConversationDataCache(max_frames_per_conversation=1, store=self.store)
        cache.put("acme", "c1", "df_1", "q1", pd.DataFrame({"session_id": ["a"]}),
                  meta=_meta("df_1"))
        cache.put("acme", "c1", "df_2", "q2", pd.DataFrame({"session_id": ["b"]}),
                  meta=_meta("df_2"))
        labels = {f["label"] for f in cache.list_available("acme", "c1")}
        self.assertEqual(labels, {"df_1", "df_2"})

    def test_a_hot_frames_real_row_count_wins_over_the_sidecar(self):
        """The sidecar records what was written; memory holds what is actually there."""
        cache = ConversationDataCache(store=self.store)
        cache.put("acme", "c1", "df_1", "q", pd.DataFrame({"session_id": ["a", "b", "c"]}),
                  meta=_meta("df_1", row_count=1))
        entry = cache.list_available("acme", "c1")[0]
        self.assertEqual(entry["row_count"], 3)

    def test_describe_exposes_grain_and_sample(self):
        cache = ConversationDataCache(store=self.store)
        cache.put("acme", "c1", "df_1", "q",
                  pd.DataFrame({"session_id": ["a", "b", "c", "d"]}), meta=_meta("df_1"))
        d = cache.list_available("acme", "c1")[0]
        self.assertEqual(d["grain"], ["session_id"])
        self.assertEqual(len(d["sample"]), 3)

    def test_describe_carries_the_cube_fields_the_data_manager_reads(self):
        cache = ConversationDataCache(store=self.store)
        cache.put("acme", "c1", "df_1", "q", pd.DataFrame({"country": ["DE"]}),
                  meta=_meta("df_1", population_hash="pop_A", dimensions=["country"],
                             base_view="checkout_sessions", truncated=True))
        d = cache.list_available("acme", "c1")[0]
        self.assertEqual(d["population_hash"], "pop_A")
        self.assertEqual(d["dimensions"], ["country"])
        self.assertEqual(d["base_view"], "checkout_sessions")
        self.assertTrue(d["truncated"])

    def test_the_sample_is_json_safe(self):
        """describe() output goes into an LLM prompt and a JSON payload; a Timestamp
        or a numpy scalar in there would break serialization at the API boundary."""
        import json
        cache = ConversationDataCache(store=self.store)
        cache.put("acme", "c1", "df_1", "q",
                  pd.DataFrame({"d": pd.to_datetime(["2026-01-01"]), "n": [1]}),
                  meta=_meta("df_1"))
        json.dumps(cache.list_available("acme", "c1"))

    def test_a_cold_cache_serves_a_conversation_entirely_from_disk(self):
        """The point of durability: a fresh process must find the extracts."""
        warm = ConversationDataCache(store=self.store)
        warm.put("acme", "c1", "df_1", "q", pd.DataFrame({"session_id": ["a"]}),
                 meta=_meta("df_1"))
        cold = ConversationDataCache(store=ExtractStore(self._tmp.name))
        self.assertIsNotNone(cold.get("acme", "c1", "df_1"))
        self.assertEqual([f["label"] for f in cold.list_available("acme", "c1")], ["df_1"])

    def test_next_label_accounts_for_frames_only_on_disk(self):
        """A cold process must not reissue df_1 over an existing extract."""
        warm = ConversationDataCache(store=self.store)
        warm.put("acme", "c1", "df_1", "q", pd.DataFrame({"session_id": ["a"]}),
                 meta=_meta("df_1"))
        cold = ConversationDataCache(store=ExtractStore(self._tmp.name))
        self.assertEqual(cold.next_label("acme", "c1"), "df_2")

    def test_an_unwritable_store_degrades_to_memory_instead_of_failing_the_turn(self):
        class Broken:
            def put(self, *a, **k):
                raise OSError("disk full")

            def load(self, *a, **k):
                return None

            def list_metas(self, *a, **k):
                return []

        cache = ConversationDataCache(store=Broken())
        cache.put("acme", "c1", "df_1", "q", pd.DataFrame({"a": [1]}), meta=_meta("df_1"))
        self.assertIsNotNone(cache.get("acme", "c1", "df_1"))

    def test_cache_without_store_behaves_exactly_as_before(self):
        cache = ConversationDataCache()  # no store
        cache.put("acme", "c1", "df_1", "q", pd.DataFrame({"a": [1]}))
        self.assertIsNotNone(cache.get("acme", "c1", "df_1"))
        self.assertIsNone(cache.get("acme", "c1", "df_9"))

    def test_put_without_meta_writes_nothing_to_disk(self):
        cache = ConversationDataCache(store=self.store)
        cache.put("acme", "c1", "df_1", "q", pd.DataFrame({"a": [1]}))
        self.assertEqual(self.store.list_metas("acme", "c1"), [])


if __name__ == "__main__":
    unittest.main()
