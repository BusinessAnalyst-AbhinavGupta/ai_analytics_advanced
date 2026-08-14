"""Deterministic static policy for LLM-synthesized Python cells. Runs BEFORE
any execution, same spirit as execution/policy.py's QueryPolicy for SQL: an
AST-inspection denylist pass, not a sandbox by itself -- the sandbox
(python_sandbox.py) still enforces CPU/memory/wall-clock limits as the real
containment. This policy exists so obviously out-of-bounds code (network,
filesystem, unrelated imports, introspection into dunder internals) is
rejected before it ever reaches the subprocess, with a reason fed back to
the LLM for its next attempt -- mirrors QueryPolicy's decision.denied retry
contract used by _synthesize_and_execute_sql / _synthesize_and_execute_python.
"""
from __future__ import annotations

import ast
from typing import List

from ..domain import PythonPolicyDecision

ALLOWED_IMPORTS = {"pandas", "numpy", "math", "statistics", "datetime", "collections", "re"}
DENIED_NAMES = {
    "eval", "exec", "compile", "__import__", "open", "input",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
}


def _has_result_assignment(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "result":
                    return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "result":
            return True
    return False


class _Visitor(ast.NodeVisitor):
    def __init__(self):
        self.reasons: List[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root not in ALLOWED_IMPORTS:
                self.reasons.append(f"import of '{alias.name}' is not allowed")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = (node.module or "").split(".")[0]
        if root not in ALLOWED_IMPORTS:
            self.reasons.append(f"import from '{node.module}' is not allowed")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in DENIED_NAMES:
            self.reasons.append(f"use of '{node.id}' is not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__"):
            self.reasons.append(f"access to dunder attribute '{node.attr}' is not allowed")
        self.generic_visit(node)


class PythonCodePolicy:
    def validate(self, code: str) -> PythonPolicyDecision:
        code = code.strip()
        if not code:
            return PythonPolicyDecision(allowed=False, reasons=["no code provided"])

        try:
            tree = ast.parse(code, mode="exec")
        except SyntaxError as exc:
            return PythonPolicyDecision(allowed=False, reasons=[f"syntax error: {exc}"])

        visitor = _Visitor()
        visitor.visit(tree)
        if visitor.reasons:
            return PythonPolicyDecision(allowed=False, reasons=visitor.reasons)

        if not _has_result_assignment(tree):
            return PythonPolicyDecision(
                allowed=False,
                reasons=["code must assign its final answer to a variable named 'result'"])

        return PythonPolicyDecision(allowed=True, approved_code=code)
