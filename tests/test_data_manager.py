"""Task 10 -- reuse / widen / retrieve, decided in code and never by an LLM.

"Does the workspace already contain August, Germany, over the checkout-sessions
population, cut by device?" is set containment with a right answer. The LLM's
job is to *state the requirement*; deciding whether it is met belongs here.
Every avoided warehouse query is an avoided round trip through a human's
browser tab.
"""
from __future__ import annotations

import tempfile
import unittest

import pandas as pd

from analytics_platform.config import Settings
from analytics_platform.data_manager import CoverageVerdict, DataManager, DataRequirement
from analytics_platform.execution.dataframe_cache import ConversationDataCache
from analytics_platform.execution.extract_store import ExtractMeta, ExtractStore
from analytics_platform.execution.workspace import AnalyticalWorkspace
from analytics_platform.domain import CubeMeasure

SUM_REV = CubeMeasure("revenue", "SUM(revenue)", additive=True)
DISTINCT_USERS = CubeMeasure("unique_users", "COUNT(DISTINCT user_id)", additive=False)
AVG_REV = CubeMeasure("revenue", "AVG(revenue)", additive=True,
                      read_expr="revenue_sum / NULLIF(revenue_count, 0)")


def _req(dimensions=("country",), measures=(SUM_REV,), pop="pop_A", **kw):
    return DataRequirement(base_view="checkout_sessions", population_hash=pop,
                           grain=kw.pop("grain", ["session_id"]),
                           dimensions=list(dimensions), measures=list(measures),
                           filters=kw.pop("filters", {}), **kw)


class _ManagerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExtractStore(self._tmp.name)
        self.cache = ConversationDataCache(store=self.store)
        self.workspace = AnalyticalWorkspace(self.store)
        self.dm = DataManager(self.cache, self.workspace, Settings())

    def tearDown(self):
        self.workspace.close_all()
        self._tmp.cleanup()

    def cached(self, label, pop="pop_A", dimensions=(), columns=(), grain=("session_id",),
               rows=10, truncated=False, non_additive=(), filters=None, time=None):
        meta = ExtractMeta(
            label=label, description="q", grain=list(grain), columns=list(columns),
            dtypes={c: "object" for c in columns}, row_count=rows, truncated=truncated,
            sql="SELECT 1", created_at="2026-08-15T00:00:00Z",
            base_view="checkout_sessions", population_hash=pop,
            dimensions=list(dimensions), non_additive=list(non_additive),
            filters=dict(filters or {}),
            time_column="date" if time else "",
            time_start=time[0] if time else "", time_end=time[1] if time else "")
        # A real frame of `rows` rows: describe() lets a hot frame's true row
        # count win over the sidecar, and selection ranks on that count.
        df = (pd.DataFrame({c: ["x"] * rows for c in columns}) if columns
              else pd.DataFrame({"n": list(range(rows))}))
        self.cache.put("acme", "c1", label, "q", df, meta=meta)
        return meta

    def assess(self, req):
        return self.dm.assess("acme", "c1", req)


class TestThePopulationGate(_ManagerCase):
    """Gate 0 -- the gate the whole design rests on."""

    def test_a_cube_over_a_different_population_is_never_reused(self):
        """Close-but-different is not reusable. Reusing it is exactly the silent
        cross-population comparison this design exists to prevent."""
        self.cached("df_1", pop="pop_B", dimensions=["country", "device"],
                    columns=["country", "device", "revenue"])
        v = self.assess(_req(pop="pop_A"))
        self.assertEqual(v.decision, "retrieve")
        self.assertEqual(v.label, "")
        self.assertIn("population", v.reason.lower())

    def test_the_same_population_with_a_different_projection_still_reuses(self):
        """Question B added a column at some point; the rows never changed."""
        self.cached("df_1", pop="pop_A", dimensions=["country", "device", "service_line"],
                    columns=["country", "device", "service_line", "revenue"])
        self.assertEqual(self.assess(_req(dimensions=["country"])).decision, "reuse")

    def test_an_empty_workspace_is_a_retrieve(self):
        v = self.assess(_req())
        self.assertEqual(v.decision, "retrieve")
        self.assertEqual(v.label, "")

    def test_a_requirement_with_no_population_never_reuses_anything(self):
        """The aggregate path has no population, so nothing is comparable to it."""
        self.cached("df_1", pop="pop_A", dimensions=["country"], columns=["country", "revenue"])
        self.assertEqual(self.assess(_req(pop="")).decision, "retrieve")


