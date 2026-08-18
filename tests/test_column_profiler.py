"""Task 5 -- real column values and real cardinalities in the Brain.

Without this, even a schema-aware LLM still guesses that a status column says
'COMPLETED' when the data says 'complete', and the cube cell guard (Task 7) has
no distinct counts to size a GROUP BY against.
"""
from __future__ import annotations

import unittest

import pandas as pd

from analytics_platform.domain import (PROFILE_CARDINALITY_CAP, PROFILE_TOP_VALUES,
                                       ColumnProfile, DataSourceKind, NodeKind)
from analytics_platform.execution.base import QueryResult
from analytics_platform.junior import JuniorEngine
from tests.helpers import make_ctx


class FakeExecutor:
    """Returns a canned frame for the sample query and a canned count for the
    COUNT(*) probe, counting calls so caching is observable."""

    def __init__(self):
        self._df = pd.DataFrame()
        self.call_count = 0
        self.row_total = None
        self.sql_seen = []

    def returns(self, df):
        self._df = df

    def supports(self, ctx):
        return True

    def session_status(self, tenant_id):
        from analytics_platform.execution.base import SessionStatus
        return SessionStatus(state="valid", tenant_id=tenant_id)

    def execute(self, sql, ctx):
        self.call_count += 1
        self.sql_seen.append(sql)
        if getattr(self, "truncate_sample", False) and not sql.lower().lstrip().startswith(
                "select count(*)"):
            return QueryResult(ok=True, data=self._df.copy(), row_count=len(self._df),
                               columns=list(self._df.columns), truncated=True)
        if sql.lower().lstrip().startswith("select count(*)"):
            total = self.row_total if self.row_total is not None else len(self._df)
            return QueryResult(ok=True, data=pd.DataFrame({"row_count": [total]}),
                               row_count=1, columns=["row_count"])
        return QueryResult(ok=True, data=self._df.copy(), row_count=len(self._df),
                           columns=list(self._df.columns))

    def cancel(self, execution_id):
        return True


