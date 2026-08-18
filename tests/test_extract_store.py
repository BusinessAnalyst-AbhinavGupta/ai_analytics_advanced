"""Task 1 — the durable Parquet layer under every conversation extract."""
from __future__ import annotations

import json
import os

import pandas as pd
import pytest

from analytics_platform.execution.extract_store import ExtractStore, ExtractMeta


def _meta(label="df_1", rows=3, **kw):
    d = dict(label=label, description="q", grain=["session_id"],
             columns=["session_id", "revenue"],
             dtypes={"session_id": "object", "revenue": "int64"},
             row_count=rows, truncated=False, sql="SELECT 1",
             created_at="2026-08-15T00:00:00Z")
    d.update(kw)
    return ExtractMeta(**d)


def test_put_then_load_roundtrips(tmp_path):
    store = ExtractStore(str(tmp_path))
    df = pd.DataFrame({"session_id": ["a", "b", "c"], "revenue": [1, 2, 3]})
    store.put("acme", "conv_1", _meta(), df)
    back = store.load("acme", "conv_1", "df_1")
    pd.testing.assert_frame_equal(back, df)
    assert store.meta("acme", "conv_1", "df_1").grain == ["session_id"]


def test_tenants_get_separate_directories(tmp_path):
    store = ExtractStore(str(tmp_path))
    df = pd.DataFrame({"session_id": ["a"], "revenue": [1]})
    store.put("acme", "conv_1", _meta(), df)
    store.put("globex", "conv_1", _meta(), df)
    assert store.load("globex", "conv_1", "df_1") is not None
    assert "acme" not in store.path("globex", "conv_1", "df_1")


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", "x" * 65, "a b"])
def test_path_traversal_is_rejected(tmp_path, bad):
    store = ExtractStore(str(tmp_path))
    with pytest.raises(ValueError):
        store.dir_for(bad, "conv_1")
    with pytest.raises(ValueError):
        store.dir_for("acme", bad)


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", "x" * 65, "a b"])
def test_a_bad_label_is_rejected_before_it_reaches_the_filesystem(tmp_path, bad):
    store = ExtractStore(str(tmp_path))
    with pytest.raises(ValueError):
        store.path("acme", "conv_1", bad)


def test_missing_extract_returns_none(tmp_path):
    store = ExtractStore(str(tmp_path))
    assert store.load("acme", "conv_1", "df_9") is None
    assert store.meta("acme", "conv_1", "df_9") is None


def test_delete_conversation_removes_everything(tmp_path):
    store = ExtractStore(str(tmp_path))
    store.put("acme", "conv_1", _meta(), pd.DataFrame({"session_id": ["a"], "revenue": [1]}))
    store.delete_conversation("acme", "conv_1")
    assert store.list_metas("acme", "conv_1") == []


def test_list_metas_returns_every_sidecar_in_label_order(tmp_path):
    store = ExtractStore(str(tmp_path))
    df = pd.DataFrame({"session_id": ["a"], "revenue": [1]})
    store.put("acme", "c1", _meta("df_2"), df)
    store.put("acme", "c1", _meta("df_1"), df)
    assert [m.label for m in store.list_metas("acme", "c1")] == ["df_1", "df_2"]


def test_a_sidecar_without_its_parquet_is_not_reported_as_complete(tmp_path):
    """The parquet is written first, so a json-only pair means a half-write."""
    store = ExtractStore(str(tmp_path))
    store.put("acme", "c1", _meta(), pd.DataFrame({"session_id": ["a"], "revenue": [1]}))
    os.remove(store.path("acme", "c1", "df_1"))
    assert store.meta("acme", "c1", "df_1") is None
    assert store.list_metas("acme", "c1") == []


def test_a_corrupt_parquet_degrades_to_none_instead_of_raising(tmp_path):
    """A bad extract must fall back to 're-run SQL', never crash a chat turn."""
    store = ExtractStore(str(tmp_path))
    store.put("acme", "c1", _meta(), pd.DataFrame({"session_id": ["a"], "revenue": [1]}))
    with open(store.path("acme", "c1", "df_1"), "w") as fh:
        fh.write("not parquet")
    assert store.load("acme", "c1", "df_1") is None


def test_cube_fields_survive_the_roundtrip(tmp_path):
    """Task 10 reads these off the sidecar; they must persist verbatim."""
    store = ExtractStore(str(tmp_path))
    m = _meta(base_view="checkout_sessions", population_hash="pop_A",
              projection_hash="proj_A", dimensions=["country", "device"],
              non_additive=["unique_users"], filters={"country": ["Germany"]},
              time_column="date", time_start="2026-08-01", time_end="2026-08-31")
    store.put("acme", "c1", m, pd.DataFrame({"session_id": ["a"], "revenue": [1]}))
    back = store.meta("acme", "c1", "df_1")
    assert back.population_hash == "pop_A"
    assert back.dimensions == ["country", "device"]
    assert back.filters == {"country": ["Germany"]}
    assert back.non_additive == ["unique_users"]
    assert back.time_start == "2026-08-01"


def test_sweep_removes_stale_conversations_and_keeps_fresh_ones(tmp_path):
    store = ExtractStore(str(tmp_path))
    df = pd.DataFrame({"session_id": ["a"], "revenue": [1]})
    store.put("acme", "old", _meta(created_at="2020-01-01T00:00:00Z"), df)
    store.put("acme", "new", _meta(), df)
    # `new` is stamped in 2026; anchor the cutoff so the test is not time-dependent.
    removed = store.sweep(retention_days=30, now="2026-08-20T00:00:00Z")
    assert removed == 1
    assert store.list_metas("acme", "old") == []
    assert [m.label for m in store.list_metas("acme", "new")] == ["df_1"]


def test_sweep_on_an_empty_tree_is_a_noop(tmp_path):
    assert ExtractStore(str(tmp_path)).sweep(retention_days=30) == 0