class TestContainment(_ManagerCase):
    """The original grain rule, moved up one level to cube dimensions."""

    def test_a_subset_of_the_cubes_dimensions_rolls_up(self):
        self.cached("df_1", dimensions=["country", "device", "date"],
                    columns=["country", "device", "date", "revenue"],
                    time=("2026-08-01", "2026-08-31"))
        v = self.assess(_req(dimensions=["country"], time_column="date",
                             time_start="2026-08-05", time_end="2026-08-10"))
        self.assertEqual(v.decision, "reuse")
        self.assertEqual(v.label, "df_1")

    def test_an_exact_dimension_match_reuses(self):
        self.cached("df_1", dimensions=["country"], columns=["country", "revenue"])
        self.assertEqual(self.assess(_req(dimensions=["country"])).decision, "reuse")

    def test_a_dimension_the_cube_does_not_carry_asks_to_widen(self):
        self.cached("df_1", dimensions=["country"], columns=["country", "revenue"])
        v = self.assess(_req(dimensions=["country", "device"]))
        self.assertEqual(v.decision, "widen")
        self.assertEqual(v.missing_dimensions, ["device"])
        self.assertEqual(v.supersedes, "df_1")

    def test_a_finer_stored_grain_is_still_reusable_for_a_keyset_extract(self):
        """The original grain rule survives unchanged for ID-grain extracts."""
        self.cached("df_1", grain=["session_id", "event_id"], dimensions=[],
                    columns=["session_id", "event_id", "revenue"])
        self.assertEqual(self.assess(_req(dimensions=[])).decision, "reuse")

    def test_a_coarser_stored_grain_is_never_reused(self):
        self.cached("df_1", grain=["country"], dimensions=[],
                    columns=["country", "revenue"])
        self.assertEqual(self.assess(_req(dimensions=[])).decision, "retrieve")


class TestAdditivity(_ManagerCase):
    """The rule that makes containment safe."""

    def test_a_distinct_count_cannot_be_rolled_up_to_fewer_dimensions(self):
        self.cached("df_1", dimensions=["country", "device"],
                    columns=["country", "device", "unique_users"],
                    non_additive=["unique_users"])
        v = self.assess(_req(dimensions=["country"], measures=[DISTINCT_USERS]))
        self.assertEqual(v.decision, "retrieve")
        self.assertIn("unique_users", v.reason)
        self.assertIn("distinct", v.reason.lower())

    def test_a_distinct_count_at_the_cubes_own_dimensions_is_fine(self):
        """No roll-up is happening, so non-additivity does not bite."""
        self.cached("df_1", dimensions=["country", "device"],
                    columns=["country", "device", "unique_users"],
                    non_additive=["unique_users"])
        v = self.assess(_req(dimensions=["country", "device"], measures=[DISTINCT_USERS]))
        self.assertEqual(v.decision, "reuse")

    def test_an_additive_measure_rolls_up_past_a_non_additive_sibling(self):
        """Only the measures the requirement actually asks for matter."""
        self.cached("df_1", dimensions=["country", "device"],
                    columns=["country", "device", "revenue", "unique_users"],
                    non_additive=["unique_users"])
        self.assertEqual(self.assess(_req(dimensions=["country"])).decision, "reuse")

    def test_an_averaged_measure_reuses_via_its_sum_and_count(self):
        self.cached("df_1", dimensions=["country", "device"],
                    columns=["country", "device", "revenue_sum", "revenue_count"])
        self.assertEqual(
            self.assess(_req(dimensions=["country"], measures=[AVG_REV])).decision, "reuse")

    def test_an_averaged_measure_missing_its_count_is_not_covered(self):
        self.cached("df_1", dimensions=["country"], columns=["country", "revenue_sum"])
        v = self.assess(_req(dimensions=["country"], measures=[AVG_REV]))
        self.assertEqual(v.decision, "widen")
        self.assertIn("revenue", v.missing_measures)


class TestMeasuresAndSlices(_ManagerCase):
    def test_a_measure_the_cube_lacks_asks_to_widen(self):
        self.cached("df_1", dimensions=["country"], columns=["country", "revenue"])
        v = self.assess(_req(measures=[CubeMeasure("orders", "COUNT(*)", True)]))
        self.assertEqual(v.decision, "widen")
        self.assertEqual(v.missing_measures, ["orders"])

    def test_a_wider_slice_than_the_cube_forces_a_retrieve(self):
        """Those rows were never fetched."""
        self.cached("df_1", dimensions=["country"], columns=["country", "revenue"],
                    filters={"country": ["Germany"]})
        self.assertEqual(self.assess(_req(filters={})).decision, "retrieve")

    def test_a_narrower_slice_reuses(self):
        self.cached("df_1", dimensions=["country"], columns=["country", "revenue"])
        self.assertEqual(
            self.assess(_req(filters={"country": ["Germany"]})).decision, "reuse")

    def test_an_equal_slice_reuses(self):
        self.cached("df_1", dimensions=["country"], columns=["country", "revenue"],
                    filters={"country": ["Germany"]})
        self.assertEqual(
            self.assess(_req(filters={"country": ["Germany"]})).decision, "reuse")

    def test_a_partly_overlapping_slice_is_not_covered(self):
        self.cached("df_1", dimensions=["country"], columns=["country", "revenue"],
                    filters={"country": ["Germany"]})
        self.assertEqual(
            self.assess(_req(filters={"country": ["Germany", "France"]})).decision, "retrieve")

    def test_a_filter_on_a_dimension_the_cube_lacks_is_a_miss(self):
        """You cannot filter on what you did not GROUP BY."""
        self.cached("df_1", dimensions=["country"], columns=["country", "revenue"])
        v = self.assess(_req(filters={"device": ["ios"]}))
        self.assertEqual(v.decision, "widen")
        self.assertIn("device", v.missing_dimensions)