class _ProfilerCase(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.tid = self.ctx.tenants.create_tenant("ProfileCo").id
        self.fake = FakeExecutor()
        self.engine = JuniorEngine(self.ctx.stores, executor=self.fake,
                                   tenants=self.ctx.tenants, settings=self.ctx.settings)
        for t in ("orders", "sessions", "events"):
            self.ctx.tenants.add_datasource(self.tid, t, DataSourceKind.DIRECT_DB,
                                            dialect="athena", tables=[t])

    def tearDown(self):
        self.ctx.close()

    def profile(self, table="orders", **kw):
        return self.engine.profile_tables(self.tid, [table], **kw)[table]

    def column(self, name, table="orders"):
        return next(p for p in self.profile(table) if p.column == name)


class TestColumnProfiler(_ProfilerCase):
    def test_low_cardinality_column_stores_every_value(self):
        self.fake.returns(pd.DataFrame({
            "order_id": [f"o{i}" for i in range(100)],
            "status": ["COMPLETED", "CANCELLED"] * 50,
        }))
        status = self.column("status")
        self.assertEqual(status.distinct_count, 2)
        self.assertEqual(sorted(status.values), ["CANCELLED", "COMPLETED"])
        self.assertIs(status.values_complete, True)

    def test_high_cardinality_column_is_capped_to_top_n(self):
        self.fake.returns(pd.DataFrame({"city": [f"city_{i % 300}" for i in range(3000)]}))
        city = self.column("city")
        self.assertEqual(city.distinct_count, 300)
        self.assertEqual(len(city.values), PROFILE_TOP_VALUES)
        self.assertIs(city.values_complete, False)

    def test_the_top_values_are_the_most_frequent_ones(self):
        self.fake.returns(pd.DataFrame({
            "city": ["Mumbai"] * 100 + ["Delhi"] * 50 + [f"c{i}" for i in range(200)]}))
        self.assertEqual(self.column("city").values[:2], ["Mumbai", "Delhi"])

    def test_a_saturated_sample_never_claims_completeness(self):
        """The sample hit its ceiling, so even a 2-value column might have unseen
        values. A values_complete that is really a sample artifact makes the LLM
        emit a WHERE status IN (...) that silently drops rows."""
        self.engine.settings.profile_sample_rows = 100
        self.fake.returns(pd.DataFrame({"status": ["A", "B"] * 50}))   # exactly 100 rows
        self.assertIs(self.column("status").values_complete, False)

    def test_numeric_and_date_columns_carry_a_range(self):
        self.fake.returns(pd.DataFrame({
            "revenue": [1.0, 500.0, 99.0],
            "order_date": pd.to_datetime(["2026-01-01", "2026-06-30", "2026-03-15"]),
        }))
        profiles = {p.column: p for p in self.profile()}
        self.assertEqual(profiles["revenue"].min_value, "1.0")
        self.assertEqual(profiles["revenue"].max_value, "500.0")
        self.assertTrue(profiles["order_date"].min_value.startswith("2026-01-01"))
        self.assertTrue(profiles["order_date"].max_value.startswith("2026-06-30"))

    def test_a_text_column_carries_no_range(self):
        self.fake.returns(pd.DataFrame({"status": ["A", "B"]}))
        self.assertEqual(self.column("status").min_value, "")

    def test_null_fraction_is_recorded(self):
        self.fake.returns(pd.DataFrame({"coupon": [None, None, "X", "Y"]}))
        self.assertEqual(self.column("coupon").null_fraction, 0.5)

    def test_long_values_are_truncated(self):
        """Values land in an LLM prompt; an unbounded free-text column would blow
        the context."""
        self.fake.returns(pd.DataFrame({"notes": ["x" * 5000, "y" * 5000]}))
        self.assertTrue(all(len(v) <= 100 for v in self.column("notes").values))

    def test_every_value_is_coerced_to_a_string(self):
        self.fake.returns(pd.DataFrame({"flag": [True, False]}))
        self.assertTrue(all(isinstance(v, str) for v in self.column("flag").values))

    def test_profiles_persist_as_one_node_per_table(self):
        self.fake.returns(pd.DataFrame({"a": [1]}))
        self.engine.profile_tables(self.tid, ["orders", "sessions"])
        titles = {n.title for n in self.engine.brain(self.tid).all(kind=NodeKind.DEFINITION)}
        self.assertIn("Column Profile: orders", titles)
        self.assertIn("Column Profile: sessions", titles)

    def test_a_stored_profile_reads_back_as_a_column_profile(self):
        self.fake.returns(pd.DataFrame({"status": ["A", "B"]}))
        self.engine.profile_tables(self.tid, ["orders"])
        got = self.engine.get_column_profiles(self.tid, "orders")
        self.assertEqual([p.column for p in got], ["status"])
        self.assertIsInstance(got[0], ColumnProfile)

    def test_a_table_with_no_profile_is_absent_not_empty(self):
        """Task 7 must fail closed on an unprofiled column, so 'no profile' has to
        be distinguishable from 'profiled as low-cardinality'."""
        self.assertEqual(self.engine.get_column_profiles(self.tid, "orders"), [])

    def test_second_call_skips_already_profiled_tables(self):
        self.fake.returns(pd.DataFrame({"a": [1]}))
        self.engine.profile_tables(self.tid, ["orders"])
        n = self.fake.call_count
        self.engine.profile_tables(self.tid, ["orders"])
        self.assertEqual(self.fake.call_count, n)          # cached
        self.engine.profile_tables(self.tid, ["orders"], force=True)
        self.assertGreater(self.fake.call_count, n)        # forced

    def test_reprofiling_updates_in_place_rather_than_duplicating(self):
        self.fake.returns(pd.DataFrame({"a": [1]}))
        self.engine.profile_tables(self.tid, ["orders"])
        self.engine.profile_tables(self.tid, ["orders"], force=True)
        nodes = [n for n in self.engine.brain(self.tid).all(kind=NodeKind.DEFINITION)
                 if n.title == "Column Profile: orders"]
        self.assertEqual(len(nodes), 1)

    def test_the_sample_goes_through_query_policy(self):
        """No raw executor.execute of an unvalidated f-string."""
        self.fake.returns(pd.DataFrame({"a": [1]}))
        self.engine.profile_tables(self.tid, ["orders"])
        self.assertTrue(any("LIMIT" in s.upper() for s in self.fake.sql_seen),
                        self.fake.sql_seen)

    def test_a_failing_table_is_reported_not_raised(self):
        class Broken(FakeExecutor):
            def execute(self, sql, ctx):
                self.call_count += 1
                return QueryResult(ok=False, error="ACCESS_DENIED")

        engine = JuniorEngine(self.ctx.stores, executor=Broken(),
                              tenants=self.ctx.tenants, settings=self.ctx.settings)
        self.assertEqual(engine.profile_tables(self.tid, ["orders"]), {"orders": []})


class TestRowCountEstimate(_ProfilerCase):
    """distinct_count and row_count_estimate feed Task 7's cell guard, not just
    prompts. An estimate of 50,000 on a 1.2M-row table would wave through a cube
    24x larger than the guard believes."""

    def test_the_real_row_count_is_used_when_it_can_be_obtained(self):
        self.fake.returns(pd.DataFrame({"a": list(range(10))}))
        self.fake.row_total = 1_200_000
        self.engine.profile_tables(self.tid, ["orders"])
        payload = self.engine.get_profile_payload(self.tid, "orders")
        self.assertEqual(payload["row_count_estimate"], 1_200_000)

    def test_the_sample_size_is_the_documented_fallback(self):
        class NoCount(FakeExecutor):
            def execute(self, sql, ctx):
                if sql.lower().lstrip().startswith("select count(*)"):
                    return QueryResult(ok=False, error="nope")
                return super().execute(sql, ctx)

        broken = NoCount()
        broken.returns(pd.DataFrame({"a": list(range(7))}))
        engine = JuniorEngine(self.ctx.stores, executor=broken,
                              tenants=self.ctx.tenants, settings=self.ctx.settings)
        engine.profile_tables(self.tid, ["orders"])
        payload = engine.get_profile_payload(self.tid, "orders")
        self.assertEqual(payload["row_count_estimate"], 7)
        self.assertTrue(payload["row_count_is_estimate"])


class TestFanout(_ProfilerCase):
    """The measurement that turns '5-7% of sessions span multiple service lines'
    from tribal knowledge into a number the planner can read."""

    def test_fanout_detects_a_multi_valued_categorical(self):
        """s1 touches two service lines, s2 and s3 touch one -> fan-out is 1/3."""
        self.fake.returns(pd.DataFrame({
            "session_id":   ["s1", "s1", "s2", "s3"],
            "service_line": ["mobile", "fixed", "mobile", "ott"],
        }))
        sl = self.column("service_line", table="events")
        self.assertAlmostEqual(sl.fanout_by_key["session_id"], 1 / 3, places=6)

    def test_a_clean_categorical_has_zero_fanout(self):
        self.fake.returns(pd.DataFrame({
            "session_id": ["s1", "s1", "s2"],
            "country":    ["DE", "DE", "IN"],
        }))
        self.assertEqual(self.column("country", table="events").fanout_by_key["session_id"], 0.0)

    def test_a_key_does_not_fan_out_against_itself(self):
        self.fake.returns(pd.DataFrame({
            "session_id": ["s1", "s1", "s2"],
            "country":    ["DE", "DE", "IN"],
        }))
        self.assertNotIn("session_id",
                         self.column("session_id", table="events").fanout_by_key)

    def test_a_high_cardinality_column_gets_no_fanout(self):
        """Fan-out on free text is noise."""
        self.fake.returns(pd.DataFrame({
            "session_id": [f"s{i}" for i in range(200)],
            "notes":      [f"note {i}" for i in range(200)],
        }))
        self.assertEqual(self.column("notes", table="events").fanout_by_key, {})


class TestCatalogDuplicateBug(unittest.TestCase):
    """5a -- refresh_catalog's `if cat_node:` and `else:` branches were byte
    identical, so every refresh appended another 'Database Catalog' node and
    get_catalog returned whichever all() yielded first."""

    def setUp(self):
        from analytics_platform.fixtures import build_retail_warehouse
        self.ctx = make_ctx(warehouse=build_retail_warehouse())
        self.tid = self.ctx.tenants.create_tenant("CatalogCo").id
        self.ctx.tenants.add_datasource(self.tid, "Events", DataSourceKind.DIRECT_DB,
                                        dialect="athena", tables=["events"])
        self.engine = JuniorEngine(self.ctx.stores, executor=self.ctx.executor,
                                   tenants=self.ctx.tenants)

    def tearDown(self):
        self.ctx.close()

    def test_refresh_catalog_updates_in_place_instead_of_duplicating(self):
        self.engine.refresh_catalog(self.tid)
        self.engine.refresh_catalog(self.tid)
        nodes = [n for n in self.engine.brain(self.tid).all(kind=NodeKind.DEFINITION)
                 if n.title == "Database Catalog"]
        self.assertEqual(len(nodes), 1)

    def test_the_surviving_node_carries_the_latest_payload(self):
        self.engine.refresh_catalog(self.tid)
        self.ctx.tenants.add_datasource(self.tid, "Orders", DataSourceKind.DIRECT_DB,
                                        dialect="athena", tables=["orders"])
        self.engine.refresh_catalog(self.tid)
        tables = {t["table"] for t in self.engine.get_catalog(self.tid)["tables"]}
        self.assertIn("orders", tables)


if __name__ == "__main__":
    unittest.main()


class TestASaturatedSampleCannotClaimCompleteness(unittest.TestCase):
    """The warehouse can return FEWER rows than the sample asked for and still
    say the result was cut short -- Metabase caps unaggregated queries at 2,000
    rows by default, whatever LIMIT we send.

    Counting rows alone cannot see that: 2,000 < 10,000 reads as "the table was
    smaller than the sample", which is the one interpretation under which a
    complete value list IS claimable. So a truncated sample that happens to be
    short would assert `values_complete=True` over a table it saw a millionth
    of, and those values go straight into the filter literals of generated SQL.
    """

    def setUp(self):
        self.ctx = make_ctx()
        self.tid = self.ctx.tenants.create_tenant("TruncCo").id
        self.fake = FakeExecutor()
        self.fake.returns(pd.DataFrame({"status": ["a", "b"], "n": [1, 2]}))
        self.fake.row_total = 628_358_776
        self.engine = JuniorEngine(self.ctx.stores, executor=self.fake,
                                   tenants=self.ctx.tenants, settings=self.ctx.settings)
        self.ctx.tenants.add_datasource(self.tid, "t", DataSourceKind.DIRECT_DB,
                                        dialect="athena", tables=["t"])

    def tearDown(self):
        self.ctx.close()

    def test_a_short_but_truncated_sample_forfeits_completeness(self):
        self.fake.truncate_sample = True
        profiles = self.engine.profile_tables(self.tid, ["t"], force=True)["t"]
        self.assertTrue(profiles, "expected profiles")
        for p in profiles:
            self.assertFalse(p.values_complete,
                             f"{p.column} claimed a complete value list from a "
                             f"truncated sample")

    def test_the_payload_records_that_the_sample_saturated(self):
        self.fake.truncate_sample = True
        self.engine.profile_tables(self.tid, ["t"], force=True)
        self.assertTrue(self.engine.get_profile_payload(self.tid, "t")["sample_saturated"])

    def test_an_untruncated_short_sample_still_claims_completeness(self):
        """The table really was smaller than the sample -- that IS complete."""
        profiles = self.engine.profile_tables(self.tid, ["t"], force=True)["t"]
        self.assertTrue(any(p.values_complete for p in profiles))
