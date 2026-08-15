"""Tests for run_python_sandboxed -- executes a policy-approved Python cell
in an isolated subprocess with CPU/memory/wall-clock limits, returning only
a capped summary of its `result` variable."""
from __future__ import annotations

import time
import unittest

import pandas as pd

from analytics_platform.execution.python_sandbox import run_python_sandboxed


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


if __name__ == "__main__":
    unittest.main()