class TestTime(_ManagerCase):
    def test_a_date_range_beyond_the_cube_names_the_missing_window(self):
        """Cells over disjoint date ranges are disjoint and additive, so a
        time-only gap IS fetchable in isolation -- hence widen, not retrieve.
        (The plan named this test '...asks_to_retrieve', but its own Task 12
        widen branch composes exactly this gap and UNION ALLs it on.)"""
        self.cached("df_1", dimensions=["date"], columns=["date", "revenue"],
                    time=("2026-08-01", "2026-08-31"))
        v = self.assess(_req(dimensions=["date"], time_column="date",
                             time_start="2026-07-01", time_end="2026-08-31"))
        self.assertEqual(v.missing_time_ranges, [("2026-07-01", "2026-07-31")])
        self.assertEqual(v.decision, "widen")

    def test_a_range_extending_past_the_end_is_also_a_gap(self):
        self.cached("df_1", dimensions=["date"], columns=["date", "revenue"],
                    time=("2026-08-01", "2026-08-31"))
        v = self.assess(_req(dimensions=["date"], time_column="date",
                             time_start="2026-08-01", time_end="2026-09-15"))
        self.assertEqual(v.missing_time_ranges, [("2026-09-01", "2026-09-15")])

    def test_a_covered_range_reuses(self):
        self.cached("df_1", dimensions=["date"], columns=["date", "revenue"],
                    time=("2026-08-01", "2026-08-31"))
        v = self.assess(_req(dimensions=["date"], time_column="date",
                             time_start="2026-08-05", time_end="2026-08-10"))
        self.assertEqual(v.decision, "reuse")

    def test_a_cube_with_no_recorded_range_cannot_answer_a_dated_question(self):
        self.cached("df_1", dimensions=["date"], columns=["date", "revenue"])
        v = self.assess(_req(dimensions=["date"], time_column="date",
                             time_start="2026-08-01", time_end="2026-08-31"))
        self.assertNotEqual(v.decision, "reuse")


class TestTruncation(_ManagerCase):
    def test_a_truncated_cube_is_not_reused_for_a_population_question(self):
        """Cells were dropped at the ceiling, so totals and rates over the whole
        population are wrong."""
        self.cached("df_1", dimensions=["country"], columns=["country", "revenue"],
                    truncated=True)
        self.assertEqual(self.assess(_req()).decision, "retrieve")

    def test_a_truncated_cube_is_reusable_for_a_strictly_narrower_slice(self):
        self.cached("df_1", dimensions=["country"], columns=["country", "revenue"],
                    truncated=True)
        v = self.assess(_req(filters={"country": ["Germany"]}))
        self.assertEqual(v.decision, "reuse")
        self.assertIn("truncat", v.reason.lower())


class TestSelection(_ManagerCase):
    def test_the_smallest_sufficient_cube_wins(self):
        self.cached("df_1", dimensions=["country", "device"],
                    columns=["country", "device", "revenue"], rows=90_000)
        self.cached("df_2", dimensions=["country", "device"],
                    columns=["country", "device", "revenue"], rows=4_000)
        self.assertEqual(self.assess(_req()).label, "df_2")

    def test_a_wider_cube_supersedes_and_is_preferred_afterwards(self):
        """Rule 8 beats rule 9: keeping answers on the widest available cube is
        what keeps later follow-ups local. They share a population, so both give
        the same answer anyway."""
        self.cached("df_1", dimensions=["country"], columns=["country", "revenue"], rows=30)
        self.cached("df_2", dimensions=["country", "device"],
                    columns=["country", "device", "revenue"], rows=120)
        v = self.assess(_req(dimensions=["country"]))
        self.assertEqual(v.decision, "reuse")
        self.assertEqual(v.label, "df_2")

    def test_the_reason_names_the_cube_and_why(self):
        self.cached("df_1", dimensions=["country"], columns=["country", "revenue"])
        self.assertIn("df_1", self.assess(_req()).reason)

    def test_the_widen_reason_names_what_is_missing(self):
        self.cached("df_1", dimensions=["country"], columns=["country", "revenue"])
        v = self.assess(_req(dimensions=["country", "service_line"]))
        self.assertIn("service_line", v.reason)

    def test_no_model_is_reachable_from_this_module(self):
        """The decision is set containment with a right answer, not a judgement,
        so nothing here may reach a model even by accident."""
        import ast
        import analytics_platform.data_manager as mod
        tree = ast.parse(open(mod.__file__).read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotEqual(node.func.attr, "generate")
        self.assertFalse([m for m in imported if "llm" in m or "client" in m], imported)


if __name__ == "__main__":
    unittest.main()
