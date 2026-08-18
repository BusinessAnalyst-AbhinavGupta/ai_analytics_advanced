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

    def test_a_full_cube_read_is_called_complete(self):
        ctx = StakeholderService._data_context(CUBE, frame_rows=5)
        self.assertIn("COMPLETE cube", ctx)
        self.assertIn("shares and totals are valid", ctx)

    def test_a_top_one_recut_is_called_a_subset(self):
        """The live failure: ORDER BY ... LIMIT 1 over a 5-row cube returned one
        row, and the answer was "the only service line present is 'NA', so it
        accounts for 100% of the sessions"."""
        ctx = StakeholderService._data_context([{"service_line": "NA"}], frame_rows=5)
        self.assertIn("SUBSET", ctx)
        self.assertIn("1 of the 5 rows", ctx)
        self.assertIn("do not compute a share", ctx)
        self.assertNotIn("COMPLETE", ctx)

    def test_an_unknown_frame_size_stays_cautious(self):
        """0 means "we could not size the cube" -- it must not read as complete."""
        ctx = StakeholderService._data_context([{"service_line": "NA"}], frame_rows=0)
        self.assertNotIn("COMPLETE", ctx)
        self.assertIn("do not conclude no other rows exist", ctx)

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


class TestCubeReadingRules(unittest.TestCase):
    """The workspace query has to keep the numbers the answer will quote. Live,
    a "largest share" turn wrote `SELECT service_line ... ORDER BY
    session_count / SUM(session_count) OVER () DESC LIMIT 1` -- it computed the
    share, ranked by it, then selected only the label, so the answer had a name
    and no number and could not state the share at all."""

    def _rules(self):
        return StakeholderService._cube_rules(["service_line"], [])

    def test_measures_ranked_by_must_be_selected(self):
        self.assertIn("including any you ranked or", self._rules())

    def test_small_cubes_come_back_whole(self):
        self.assertIn("whole and ordered", self._rules())

    def test_the_additive_case_is_still_stated(self):
        self.assertIn("Every measure in this cube is additive.", self._rules())

    def test_non_additive_measures_still_win_the_slot(self):
        rules = StakeholderService._cube_rules(["service_line"], ["uniques"])
        self.assertIn("NON-ADDITIVE", rules)
        self.assertIn("whole and ordered", rules)


class TestASliceOfASmallCubeIsShownWithTheWholeCube(unittest.TestCase):
    """The analysis step sometimes ranks by a measure and then selects only the
    label. Live that produced "I cannot determine which service line has the
    largest share" from a cube that held every figure needed. When the cube is
    small it is handed over whole, so the slice's omissions stop mattering."""

    SLICE = [{"service_line": "NA"}]

    def test_the_whole_cube_is_included(self):
        ctx = StakeholderService._data_context(self.SLICE, frame_rows=5, full_cube=CUBE)
        for row in CUBE:
            self.assertIn(str(row["sessions"]), ctx)

    def test_the_slice_is_still_identified_as_a_slice(self):
        ctx = StakeholderService._data_context(self.SLICE, frame_rows=5, full_cube=CUBE)
        self.assertIn("ranked or filtered slice", ctx)
        self.assertIn("COMPLETE cube", ctx)

    def test_a_cube_too_big_to_show_falls_back_to_the_subset_warning(self):
        """No full cube available -> the honest subset wording, not a claim that
        the slice is everything."""
        ctx = StakeholderService._data_context(self.SLICE, frame_rows=90000, full_cube=None)
        self.assertIn("SUBSET", ctx)
        self.assertIn("do not compute a share", ctx)

    def test_a_partial_cube_is_not_passed_off_as_complete(self):
        """full_cube must match frame_rows exactly; a short read is not the cube."""
        ctx = StakeholderService._data_context(self.SLICE, frame_rows=5, full_cube=CUBE[:3])
        self.assertIn("SUBSET", ctx)
        self.assertNotIn("COMPLETE cube", ctx)
