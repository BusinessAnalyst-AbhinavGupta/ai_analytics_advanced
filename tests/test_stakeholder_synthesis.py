"""The last step of the pipeline: turning a cube into a sentence.

Everything upstream works to produce a population-hashed, reconcilable cube.
If synthesis then narrates it from an arbitrary handful of rows, that care is
thrown away exactly where the user reads it -- and the sentence sounds just as
authoritative either way.
"""
from __future__ import annotations

import unittest

from analytics_platform.stakeholder import StakeholderService


CUBE = [{"service_line": "fmc", "sessions": 3},
        {"service_line": "fixed", "sessions": 4403},
        {"service_line": "NA", "sessions": 16724},
        {"service_line": "mobile", "sessions": 8009},
        {"service_line": "ott", "sessions": 2134}]


class TestASmallCubeArrivesWhole(unittest.TestCase):
    def test_every_row_is_present(self):
        """The live failure: a 5-row cube was cut to 3, and the answer dropped
        `mobile` (8,009 sessions, the second largest) while keeping `fmc` (3).
        The prose still read as a complete distribution."""
        ctx = StakeholderService._data_context(CUBE, ["service_line", "sessions"])
        for row in CUBE:
            self.assertIn(row["service_line"], ctx)
            self.assertIn(str(row["sessions"]), ctx)

    def test_it_says_the_result_is_complete(self):
        ctx = StakeholderService._data_context(CUBE)
        self.assertIn("COMPLETE", ctx)
        self.assertIn("all 5 row(s)", ctx)

    def test_complete_describes_the_query_not_the_population(self):
        """A workspace re-cut ending in ORDER BY ... LIMIT 1 returns one row
        completely. Told only "complete", the model answered that this was the
        only category present and therefore held 100% of sessions."""
        ctx = StakeholderService._data_context([{"service_line": "NA"}])
        self.assertIn("query that ran", ctx)
        self.assertIn("do not conclude that no other categories", ctx)
        self.assertIn("do not compute a share", ctx)

    def test_no_rows_is_no_context(self):
        self.assertEqual(StakeholderService._data_context([]), "")
        self.assertEqual(StakeholderService._data_context(None), "")


class TestACubeTooLargeToShow(unittest.TestCase):
    def setUp(self):
        self.big = [{"city": f"city_{i:05d}", "sessions": i} for i in range(4000)]

    def test_it_is_labelled_partial(self):
        ctx = StakeholderService._data_context(self.big)
        self.assertIn("PARTIAL", ctx)
        self.assertNotIn("COMPLETE", ctx)

    def test_it_states_the_true_row_count_and_what_is_missing(self):
        ctx = StakeholderService._data_context(self.big)
        self.assertIn("4000 rows", ctx)
        self.assertIn("are NOT", ctx)

    def test_it_keeps_the_largest_rows_not_the_first_ones(self):
        """Truncating in warehouse-emission order is what made `fmc` (3) survive
        while `mobile` (8,009) was dropped. Rank, then cut."""
        ctx = StakeholderService._data_context(self.big)
        self.assertIn("city_03999", ctx)      # the largest measure
        self.assertNotIn("city_00000", ctx)   # the smallest

    def test_it_stays_within_the_prompt_budget(self):
        ctx = StakeholderService._data_context(self.big)
        self.assertLessEqual(len(ctx), StakeholderService.SYNTHESIS_CONTEXT_CHARS * 1.5)


class TestRankingColumnChoice(unittest.TestCase):
    def test_the_first_all_numeric_column_wins(self):
        self.assertEqual(StakeholderService._measure_key(CUBE), "sessions")

    def test_a_column_that_is_numeric_only_sometimes_is_not_a_measure(self):
        rows = [{"a": 1, "b": 2}, {"a": "x", "b": 3}]
        self.assertEqual(StakeholderService._measure_key(rows), "b")

    def test_no_numeric_column_is_handled(self):
        rows = [{"a": "x"}, {"a": "y"}]
        self.assertEqual(StakeholderService._measure_key(rows), "")


class TestTheWorkspaceInventoryShownToThePlanner(unittest.TestCase):
    """Coverage matches measures by the name the cube stores them under. If the
    planner cannot see those names it invents new ones, every re-cut reads as a
    missing measure, and the promise that a follow-up costs no warehouse query
    quietly stops holding."""

    FRAMES = [{"label": "df_1", "description": "sessions by service line",
               "base_view": "checkout_sessions", "dimensions": ["service_line"],
               "columns": ["service_line", "checkout_sessions"],
               "row_count": 5, "truncated": False, "sample": []}]

    def _prompt(self):
        class Ctx:
            rendered = ""
        return StakeholderService._plan_prompt(
            StakeholderService, "which is largest?", Ctx(), self.FRAMES)

    def test_the_measure_names_are_listed(self):
        self.assertIn("measures=['checkout_sessions']", self._prompt())

    def test_dimensions_are_not_repeated_as_measures(self):
        self.assertNotIn("measures=['service_line'", self._prompt())

    def test_the_planner_is_told_to_reuse_the_exact_name(self):
        self.assertIn("name it EXACTLY as", self._prompt())
