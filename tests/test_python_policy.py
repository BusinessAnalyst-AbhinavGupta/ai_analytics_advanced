"""Tests for PythonCodePolicy -- the static, AST-based gate that runs BEFORE
any LLM-synthesized Python cell reaches the sandbox. Mirrors
execution/policy.py's QueryPolicy for SQL: not a sandbox by itself (the
sandbox's resource limits are the real containment), but rejects obviously
disallowed code up front with a reason fed back to the LLM for retry."""
from __future__ import annotations

import unittest

from analytics_platform.execution.python_policy import PythonCodePolicy


class TestPythonCodePolicy(unittest.TestCase):
    def setUp(self):
        self.policy = PythonCodePolicy()

    def test_valid_code_with_result_assignment_is_allowed(self):
        decision = self.policy.validate("result = df_1['amount'].sum()")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.approved_code, "result = df_1['amount'].sum()")

    def test_empty_code_is_denied(self):
        decision = self.policy.validate("")
        self.assertTrue(decision.denied)

    def test_syntax_error_is_denied(self):
        decision = self.policy.validate("result = (")
        self.assertTrue(decision.denied)
        self.assertIn("syntax error", decision.reasons[0].lower())

    def test_missing_result_assignment_is_denied(self):
        decision = self.policy.validate("x = df_1['amount'].sum()")
        self.assertTrue(decision.denied)
        self.assertTrue(any("result" in r for r in decision.reasons))

    def test_disallowed_import_is_denied(self):
        decision = self.policy.validate("import os\nresult = os.getcwd()")
        self.assertTrue(decision.denied)
        self.assertTrue(any("os" in r for r in decision.reasons))

    def test_allowed_import_is_permitted(self):
        decision = self.policy.validate("import numpy as np\nresult = np.mean([1, 2, 3])")
        self.assertTrue(decision.allowed)

    def test_import_from_disallowed_module_is_denied(self):
        decision = self.policy.validate("from subprocess import run\nresult = 1")
        self.assertTrue(decision.denied)

    def test_eval_call_is_denied(self):
        decision = self.policy.validate("result = eval('1+1')")
        self.assertTrue(decision.denied)
        self.assertTrue(any("eval" in r for r in decision.reasons))

    def test_open_call_is_denied(self):
        decision = self.policy.validate("f = open('/etc/passwd')\nresult = f.read()")
        self.assertTrue(decision.denied)

    def test_dunder_attribute_access_is_denied(self):
        decision = self.policy.validate("result = ().__class__.__bases__")
        self.assertTrue(decision.denied)

    def test_multiple_reasons_all_reported(self):
        decision = self.policy.validate("import os\nx = eval('1')")
        self.assertGreaterEqual(len(decision.reasons), 2)

    def test_builtins_eval_is_denied(self):
        """__builtins__.eval(...) is a critical escape vector and must be denied."""
        decision = self.policy.validate("result = __builtins__.eval('1+1')")
        self.assertTrue(decision.denied)
        self.assertTrue(any("__builtins__" in r for r in decision.reasons))

    def test_builtins_exec_is_denied(self):
        """__builtins__.exec(...) is a critical escape vector and must be denied."""
        decision = self.policy.validate("result = __builtins__.exec('x=1')")
        self.assertTrue(decision.denied)
        self.assertTrue(any("__builtins__" in r for r in decision.reasons))

    def test_builtins_open_is_denied(self):
        """__builtins__.open(...) is a critical escape vector and must be denied."""
        decision = self.policy.validate("result = __builtins__.open('/etc/passwd')")
        self.assertTrue(decision.denied)
        self.assertTrue(any("__builtins__" in r for r in decision.reasons))

    def test_builtins_subscript_eval_is_denied(self):
        """__builtins__['eval'](...) subscript form must be denied."""
        decision = self.policy.validate("result = __builtins__['eval']('1+1')")
        self.assertTrue(decision.denied)
        self.assertTrue(any("__builtins__" in r for r in decision.reasons))

    def test_result_nested_in_function_is_denied(self):
        """result assigned only inside a function def (not module-level) must be denied."""
        decision = self.policy.validate("def f():\n    result = 1\nx = 5")
        self.assertTrue(decision.denied)
        self.assertTrue(any("result" in r for r in decision.reasons))


if __name__ == "__main__":
    unittest.main()
