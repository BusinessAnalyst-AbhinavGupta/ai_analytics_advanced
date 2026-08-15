"""Tests for ConversationDataCache -- the in-process, per-conversation
DataFrame cache the Python compute path reads from and writes to."""
from __future__ import annotations

import unittest

import pandas as pd

from analytics_platform.execution.dataframe_cache import ConversationDataCache


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


if __name__ == "__main__":
    unittest.main()
