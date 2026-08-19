"""Tests for run_python_sandboxed -- executes a policy-approved Python cell
in an isolated subprocess with CPU/memory/wall-clock limits, returning only
a capped summary of its `result` variable."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

import pandas as pd

from analytics_platform.execution.python_sandbox import (
    EXTRACT_MEMORY_MB, EXTRACT_TIMEOUT_S, MAX_RESULT_CHARS, run_python_sandboxed)


class TestRunPythonSandboxed(unittest.TestCase):
    def test_scalar_result_is_returned(self):
        df = pd.DataFrame({"amount": [1, 2, 3]})
        res = run_python_sandboxed("result = int(df_1['amount'].sum())", {"df_1": df})
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.result_summary, 6)

    def test_dataframe_result_is_summarized_not_raw(self):
        df = pd.DataFrame({"amount": range(100)})
        res = run_python_sandboxed(
            "result = df_1.groupby(df_1['amount'] % 2).sum()", {"df_1": df})
        self.assertTrue(res.ok, res.error)
        self.assertIsInstance(res.result_summary, list)
        self.assertLessEqual(len(res.result_summary), 20)
        self.assertEqual(res.result_shape["rows"], 2)

    def test_print_output_is_captured_as_stdout(self):
        res = run_python_sandboxed("print('hello from sandbox')\nresult = 1", {})
        self.assertTrue(res.ok, res.error)
        self.assertIn("hello from sandbox", res.stdout)

    def test_runtime_exception_is_reported_not_raised(self):
        res = run_python_sandboxed("result = 1 / 0", {})
        self.assertFalse(res.ok)
        self.assertIn("ZeroDivisionError", res.error)

    def test_missing_result_variable_is_an_error(self):
        res = run_python_sandboxed("x = 1", {})
        self.assertFalse(res.ok)
        self.assertIn("result", res.error)

    def test_wall_clock_timeout_is_enforced(self):
        start = time.monotonic()
        res = run_python_sandboxed("while True:\n    pass\nresult = 1", {}, timeout_s=1.0)
        elapsed = time.monotonic() - start
        self.assertFalse(res.ok)
        self.assertIn("timeout", res.error.lower())
        self.assertLess(elapsed, 5.0)  # killed promptly, not left running

    def test_dataframe_passed_in_is_available_by_its_label(self):
        df = pd.DataFrame({"x": [10, 20]})
        res = run_python_sandboxed("result = list(df_1['x'])", {"df_1": df})
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.result_summary, [10, 20])

    def test_stdout_is_capped_on_error_path(self):
        # print() a large amount of output (as if the code had printed a raw
        # DataFrame) and then blow up -- the error path must cap stdout the
        # same way the success path does, not let it cross uncapped.
        res = run_python_sandboxed(
            "print('x' * 50000)\nresult = 1 / 0", {})
        self.assertFalse(res.ok)
        self.assertLessEqual(len(res.stdout), MAX_RESULT_CHARS)

    def test_error_is_capped_on_error_path(self):
        # A raised exception's message can carry raw DataFrame content (e.g.
        # `assert False, df.to_string()` -- the AST policy allows `assert`
        # since it needs no denied builtin name) and that message ends up in
        # traceback.format_exc(). The error path must cap it the same way the
        # stdout path already does, not let raw DataFrame content cross
        # uncapped into the repair-loop prompt or logs.
        df = pd.DataFrame({"amount": range(400), "note": ["x" * 20] * 400})
        res = run_python_sandboxed(
            "assert False, df_1.to_string()\nresult = 1", {"df_1": df})
        self.assertFalse(res.ok)
        self.assertLessEqual(len(res.error), MAX_RESULT_CHARS)

    def test_dataframe_result_with_wide_text_is_capped(self):
        long_text = "y" * 10000
        df = pd.DataFrame({"blob": [long_text] * 25})
        res = run_python_sandboxed("result = df_1", {"df_1": df})
        self.assertTrue(res.ok, res.error)
        serialized_len = len(json.dumps(res.result_summary)) \
            if not isinstance(res.result_summary, str) else len(res.result_summary)
        self.assertLess(serialized_len, MAX_RESULT_CHARS * 2)


class TestSandboxLoadsFromParquet(unittest.TestCase):
    """Task 3 -- at the 1,000,000-row ceiling, pickling every row through a pipe
    into a child with a 512MB RLIMIT_AS is both very slow and an immediate
    MemoryError. The child opens the Parquet file itself instead."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_dataframe_paths_are_loaded_in_the_child(self):
        p = self.tmp / "df_1.parquet"
        pd.DataFrame({"revenue": [1, 2, 3, 4]}).to_parquet(p, index=False)
        res = run_python_sandboxed("result = int(df_1['revenue'].sum())",
                                   dataframe_paths={"df_1": str(p)})
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.result_summary, 10)

    def test_unreadable_parquet_reports_an_error_not_a_crash(self):
        bad = self.tmp / "df_1.parquet"
        bad.write_text("not parquet")
        res = run_python_sandboxed("result = 1", dataframe_paths={"df_1": str(bad)})
        self.assertFalse(res.ok)
        self.assertIn("df_1", res.error)

    def test_a_missing_parquet_reports_an_error_not_a_crash(self):
        res = run_python_sandboxed("result = 1",
                                   dataframe_paths={"df_1": str(self.tmp / "gone.parquet")})
        self.assertFalse(res.ok)
        self.assertIn("df_1", res.error)

    def test_paths_and_inline_frames_can_be_mixed(self):
        p = self.tmp / "df_1.parquet"
        pd.DataFrame({"revenue": [5]}).to_parquet(p, index=False)
        res = run_python_sandboxed(
            "result = int(df_1['revenue'].sum() + df_2['revenue'].sum())",
            dataframes={"df_2": pd.DataFrame({"revenue": [7]})},
            dataframe_paths={"df_1": str(p)})
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.result_summary, 12)

    def test_dataframes_is_now_optional(self):
        """Every existing caller passes it positionally; the extract path does not."""
        res = run_python_sandboxed("result = 1 + 1")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.result_summary, 2)

    def test_the_result_cap_still_applies_on_the_parquet_path(self):
        """Only a summary crosses back, however the frame got into scope."""
        p = self.tmp / "df_1.parquet"
        pd.DataFrame({"n": range(500)}).to_parquet(p, index=False)
        res = run_python_sandboxed("result = df_1", dataframe_paths={"df_1": str(p)})
        self.assertTrue(res.ok, res.error)
        self.assertLessEqual(len(res.result_summary), 20)
        self.assertEqual(res.result_shape["rows"], 500)

    def test_the_extract_constants_are_larger_than_the_defaults(self):
        """A million-row cube does not fit in the 512MB default."""
        self.assertGreater(EXTRACT_MEMORY_MB, 512)
        self.assertGreaterEqual(EXTRACT_TIMEOUT_S, 30.0)


if __name__ == "__main__":
    unittest.main()
