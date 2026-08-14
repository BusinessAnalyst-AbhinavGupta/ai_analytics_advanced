# DataFrame Compute Engine & Python Repair Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the stakeholder chat a second compute path alongside SQL: once a
turn has fetched a DataFrame via synthesized SQL, later turns in the *same*
conversation can answer a follow-up by running LLM-written Python directly
against that already-fetched data instead of re-querying the warehouse —
with an automatic error-repair loop mirroring the existing SQL retry loop,
under a strict local-compute-only policy (raw data never leaves the process;
only column/dtype schemas and small, capped result summaries ever reach the
LLM or get persisted).

**Architecture:** A new in-memory `ConversationDataCache` holds DataFrames
fetched via SQL, keyed by `(tenant_id, conversation_id)`, evicted LRU so a
long-running process doesn't grow without bound. After each successful SQL
synthesis+execution in `StakeholderService.answer()`, the resulting
DataFrame is cached under a fresh label (`df_1`, `df_2`, ...). On a later
turn in the same conversation, before synthesizing fresh SQL, a small
LLM-driven routing step (`_choose_compute_path`, structured-JSON, same
pattern as `SkillEngine.extract_params`) decides whether the question can be
answered by Python over an already-cached DataFrame. If so, a
`_synthesize_and_execute_python` retry loop — structurally identical to
`_synthesize_and_execute_sql` — writes and runs pandas code. Every candidate
is statically checked by a new `PythonCodePolicy` (AST-based denylist:
imports, dunder access, `eval`/`exec`/`open`, mirrors `QueryPolicy`'s shape
and its "reason fed back to the LLM on rejection" contract) before it's ever
executed, and then actually runs inside a fresh (`spawn`) subprocess with
CPU-time and address-space `resource` limits and a wall-clock timeout —
`run_python_sandboxed` in a new `execution/python_sandbox.py` module. The
subprocess returns only a capped, truncated summary of a `result` variable
the code must assign (never the full raw DataFrame) back to the parent. Any
failure at any stage (routing declines, policy rejects every attempt, the
sandbox errors or times out on every attempt) falls through to the existing,
already-shipped SQL path unchanged — this plan is strictly additive on top
of B1's `_synthesize_and_execute_sql`, never a replacement for it.

**Tech Stack:** Python 3.14 / FastAPI / SQLite / pandas / numpy (all
existing — `requirements-advanced.txt` already pins `pandas==3.0.5`,
`numpy==2.5.1`). Python's stdlib `multiprocessing` + `resource` modules for
sandboxing (no new dependency — `resource` is POSIX-only, which is fine:
this repo's `execution/browser_session.py` already shells out via
`subprocess`/AppleScript, i.e. is already macOS/POSIX-only in practice).
Next.js 15 / React / Zustand / TypeScript on the frontend, reusing the
`CollapsibleCode` component B1 built generically for exactly this reuse.

## Global Constraints

- No feature branches — every task commits directly to `main`.
- **Local-compute-only, for real, not just in the docstring:** a raw
  DataFrame (or any row-level slice of one) must never be serialized into an
  LLM prompt or into a persisted column. Only three things may cross that
  boundary: (a) a DataFrame's **schema** (`columns`, `dtypes`, `row_count` —
  no data) when prompting the routing/code-synthesis LLM calls, (b) a
  **capped, truncated summary** of a Python cell's `result` variable
  (`MAX_RESULT_ROWS` rows / `MAX_RESULT_CHARS` chars, defined in
  `python_sandbox.py`) after execution, and (c) that same capped summary
  when persisted into the new `python_cells` column. If a task's diff would
  put a full DataFrame or an unbounded slice into a prompt, an INSERT, or an
  API response, that is a Global Constraint violation, not a style nit.
- **All LLM-synthesized Python code executes inside `run_python_sandboxed`'s
  subprocess. Never call `exec()`/`eval()` on synthesized code anywhere else
  in the codebase** — not for a "quick check", not in a test helper that
  then gets copy-pasted into real code later.
- Every tenant's data lives in its own SQLite file at `tenants/<id>/tenant.db`;
  every new query scopes by `tenant_id` (defence-in-depth; the file boundary
  is the real isolation). The in-memory `ConversationDataCache` is process-wide
  (not per-tenant-file) precisely because it never touches disk — it MUST key
  every entry by `(tenant_id, conversation_id)` so one tenant's Python cell can
  never read another tenant's cached DataFrame, even though they share the
  same Python process and cache object.
- New columns on an existing table: a non-destructive
  `ALTER TABLE ... ADD COLUMN` inside `_migrate()` in
  `analytics_platform/database.py`, following the existing `conversation_id`
  column as the template (`database.py:219-224`) — note `conversation_id`
  was added via `_migrate()` alone, with no corresponding change to the
  `CREATE TABLE IF NOT EXISTS stakeholder_answers` text; `_migrate()` runs
  after every `init_db()` call including on a freshly created database, so
  this is sufficient on its own. Follow that exact pattern for the new
  `python_cells` column — do not also edit the `CREATE TABLE` text.
- No silent failures: if compute-path routing errors, if every Python
  synthesis attempt is policy-rejected, or if every sandboxed execution
  attempt fails/times out, `answer()` must fall through to the existing SQL
  path (or its own existing fallbacks) exactly as if the Python path had
  never been attempted — never raise, never return a bare traceback as the
  "answer", never silently return an empty/wrong answer.
- Backend API base URL is hardcoded as `http://localhost:8000` in the
  frontend (existing convention) — this plan adds no new routes and no new
  fetches, so this doesn't come up, but if it did, follow the convention.
- Frontend styling follows the existing convention exactly: inline
  `style={{...}}` objects using the CSS custom properties already defined in
  `globals.css` — no CSS-in-JS library, no Tailwind, no new global
  stylesheet, no new frontend dependency.
- Run backend tests with `.venv/bin/python -m pytest tests/ -q` from the
  repo root (NOT bare `pytest`).
- Every task is TDD: failing test, verify the failure, minimal
  implementation, verify the pass, commit.

---

## File Structure

- **Create** `analytics_platform/execution/dataframe_cache.py` —
  `CachedFrame` + `ConversationDataCache` (LRU-bounded, tenant/conversation-scoped,
  in-memory only).
- **Create** `analytics_platform/execution/python_policy.py` —
  `PythonCodePolicy`, an AST-based static check mirroring
  `execution/policy.py`'s `QueryPolicy`.
- **Create** `analytics_platform/execution/python_sandbox.py` —
  `PythonExecResult` + `run_python_sandboxed`, the actual subprocess
  sandbox with CPU/memory/wall-clock limits and result summarization.
- **Modify** `analytics_platform/domain.py` — add `PythonPolicyDecision`
  dataclass (mirrors `PolicyDecision`, `approved_code` instead of
  `approved_sql`).
- **Modify** `analytics_platform/database.py` — add the `python_cells`
  column migration to `_migrate()`.
- **Modify** `analytics_platform/stakeholder.py` — `ConversationDataCache`
  wiring, `_choose_compute_path`, `_synthesize_python`,
  `_synthesize_and_execute_python`, the routing branch in `answer()`,
  `_record()`/`get_conversation()` threading for `python_cells`.
- **Modify** `tests/test_stakeholder.py` — cache wiring, routing, repair-loop,
  and end-to-end multi-turn tests.
- **Create** `tests/test_dataframe_cache.py`,
  `tests/test_python_policy.py`, `tests/test_python_sandbox.py`.
- **Modify** `frontend/src/store/useStore.ts` — `StakeholderMessage.python_cells`
  field.
- **Modify** `frontend/src/components/StakeholderChat.tsx` — render
  `m.python_cells` via the existing `CollapsibleCode` component.

---

### Task 1: `ConversationDataCache`

**Files:**
- Create: `analytics_platform/execution/dataframe_cache.py`
- Test: `tests/test_dataframe_cache.py`

**Interfaces:**
- Produces: `CachedFrame` (dataclass: `label: str, description: str, df: pd.DataFrame`,
  method `describe() -> Dict[str, Any]` returning `{label, description, columns,
  dtypes, row_count}` — no row data); `ConversationDataCache` with methods
  `put(tenant_id, conversation_id, label, description, df) -> None`,
  `get(tenant_id, conversation_id, label) -> Optional[pd.DataFrame]`,
  `list_available(tenant_id, conversation_id) -> List[Dict[str, Any]]` (schemas
  only), `next_label(tenant_id, conversation_id) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_dataframe_cache.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'analytics_platform.execution.dataframe_cache'`.

- [ ] **Step 3: Implement `ConversationDataCache`**

```python
"""In-process, per-conversation DataFrame cache for the Python compute path.

Holds already-fetched query results in memory so a follow-up turn in the
same conversation can run Python against them instead of re-running SQL.
Never persisted to disk. Only `describe()`'s schema (columns/dtypes/row
count -- never row data) is meant to leave this module, e.g. into an LLM
prompt; callers must not serialize a DataFrame straight out of `get()` into
anything that reaches the LLM or a persisted column. Tenant- and
conversation-scoped by construction (every method takes both ids), and
LRU-bounded in both dimensions so a long-running process doesn't grow
without limit.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


@dataclass
class CachedFrame:
    label: str
    description: str
    df: pd.DataFrame

    def describe(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "description": self.description,
            "columns": list(self.df.columns),
            "dtypes": {c: str(t) for c, t in self.df.dtypes.items()},
            "row_count": len(self.df),
        }


class ConversationDataCache:
    def __init__(self, max_conversations: int = 50, max_frames_per_conversation: int = 5):
        self.max_conversations = max_conversations
        self.max_frames_per_conversation = max_frames_per_conversation
        self._data: "OrderedDict[Tuple[str, str], OrderedDict[str, CachedFrame]]" = OrderedDict()

    def _key(self, tenant_id: str, conversation_id: str) -> Tuple[str, str]:
        return (tenant_id, conversation_id)

    def put(self, tenant_id: str, conversation_id: str, label: str,
            description: str, df: pd.DataFrame) -> None:
        key = self._key(tenant_id, conversation_id)
        if key in self._data:
            self._data.move_to_end(key)
        frames = self._data.setdefault(key, OrderedDict())
        frames[label] = CachedFrame(label=label, description=description, df=df)
        frames.move_to_end(label)
        while len(frames) > self.max_frames_per_conversation:
            frames.popitem(last=False)
        while len(self._data) > self.max_conversations:
            self._data.popitem(last=False)

    def get(self, tenant_id: str, conversation_id: str, label: str) -> Optional[pd.DataFrame]:
        key = self._key(tenant_id, conversation_id)
        frames = self._data.get(key)
        if not frames or label not in frames:
            return None
        self._data.move_to_end(key)
        frames.move_to_end(label)
        return frames[label].df

    def list_available(self, tenant_id: str, conversation_id: str) -> List[Dict[str, Any]]:
        frames = self._data.get(self._key(tenant_id, conversation_id))
        if not frames:
            return []
        return [f.describe() for f in frames.values()]

    def next_label(self, tenant_id: str, conversation_id: str) -> str:
        frames = self._data.get(self._key(tenant_id, conversation_id))
        existing = set(frames.keys()) if frames else set()
        n = 1
        while f"df_{n}" in existing:
            n += 1
        return f"df_{n}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_dataframe_cache.py -v`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/execution/dataframe_cache.py tests/test_dataframe_cache.py
git commit -m "feat(execution): add ConversationDataCache for in-process DataFrame reuse"
```

---

### Task 2: `PythonPolicyDecision` + `PythonCodePolicy`

**Files:**
- Modify: `analytics_platform/domain.py`
- Create: `analytics_platform/execution/python_policy.py`
- Test: `tests/test_python_policy.py`

**Interfaces:**
- Produces: `PythonPolicyDecision` (dataclass: `allowed: bool, reasons: List[str],
  approved_code: str`, property `denied`) in `domain.py`, next to the existing
  `PolicyDecision`. `PythonCodePolicy.validate(code: str) -> PythonPolicyDecision`.

- [ ] **Step 1: Add `PythonPolicyDecision` to domain.py**

In `analytics_platform/domain.py`, immediately after the existing
`PolicyDecision` class (the one with `allowed`, `reasons`, `approved_sql`,
`denied`), add:

```python
@dataclass
class PythonPolicyDecision:
    allowed: bool
    reasons: List[str] = field(default_factory=list)
    approved_code: str = ""

    @property
    def denied(self) -> bool:
        return not self.allowed
```

(`dataclass` and `field` are already imported in `domain.py` — `PolicyDecision`
uses them immediately above.)

- [ ] **Step 2: Write the failing tests**

```python
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_python_policy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'analytics_platform.execution.python_policy'`.

- [ ] **Step 4: Implement `PythonCodePolicy`**

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_python_policy.py -v`
Expected: PASS (11 tests).

- [ ] **Step 6: Commit**

```bash
git add analytics_platform/domain.py analytics_platform/execution/python_policy.py tests/test_python_policy.py
git commit -m "feat(execution): add PythonCodePolicy, an AST-based static gate for synthesized Python"
```

---

### Task 3: `run_python_sandboxed`

**Files:**
- Create: `analytics_platform/execution/python_sandbox.py`
- Test: `tests/test_python_sandbox.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (standalone; `PythonCodePolicy` validates
  code before this is called, but this module doesn't import it).
- Produces: `PythonExecResult` (dataclass: `ok: bool, result_summary: Any,
  result_shape: Optional[Dict[str,int]], stdout: str, error: str,
  execution_ms: float`); `run_python_sandboxed(code: str, dataframes:
  Dict[str, pd.DataFrame], timeout_s: float = 10.0, memory_mb: int = 512) ->
  PythonExecResult`. Each key in `dataframes` becomes a top-level variable
  name inside the executed code's scope (e.g. `dataframes={"df_1": df}` makes
  `df_1` available in the code).

**Known, accepted test gap (state this, don't silently skip it):** the
`memory_mb` rlimit is real defense-in-depth (see Step 3's `resource.setrlimit`
call) but is not covered by a dedicated unit test here — reliably triggering
an `RLIMIT_AS` violation from a test needs an allocation large enough to be
slow/flaky across CI environments, and `resource.setrlimit` runs inside a
spawned child process, which cross-process mocking can't intercept
meaningfully. The wall-clock timeout test below exercises the same
"runaway/misbehaving code gets forcibly stopped" containment property and is
fast and deterministic; treat it as the primary proof of containment.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_python_sandbox.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'analytics_platform.execution.python_sandbox'`.

- [ ] **Step 3: Implement `run_python_sandboxed`**

```python
"""Executes policy-approved Python cells in an isolated subprocess with
CPU/memory/wall-clock limits, returning only a small, JSON-safe summary of
`result` -- never the full DataFrame -- back to the caller. This is the
"local-compute-only" boundary in practice: the subprocess runs on the same
machine (nothing crosses the network), but its output crossing back into the
main process -- and from there, potentially into an LLM prompt or persisted
column -- is capped and summarized right here so nothing downstream has to
remember to truncate it.

Uses a fresh ("spawn") process per execution rather than fork, so resource
limits set inside the child don't inherit state from the parent's
already-running interpreter.
"""
from __future__ import annotations

import builtins
import contextlib
import io
import multiprocessing as mp
import time
import traceback
from dataclasses import dataclass
from typing import Any, Dict, Optional

import pandas as pd

DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MEMORY_MB = 512
MAX_RESULT_ROWS = 20
MAX_RESULT_CHARS = 4000

_ALLOWED_BUILTIN_NAMES = (
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int",
    "len", "list", "map", "max", "min", "range", "round", "set", "sorted", "str",
    "sum", "tuple", "zip", "print",
)
_SAFE_BUILTINS = {name: getattr(builtins, name) for name in _ALLOWED_BUILTIN_NAMES}


@dataclass
class PythonExecResult:
    ok: bool
    result_summary: Any = None
    result_shape: Optional[Dict[str, int]] = None
    stdout: str = ""
    error: str = ""
    execution_ms: float = 0.0


def _worker(code: str, dataframes: Dict[str, pd.DataFrame], memory_mb: int,
            timeout_s: float, conn) -> None:
    try:
        import resource
        cpu_limit = int(timeout_s) + 5
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
        resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024 * 1024, memory_mb * 1024 * 1024))
    except (ImportError, ValueError, OSError):
        pass  # best-effort: not all platforms support these rlimits

    scope: Dict[str, Any] = {"pd": pd, **dataframes}
    stdout_buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout_buf):
            exec(compile(code, "<python_cell>", "exec"), {"__builtins__": _SAFE_BUILTINS}, scope)
        if "result" not in scope:
            conn.send(("error", "code did not assign a 'result' variable", stdout_buf.getvalue()))
            return
        conn.send(("ok", scope["result"], stdout_buf.getvalue()))
    except Exception:
        conn.send(("error", traceback.format_exc(limit=3), stdout_buf.getvalue()))
    finally:
        conn.close()


def _summarize(value: Any):
    if isinstance(value, pd.DataFrame):
        head = value.head(MAX_RESULT_ROWS)
        return head.to_dict(orient="records"), {"rows": len(value), "columns": len(value.columns)}
    if isinstance(value, pd.Series):
        head = value.head(MAX_RESULT_ROWS)
        return head.to_dict(), {"rows": len(value), "columns": 1}
    try:
        import json
        text = json.dumps(value)
        if len(text) > MAX_RESULT_CHARS:
            return text[:MAX_RESULT_CHARS] + "...(truncated)", None
        return value, None
    except TypeError:
        return str(value)[:MAX_RESULT_CHARS], None


def run_python_sandboxed(code: str, dataframes: Dict[str, pd.DataFrame],
                          timeout_s: float = DEFAULT_TIMEOUT_S,
                          memory_mb: int = DEFAULT_MEMORY_MB) -> PythonExecResult:
    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_worker, args=(code, dataframes, memory_mb, timeout_s, child_conn))
    start = time.monotonic()
    proc.start()
    child_conn.close()  # only the child writes; parent must not hold this open

    if not parent_conn.poll(timeout_s):
        proc.terminate()
        proc.join(2)
        if proc.is_alive():
            proc.kill()
        return PythonExecResult(ok=False, error=f"execution exceeded {timeout_s}s timeout",
                                execution_ms=(time.monotonic() - start) * 1000)

    try:
        status, payload, stdout = parent_conn.recv()
    except EOFError:
        proc.join(2)
        return PythonExecResult(
            ok=False,
            error="sandboxed process terminated unexpectedly (likely a memory limit or crash)",
            execution_ms=(time.monotonic() - start) * 1000)

    proc.join(2)
    elapsed_ms = (time.monotonic() - start) * 1000

    if status == "error":
        return PythonExecResult(ok=False, error=payload, stdout=stdout, execution_ms=elapsed_ms)

    summary, shape = _summarize(payload)
    return PythonExecResult(ok=True, result_summary=summary, result_shape=shape,
                            stdout=stdout[-MAX_RESULT_CHARS:], execution_ms=elapsed_ms)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_python_sandbox.py -v`
Expected: PASS (7 tests). The timeout test takes slightly over 1 second
(the enforced timeout) — that's expected, not a hang.

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/execution/python_sandbox.py tests/test_python_sandbox.py
git commit -m "feat(execution): add run_python_sandboxed, a resource-limited subprocess sandbox"
```

---

### Task 4: Wire `ConversationDataCache` into `StakeholderService`

**Files:**
- Modify: `analytics_platform/stakeholder.py`
- Test: `tests/test_stakeholder.py`

**Interfaces:**
- Consumes: `ConversationDataCache` (Task 1).
- Produces: `StakeholderService.data_cache: ConversationDataCache` — later
  tasks (5, 6, 7) read and write it.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_stakeholder.py` (follow the existing `TestStakeholder`
class's `setUp` pattern — `self.ctx, self.base = app_ctx(...)`, mocked LLM
via `@patch("analytics_platform.stakeholder.make_role_client")` with
`mock_llm.generate.side_effect` supplying one `MagicMock(text=..., tokens_in=...,
tokens_out=...)` per expected `generate()` call in order: intent extraction,
then SQL synthesis, then answer synthesis, exactly as the existing
`test_sql_synthesis_repairs_after_policy_rejection`-style tests already do):

```python
    @patch("analytics_platform.stakeholder.make_role_client")
    def test_successful_sql_synthesis_caches_the_resulting_dataframe(self, mock_make_client):
        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [
            MagicMock(text='{"category": "metric_lookup"}', tokens_in=10, tokens_out=5),
            MagicMock(text="```sql\nSELECT * FROM orders LIMIT 10\n```", tokens_in=20, tokens_out=10),
            MagicMock(text='{"answer": "here you go"}', tokens_in=15, tokens_out=8),
        ]
        mock_make_client.return_value = mock_llm
        self.ctx.tenants.set_analyst_config(
            self.tid, {"stakeholder": {"enabled": True, "provider": "mock", "model": "mock"}})

        res = self.ctx.stakeholder.answer(self.tid, "how many orders", conversation_id="")

        available = self.ctx.stakeholder.data_cache.list_available(self.tid, res["conversation_id"])
        self.assertEqual(len(available), 1)
        self.assertEqual(available[0]["label"], "df_1")
```

(Adjust the exact `mock_llm.generate.side_effect` payloads/order to match
whatever the existing tests in this file already use for a plain successful
synthesized-SQL turn — copy that pattern verbatim rather than guessing at
the intent-classification response shape; the point of this test is only the
cache side-effect, not re-proving the SQL path works.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -k caches_the_resulting_dataframe -v`
Expected: FAIL with `AttributeError: 'StakeholderService' object has no attribute 'data_cache'`.

- [ ] **Step 3: Wire the cache**

In `analytics_platform/stakeholder.py`, add the import near the top (with the
other `.execution.*` imports):

```python
from .execution.dataframe_cache import ConversationDataCache
```

In `StakeholderService.__init__` (`stakeholder.py:48-60`), add after
`self.executor = executor or SamplerExecutor()`:

```python
        self.data_cache = ConversationDataCache()
```

In `answer()`, inside the `if exec_res is not None and exec_res.ok:` block
(`stakeholder.py:283`), right before the `preview = []` line, add:

```python
                if exec_res.data is not None and conversation_id:
                    label = self.data_cache.next_label(tenant_id, conversation_id)
                    self.data_cache.put(tenant_id, conversation_id, label, question[:200], exec_res.data)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -k caches_the_resulting_dataframe -v`
Expected: PASS.

- [ ] **Step 5: Run the full backend suite to confirm no regressions**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all previously-passing tests still pass (this change is additive —
it only adds a cache write after an already-successful path, it doesn't
change any return value or control flow).

- [ ] **Step 6: Commit**

```bash
git add analytics_platform/stakeholder.py tests/test_stakeholder.py
git commit -m "feat(stakeholder): cache successful SQL results into ConversationDataCache"
```

---

### Task 5: `_choose_compute_path`

**Files:**
- Modify: `analytics_platform/stakeholder.py`
- Test: `tests/test_stakeholder.py`

**Interfaces:**
- Consumes: `self.data_cache` (Task 4).
- Produces: `StakeholderService._choose_compute_path(llm, tenant_id,
  conversation_id, question) -> Tuple[str, str]` — `(path, df_label)` where
  `path` is `"python"` or `"sql"`, `df_label` is `""` when `path == "sql"`.
  Consumed by Task 7's routing branch in `answer()`.

- [ ] **Step 1: Write the failing tests**

```python
    def test_choose_compute_path_defaults_to_sql_when_nothing_cached(self):
        mock_llm = MagicMock()
        path, label = self.ctx.stakeholder._choose_compute_path(
            mock_llm, self.tid, "no-such-conversation", "how many orders")
        self.assertEqual(path, "sql")
        self.assertEqual(label, "")
        mock_llm.generate.assert_not_called()  # no point asking if nothing's cached

    def test_choose_compute_path_returns_python_when_llm_says_so_and_label_exists(self):
        import pandas as pd
        self.ctx.stakeholder.data_cache.put(
            self.tid, "conv-1", "df_1", "orders by month", pd.DataFrame({"a": [1, 2]}))
        mock_llm = MagicMock()
        mock_llm.generate.return_value = MagicMock(
            text='{"path": "python", "df_label": "df_1"}', tokens_in=5, tokens_out=5)

        path, label = self.ctx.stakeholder._choose_compute_path(
            mock_llm, self.tid, "conv-1", "what's the total")

        self.assertEqual(path, "python")
        self.assertEqual(label, "df_1")

    def test_choose_compute_path_falls_back_to_sql_on_unknown_label(self):
        import pandas as pd
        self.ctx.stakeholder.data_cache.put(
            self.tid, "conv-1", "df_1", "orders by month", pd.DataFrame({"a": [1, 2]}))
        mock_llm = MagicMock()
        mock_llm.generate.return_value = MagicMock(
            text='{"path": "python", "df_label": "df_does_not_exist"}', tokens_in=5, tokens_out=5)

        path, label = self.ctx.stakeholder._choose_compute_path(
            mock_llm, self.tid, "conv-1", "what's the total")

        self.assertEqual(path, "sql")
        self.assertEqual(label, "")

    def test_choose_compute_path_falls_back_to_sql_on_malformed_llm_response(self):
        import pandas as pd
        self.ctx.stakeholder.data_cache.put(
            self.tid, "conv-1", "df_1", "orders by month", pd.DataFrame({"a": [1, 2]}))
        mock_llm = MagicMock()
        mock_llm.generate.return_value = MagicMock(text="not json at all", tokens_in=0, tokens_out=0)

        path, label = self.ctx.stakeholder._choose_compute_path(
            mock_llm, self.tid, "conv-1", "what's the total")

        self.assertEqual(path, "sql")
        self.assertEqual(label, "")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -k choose_compute_path -v`
Expected: FAIL with `AttributeError: 'StakeholderService' object has no attribute '_choose_compute_path'`.

- [ ] **Step 3: Implement `_choose_compute_path`**

In `analytics_platform/stakeholder.py`, add this method near
`_synthesize_sql` (e.g. immediately before it):

```python
    def _choose_compute_path(self, llm: Any, tenant_id: str, conversation_id: str,
                             question: str) -> Tuple[str, str]:
        """Decide whether this turn should re-run SQL or run Python against an
        already-cached DataFrame from earlier in the same conversation.
        Returns (path, df_label) where path is "python" or "sql"; df_label is
        "" when path == "sql". Defaults to "sql" (the existing, well-tested
        path) whenever nothing is cached, the LLM response doesn't parse, or
        it names a label that isn't actually cached -- Python-over-cache is
        only ever an optimization layered on the SQL path, never a
        replacement for it.
        """
        available = self.data_cache.list_available(tenant_id, conversation_id)
        if not available:
            return "sql", ""

        frames_desc = "\n".join(
            f"- {f['label']}: {f['description']} (columns: {f['columns']})" for f in available)
        sys_prompt = (
            "You are deciding how to answer a follow-up analytics question. "
            "You must respond with a strict JSON object: "
            '{"path": "python"|"sql", "df_label": "the label to use, or empty string"}. '
            "Choose \"python\" ONLY if the question can be fully answered by computing "
            "over one of the DataFrames already available below (e.g. a follow-up "
            "aggregation, filter, or reshape of data already fetched this conversation). "
            "Choose \"sql\" if the question needs data that isn't in any available DataFrame."
        )
        prompt = f"Question: {question}\n\nAvailable DataFrames this conversation:\n{frames_desc}"
        try:
            res = llm.generate(prompt=prompt, system_prompt=sys_prompt, temperature=0.0)
            text = (res.text or "").strip() if res and hasattr(res, "text") else ""
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].strip()
            parsed = json.loads(text)
            path = parsed.get("path", "sql")
            df_label = parsed.get("df_label") or ""
            if path == "python" and df_label in {f["label"] for f in available}:
                return "python", df_label
            return "sql", ""
        except Exception as exc:  # noqa: BLE001 - routing is best-effort, default to the proven path
            logger.warning("compute-path routing failed for question %r: %s", question, exc)
            return "sql", ""
```

`json` is already imported in `stakeholder.py` (used by `_synthesize`) — no
new import needed. `Tuple` is already imported from `typing`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -k choose_compute_path -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/stakeholder.py tests/test_stakeholder.py
git commit -m "feat(stakeholder): add _choose_compute_path routing between SQL and cached-DataFrame Python"
```

---

### Task 6: `_synthesize_python` + `_synthesize_and_execute_python` repair loop

**Files:**
- Modify: `analytics_platform/stakeholder.py`
- Test: `tests/test_stakeholder.py`

**Interfaces:**
- Consumes: `PythonCodePolicy` (Task 2), `run_python_sandboxed` (Task 3),
  `self.data_cache` (Task 4).
- Produces: `StakeholderService._synthesize_and_execute_python(llm, tenant_id,
  conversation_id, question, df_label, max_attempts=3) -> Tuple[str,
  Optional[PythonExecResult], Tuple[int,int]]` — mirrors
  `_synthesize_and_execute_sql`'s return shape exactly (code-or-empty,
  result-or-None, total tokens). Consumed by Task 7's routing branch.

- [ ] **Step 1: Write the failing tests**

Mirror the existing three SQL repair-loop tests
(`test_sql_synthesis_repairs_after_policy_rejection`,
`test_sql_synthesis_repairs_after_execution_failure`,
`test_sql_synthesis_stops_after_max_attempts...` — read these in
`tests/test_stakeholder.py` first for the exact assertion style on
`mock_llm.generate.call_args_list[N].kwargs["prompt"]`) with Python
equivalents:

```python
    def test_python_synthesis_repairs_after_policy_rejection(self):
        import pandas as pd
        self.ctx.stakeholder.data_cache.put(
            self.tid, "conv-1", "df_1", "orders", pd.DataFrame({"amount": [1, 2, 3]}))
        mock_llm = MagicMock()
        mock_llm.generate.side_effect = [
            MagicMock(text="```python\nimport os\nresult = 1\n```", tokens_in=10, tokens_out=5),
            MagicMock(text="```python\nresult = int(df_1['amount'].sum())\n```", tokens_in=10, tokens_out=5),
        ]

        code, exec_res, toks = self.ctx.stakeholder._synthesize_and_execute_python(
            mock_llm, self.tid, "conv-1", "what's the total amount", "df_1")

        self.assertIsNotNone(exec_res)
        self.assertTrue(exec_res.ok)
        self.assertEqual(exec_res.result_summary, 6)
        second_call_prompt = mock_llm.generate.call_args_list[1].kwargs["prompt"]
        self.assertIn("os", second_call_prompt)

    def test_python_synthesis_repairs_after_execution_failure(self):
        import pandas as pd
        self.ctx.stakeholder.data_cache.put(
            self.tid, "conv-1", "df_1", "orders", pd.DataFrame({"amount": [1, 2, 3]}))
        mock_llm = MagicMock()
        mock_llm.generate.side_effect = [
            MagicMock(text="```python\nresult = 1 / 0\n```", tokens_in=10, tokens_out=5),
            MagicMock(text="```python\nresult = int(df_1['amount'].sum())\n```", tokens_in=10, tokens_out=5),
        ]

        code, exec_res, toks = self.ctx.stakeholder._synthesize_and_execute_python(
            mock_llm, self.tid, "conv-1", "what's the total amount", "df_1")

        self.assertIsNotNone(exec_res)
        self.assertTrue(exec_res.ok)
        second_call_prompt = mock_llm.generate.call_args_list[1].kwargs["prompt"]
        self.assertIn("ZeroDivisionError", second_call_prompt)

    def test_python_synthesis_stops_after_max_attempts_and_returns_none(self):
        import pandas as pd
        self.ctx.stakeholder.data_cache.put(
            self.tid, "conv-1", "df_1", "orders", pd.DataFrame({"amount": [1, 2, 3]}))
        mock_llm = MagicMock()
        mock_llm.generate.return_value = MagicMock(
            text="```python\nresult = 1 / 0\n```", tokens_in=10, tokens_out=5)

        code, exec_res, toks = self.ctx.stakeholder._synthesize_and_execute_python(
            mock_llm, self.tid, "conv-1", "what's the total amount", "df_1", max_attempts=3)

        self.assertIsNone(exec_res)
        self.assertEqual(code, "")
        self.assertEqual(mock_llm.generate.call_count, 3)

    def test_synthesize_and_execute_python_returns_none_for_unknown_label(self):
        mock_llm = MagicMock()
        code, exec_res, toks = self.ctx.stakeholder._synthesize_and_execute_python(
            mock_llm, self.tid, "conv-1", "what's the total", "df_does_not_exist")
        self.assertIsNone(exec_res)
        mock_llm.generate.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -k synthesize_and_execute_python -v`
Expected: FAIL with `AttributeError: 'StakeholderService' object has no attribute '_synthesize_and_execute_python'`.

- [ ] **Step 3: Implement both methods**

Add the import near the top of `stakeholder.py`:

```python
from .execution.python_policy import PythonCodePolicy
from .execution.python_sandbox import run_python_sandboxed
```

Add both methods immediately after `_synthesize_and_execute_sql`
(`stakeholder.py:598`):

```python
    def _synthesize_python(self, llm: Any, question: str, df_label: str,
                           frame_desc: Dict[str, Any], prior_code: str = "",
                           prior_error: str = "") -> Tuple[str, Tuple[int, int]]:
        prompt = (
            f"Question: {question}\n\n"
            f"A pandas DataFrame named `{df_label}` is available with columns "
            f"{frame_desc['columns']} and dtypes {frame_desc['dtypes']} "
            f"({frame_desc['row_count']} rows).\n"
        )
        if prior_code:
            prompt += (
                f"\nYour previous attempt failed:\n{prior_code}\n\nError:\n{prior_error}\n\n"
                "Write corrected code that fixes this specific problem."
            )
        sys_prompt = (
            "You are an expert data analyst. Write pandas Python code that computes the "
            f"answer to the question using the DataFrame `{df_label}` (already in scope -- "
            "do not redefine it or read it from any file/database). Assign your final "
            "answer to a variable named `result` (a scalar, dict, list, or small DataFrame "
            "-- not the full raw DataFrame unmodified). Only `pandas` (as `pd`), `numpy`, "
            "`math`, `statistics`, `datetime`, `collections`, and `re` may be imported; no "
            "file, network, or system access is available and will be rejected. Return "
            "ONLY the Python code in a ```python block. If the question can't be answered "
            "from this DataFrame, output NOTHING."
        )
        try:
            res = llm.generate(prompt=prompt, system_prompt=sys_prompt, temperature=0.0)
            text = (res.text or "").strip() if res and hasattr(res, "text") else ""
            if "```python" in text:
                code = text.split("```python")[1].split("```")[0].strip()
            elif "```" in text:
                code = text.split("```")[1].strip()
            else:
                code = text.strip()
            return code, (getattr(res, "tokens_in", 0), getattr(res, "tokens_out", 0))
        except Exception as exc:  # noqa: BLE001 - Python synthesis is best-effort
            logger.warning("Python synthesis failed for question %r: %s", question, exc,
                           exc_info=True)
            return "", (0, 0)

    def _synthesize_and_execute_python(self, llm: Any, tenant_id: str, conversation_id: str,
                                       question: str, df_label: str,
                                       max_attempts: int = 3) -> Tuple[str, Any, Tuple[int, int]]:
        """Mirrors _synthesize_and_execute_sql's retry loop: synthesize Python,
        run it through PythonCodePolicy then the sandbox, and on
        rejection/failure feed the reason back to the LLM for a corrected
        attempt. Stops at the first successful execution, or after
        max_attempts.

        Returns (code, exec_result_or_None, total_tokens). exec_result is
        None if the label isn't cached, or if every attempt failed -- the
        caller falls back to the SQL path either way.
        """
        df = self.data_cache.get(tenant_id, conversation_id, df_label)
        if df is None:
            return "", None, (0, 0)
        frame_desc = next(
            (f for f in self.data_cache.list_available(tenant_id, conversation_id)
             if f["label"] == df_label),
            {"columns": [], "dtypes": {}, "row_count": 0})

        policy = PythonCodePolicy()
        prior_code, prior_error = "", ""
        t_in_total, t_out_total = 0, 0
        for attempt in range(1, max_attempts + 1):
            code, (t_in, t_out) = self._synthesize_python(
                llm, question, df_label, frame_desc, prior_code=prior_code, prior_error=prior_error)
            t_in_total += t_in
            t_out_total += t_out
            if not code:
                break  # LLM declined -- retrying won't help

            decision = policy.validate(code)
            if decision.denied:
                logger.warning("synthesized Python rejected by policy for tenant %s "
                               "(attempt %d/%d): %s", tenant_id, attempt, max_attempts,
                               decision.reasons)
                prior_code, prior_error = code, "; ".join(decision.reasons)
                continue

            exec_res = run_python_sandboxed(decision.approved_code, {df_label: df})
            if exec_res.ok:
                return decision.approved_code, exec_res, (t_in_total, t_out_total)

            logger.warning("synthesized Python execution failed for tenant %s "
                           "(attempt %d/%d): %s", tenant_id, attempt, max_attempts, exec_res.error)
            prior_code, prior_error = decision.approved_code, exec_res.error

        return "", None, (t_in_total, t_out_total)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -k "synthesize_and_execute_python or python_synthesis" -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/stakeholder.py tests/test_stakeholder.py
git commit -m "feat(stakeholder): add _synthesize_and_execute_python repair loop mirroring the SQL one"
```

---

### Task 7: Wire the Python path into `answer()`, persist `python_cells`

**Files:**
- Modify: `analytics_platform/stakeholder.py`
- Modify: `analytics_platform/database.py`
- Test: `tests/test_stakeholder.py`

**Interfaces:**
- Consumes: `_choose_compute_path` (Task 5), `_synthesize_and_execute_python`
  (Task 6).
- Produces: `_record(..., python_cells: Optional[List[Dict[str,Any]]] = None)`
  — the returned dict and the `stakeholder_answers.python_cells` column both
  carry a list of `{"code": str, "df_label": str, "result_summary": Any}`.
  `get_conversation()`'s per-message dict gains a `python_cells` key so a
  reloaded conversation also shows Python cells (same treatment SQL's
  `queries_run` already gets — no new reload gap introduced here).

- [ ] **Step 1: Write the failing tests**

```python
    def test_answer_routes_to_python_when_cache_hit_and_records_python_cells(self):
        import pandas as pd
        self.ctx.stakeholder.data_cache.put(
            self.tid, "conv-1", "df_1", "orders", pd.DataFrame({"amount": [1, 2, 3]}))
        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [
            MagicMock(text='{"category": "metric_lookup"}', tokens_in=5, tokens_out=5),
            MagicMock(text='{"path": "python", "df_label": "df_1"}', tokens_in=5, tokens_out=5),
            MagicMock(text="```python\nresult = int(df_1['amount'].sum())\n```", tokens_in=10, tokens_out=5),
            MagicMock(text='{"answer": "the total is 6"}', tokens_in=10, tokens_out=5),
        ]
        with patch("analytics_platform.stakeholder.make_role_client", return_value=mock_llm):
            self.ctx.tenants.set_analyst_config(
                self.tid, {"stakeholder": {"enabled": True, "provider": "mock", "model": "mock"}})
            res = self.ctx.stakeholder.answer(
                self.tid, "what's the total amount", conversation_id="conv-1")

        self.assertEqual(res["queries_run"], [])
        self.assertEqual(len(res["python_cells"]), 1)
        self.assertEqual(res["python_cells"][0]["df_label"], "df_1")
        self.assertEqual(res["python_cells"][0]["result_summary"], 6)
        self.assertEqual(res["conversation_id"], "conv-1")

    def test_get_conversation_includes_python_cells_after_reload(self):
        import pandas as pd
        self.ctx.stakeholder.data_cache.put(
            self.tid, "conv-1", "df_1", "orders", pd.DataFrame({"amount": [1, 2, 3]}))
        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [
            MagicMock(text='{"category": "metric_lookup"}', tokens_in=5, tokens_out=5),
            MagicMock(text='{"path": "python", "df_label": "df_1"}', tokens_in=5, tokens_out=5),
            MagicMock(text="```python\nresult = int(df_1['amount'].sum())\n```", tokens_in=10, tokens_out=5),
            MagicMock(text='{"answer": "the total is 6"}', tokens_in=10, tokens_out=5),
        ]
        with patch("analytics_platform.stakeholder.make_role_client", return_value=mock_llm):
            self.ctx.tenants.set_analyst_config(
                self.tid, {"stakeholder": {"enabled": True, "provider": "mock", "model": "mock"}})
            self.ctx.stakeholder.answer(self.tid, "what's the total amount", conversation_id="conv-1")

        conv = self.ctx.stakeholder.get_conversation(self.tid, "conv-1")
        self.assertEqual(len(conv["messages"][0]["python_cells"]), 1)
```

(As with Task 4's test, verify the exact intent-classification mock payload
against what other passing tests in this file already use, and adjust if
needed — the fix loop's TDD steps will surface any mismatch immediately.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -k "routes_to_python or includes_python_cells" -v`
Expected: FAIL (routing branch doesn't exist yet; `python_cells` key missing from both the response dict and `_record`).

- [ ] **Step 3: Add the `python_cells` column migration**

In `analytics_platform/database.py`, inside `_migrate()`'s
`if _has("stakeholder_answers"):` block (`database.py:219-224`), add a third
column check alongside the existing two:

```python
        if _has("stakeholder_answers"):
            sa_cols = {row[1] for row in conn.execute("PRAGMA table_info(stakeholder_answers)").fetchall()}
            if "queries_run" not in sa_cols:
                conn.execute("ALTER TABLE stakeholder_answers ADD COLUMN queries_run TEXT")
            if "conversation_id" not in sa_cols:
                conn.execute("ALTER TABLE stakeholder_answers ADD COLUMN conversation_id TEXT")
            if "python_cells" not in sa_cols:
                conn.execute("ALTER TABLE stakeholder_answers ADD COLUMN python_cells TEXT")
```

- [ ] **Step 4: Extend `_record()` to persist and return `python_cells`**

In `analytics_platform/stakeholder.py`, change `_record`'s signature
(`stakeholder.py:637-645`) to add a new keyword parameter after
`queries_run`:

```python
    def _record(self, tenant_id: str, question: str, user_id: str, category: str,
                trace: str, answer: str, mode: AnswerMode, status: str,
                escalated: bool, source_ids: List[str],
                citations: Optional[List[Dict[str, Any]]] = None,
                facts: Optional[List[str]] = None,
                caveats: Optional[List[str]] = None,
                tokens_in: int = 0, tokens_out: int = 0,
                queries_run: Optional[List[str]] = None,
                python_cells: Optional[List[Dict[str, Any]]] = None,
                conversation_id: str = "") -> Dict[str, Any]:
```

Update the `INSERT` statement and its params (`stakeholder.py:652-661`) to
add the new column (20 placeholders becomes 21):

```python
        self.stores.for_tenant(tenant_id).execute(
            "INSERT INTO stakeholder_answers (id,tenant_id,question,user_id,category,answer,"
            "answer_mode,status,trace_id,created_at,source_node_ids,citations,facts,caveats,"
            "freshness,tokens_in,tokens_out,cost,escalated,queries_run,python_cells,conversation_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (answer_id, tenant_id, question, user_id, category, answer, mode.value, status,
             trace, now_iso(), dump_json(source_ids), dump_json(citations or []),
             dump_json(facts or []), dump_json(caveats or []), freshness,
             tokens_in, tokens_out, cost, int(escalated), dump_json(queries_run or []),
             dump_json(python_cells or []), conversation_id))
```

Update the returned dict (`stakeholder.py:662-667`) to include the field:

```python
        return {"answer_id": answer_id, "tenant_id": tenant_id, "question": question,
                "category": category, "answer": answer, "answer_mode": mode.value,
                "status": status, "escalated": escalated, "citations": citations or [],
                "caveats": caveats or [], "facts": facts or [], "freshness": freshness,
                "cost": cost, "trace_id": trace, "queries_run": queries_run or [],
                "python_cells": python_cells or [], "conversation_id": conversation_id}
```

- [ ] **Step 5: Add `python_cells` to `get_conversation()`'s message dict**

In `get_conversation()` (`stakeholder.py:159-178`), add a line to the
per-message dict comprehension:

```python
        messages = [{
            "answer_id": r["id"], "question": r["question"], "answer": r["answer"],
            "answer_mode": r["answer_mode"], "status": r["status"],
            "citations": load_json(r["citations"], []), "caveats": load_json(r["caveats"], []),
            "facts": load_json(r["facts"], []), "queries_run": load_json(r["queries_run"], []),
            "python_cells": load_json(r["python_cells"], []),
            "escalated": bool(r["escalated"]), "cost": r["cost"], "created_at": r["created_at"],
        } for r in rows]
```

- [ ] **Step 6: Add the routing branch to `answer()`**

In `answer()`, insert this block right after the `is_high_risk` branch
returns (`stakeholder.py:277`, i.e. right after the escalation `return out`)
and before `has_nodes = bool(query_nodes or defn_nodes)` (`stakeholder.py:279`):

```python
        if self._llm_live(llm) and conversation_id:
            path, df_label = self._choose_compute_path(llm, tenant_id, conversation_id, question)
            if path == "python":
                code, exec_res, toks = self._synthesize_and_execute_python(
                    llm, tenant_id, conversation_id, question, df_label)
                if exec_res is not None and exec_res.ok:
                    rows_for_context = (exec_res.result_summary
                                        if isinstance(exec_res.result_summary, list)
                                        else [exec_res.result_summary])
                    data_context = {"rows": rows_for_context}
                    answer, syn_toks, chart_config = self._synthesize(llm, question, category, data_context)
                    t_in = toks[0] + syn_toks[0]
                    t_out = toks[1] + syn_toks[1]

                    out = self._record(tenant_id, question, user_id, category, trace, answer,
                                       AnswerMode.ADAPTED_APPROVED_QUERY, "ANSWERED", False, [],
                                       facts=[f"computed via Python over cached data '{df_label}'"],
                                       caveats=["dynamically generated Python over previously-fetched data"],
                                       tokens_in=t_in, tokens_out=t_out,
                                       python_cells=[{"code": code, "df_label": df_label,
                                                      "result_summary": exec_res.result_summary}],
                                       conversation_id=conversation_id)
                    out["chart_config"] = chart_config
                    out["chart_data"] = rows_for_context
                    self.obs.event(tenant_id=tenant_id, trace_id=trace, stage="stakeholder.answer",
                                   actor="stakeholder", resource=out["answer_id"], status="OK",
                                   meta={"category": category,
                                         "mode": AnswerMode.ADAPTED_APPROVED_QUERY.value,
                                         "compute": "python"})
                    return out
                # Every Python synthesis/policy/execution attempt failed --
                # fall through to the existing SQL path below exactly as if
                # routing had chosen "sql" in the first place.
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -k "routes_to_python or includes_python_cells" -v`
Expected: PASS.

- [ ] **Step 8: Run the full backend suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all tests pass, including every pre-existing `test_stakeholder.py`
test — the new routing branch only fires when `conversation_id` is set AND
the cache has data AND the LLM is live AND routing returns `"python"`; every
existing test either has no cached data (routing returns `"sql"` immediately,
no LLM call spent) or doesn't reach this branch at all.

- [ ] **Step 9: Commit**

```bash
git add analytics_platform/stakeholder.py analytics_platform/database.py tests/test_stakeholder.py
git commit -m "feat(stakeholder): route eligible follow-ups to the Python-over-cache path, persist python_cells"
```

---

### Task 8: Frontend — render `python_cells` via the existing `CollapsibleCode`

**Files:**
- Modify: `frontend/src/store/useStore.ts`
- Modify: `frontend/src/components/StakeholderChat.tsx`

**Interfaces:**
- Consumes: `m.python_cells` (new field on `StakeholderMessage`, produced by
  Task 7); the existing `CollapsibleCode({label, code})` component (already
  in `StakeholderChat.tsx` from plan 1's Task 9 — written generically for
  exactly this reuse, per that plan's Self-Review Notes).

- [ ] **Step 1: Add `python_cells` to `StakeholderMessage`**

In `frontend/src/store/useStore.ts`, find the `StakeholderMessage` type
(around line 9-11, alongside `queries_run: string[]`) and add:

```typescript
  python_cells?: Array<{ code: string; df_label: string; result_summary: unknown }>;
```

(Optional with `?` — unlike `queries_run`, which every message has always
carried since plan 1, `python_cells` is a genuinely new field and older
in-memory/test data won't have it; the render check in Step 2 already guards
on truthiness.)

- [ ] **Step 2: Render `python_cells` blocks**

In `frontend/src/components/StakeholderChat.tsx`, immediately after the
existing `m.queries_run` rendering block (added in plan 1's Task 9 — search
for `SQL executed` to find it), add:

```tsx
                {m.python_cells && m.python_cells.length > 0 && (
                  m.python_cells.map((p, pi) => (
                    <CollapsibleCode
                      key={pi}
                      label={`Python executed${m.python_cells!.length > 1 ? ` (${pi + 1}/${m.python_cells!.length})` : ''}`}
                      code={p.code}
                    />
                  ))
                )}
```

- [ ] **Step 3: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Manual verification**

There is no end-to-end path yet that reliably triggers the Python route
through the live warehouse in this dev environment (routing depends on a
prior successful SQL turn in the same conversation, and this repo's live
Metabase/Athena path has a known, pre-existing, unrelated config gap —
`BrowserSessionExecutor requires a database_id` — that has repeatedly
blocked live end-to-end verification of *any* multi-step live flow in this
codebase; see plan 1's Task 7 and Task 9 ledger entries). Verify the same
way plan 1's Task 9 did: with the dev servers running, monkey-patch
`window.fetch` in the browser console (or via a script) so `POST
/stakeholder/{tenant_id}/answer` returns a synthetic response containing a
`python_cells` array with one entry, submit any question, and confirm a
"▶ Python executed" toggle renders collapsed by default, expands to show the
code on click, and collapses again on a second click — identical behavior
to the SQL block, since both render through the same `CollapsibleCode`
component.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/store/useStore.ts frontend/src/components/StakeholderChat.tsx
git commit -m "feat(frontend): render python_cells via the existing CollapsibleCode component"
```

---

### Task 9: End-to-end multi-turn integration test

**Files:**
- Modify: `tests/test_stakeholder.py`

**Interfaces:**
- Consumes: everything from Tasks 1-7, exercised together across two calls
  to `answer()` sharing one `conversation_id`.

This is the test that actually proves the point of the whole plan: a second
turn in the same conversation answers from cache instead of hitting the
warehouse again. Every earlier task's test exercises its own piece in
isolation with hand-seeded cache state; this one drives it end-to-end
through two real `answer()` calls.

- [ ] **Step 1: Write the failing test**

```python
    def test_multi_turn_conversation_second_turn_uses_cached_dataframe_not_new_sql(self):
        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [
            # Turn 1: intent -> SQL synthesis -> answer synthesis (same shape as
            # test_successful_sql_synthesis_caches_the_resulting_dataframe in Task 4;
            # match that test's exact intent-classification payload).
            MagicMock(text='{"category": "metric_lookup"}', tokens_in=10, tokens_out=5),
            MagicMock(text="```sql\nSELECT amount FROM orders LIMIT 10\n```", tokens_in=20, tokens_out=10),
            MagicMock(text='{"answer": "here is the order data"}', tokens_in=15, tokens_out=8),
            # Turn 2: intent -> routing(python) -> python synthesis -> answer synthesis
            MagicMock(text='{"category": "metric_lookup"}', tokens_in=10, tokens_out=5),
            MagicMock(text='{"path": "python", "df_label": "df_1"}', tokens_in=5, tokens_out=5),
            MagicMock(text="```python\nresult = int(df_1['amount'].sum())\n```", tokens_in=10, tokens_out=5),
            MagicMock(text='{"answer": "the total is computed from what we already fetched"}',
                     tokens_in=10, tokens_out=5),
        ]
        with patch("analytics_platform.stakeholder.make_role_client", return_value=mock_llm):
            self.ctx.tenants.set_analyst_config(
                self.tid, {"stakeholder": {"enabled": True, "provider": "mock", "model": "mock"}})

            turn1 = self.ctx.stakeholder.answer(self.tid, "show me order amounts", conversation_id="")
            conv_id = turn1["conversation_id"]
            self.assertEqual(len(turn1["queries_run"]), 1)

            # Ground truth computed from the cache itself, not hard-coded --
            # this test must pass regardless of what the fixture warehouse's
            # actual order amounts are.
            cached_df = self.ctx.stakeholder.data_cache.get(self.tid, conv_id, "df_1")
            expected_sum = int(cached_df["amount"].sum())

            turn2 = self.ctx.stakeholder.answer(
                self.tid, "what's the total of that", conversation_id=conv_id)

        # Turn 2 answered via Python over the cache, not a fresh SQL execution.
        self.assertEqual(turn2["queries_run"], [])
        self.assertEqual(len(turn2["python_cells"]), 1)
        self.assertEqual(turn2["python_cells"][0]["result_summary"], expected_sum)
        self.assertEqual(turn2["conversation_id"], conv_id)

        conv = self.ctx.stakeholder.get_conversation(self.tid, conv_id)
        self.assertEqual(len(conv["messages"]), 2)
        self.assertEqual(conv["messages"][1]["python_cells"][0]["df_label"], "df_1")
```

Note: if the fixture warehouse's `orders` table has no `amount` column,
substitute whichever numeric column the mocked SQL actually selects (check
`build_retail_warehouse()` or equivalent fixture setup used elsewhere in
`tests/test_stakeholder.py`), and update the mocked SQL text in this test's
`generate.side_effect` to match. The important property this test proves is
structural (turn 2 makes zero SQL calls and its `result_summary` matches the
cached DataFrame's own arithmetic), not any particular column name.

- [ ] **Step 2: Run test to verify it fails (or reveals the correct expected values)**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -k multi_turn_conversation -v`
Expected: fails initially — either because an earlier task's wiring has a
bug this end-to-end path exposes, or simply because the mock LLM response
sequence above doesn't exactly match this codebase's real intent
classification / SQL-synthesis prompt shapes (adjust the mocked payloads to
match, the same way Task 4 and Task 7's tests instruct). Once the mocks are
correct, replace the placeholder assertion with the real expected sum from
the fixture data and re-run.

- [ ] **Step 3: Fix any real bugs this end-to-end test surfaces**

If the mocks are correct but the test still fails, this is signal about a
real integration gap between Tasks 1-7 (e.g. a label mismatch, a
conversation_id not threaded somewhere) — fix the root cause in whichever
earlier task's code it lives in, don't patch around it in the test.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -k multi_turn_conversation -v`
Expected: PASS.

- [ ] **Step 5: Run the full backend suite one more time**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add tests/test_stakeholder.py
git commit -m "test(stakeholder): end-to-end multi-turn test proving turn 2 reuses cached data via Python"
```

---

## Self-Review Notes

- **Spec coverage** against the INDEX's plan-2 description: ✅ multi-turn
  session dynamically choosing SQL vs. Python against an already-fetched,
  cached DataFrame (`_choose_compute_path` + `ConversationDataCache`), ✅
  automatic error-repair loop mirroring `_synthesize_and_execute_sql`
  (`_synthesize_and_execute_python`, same structure, same
  denial/failure-feeds-back-to-LLM contract), ✅ strict local-compute-only
  policy (raw DataFrames never serialized into a prompt or a persisted
  column — only schemas and capped/truncated summaries; see Global
  Constraints), ✅ a real sandbox decision made and implemented (`spawn`
  subprocess + `resource` rlimits + wall-clock timeout + AST static
  pre-check), not deferred again.
- **Why this plan's architecture choice (subprocess + resource limits, not a
  container/VM sandbox or an in-process restricted `exec`):** an in-process
  restricted `exec` shares the parent's memory and can crash or hang the
  whole service on a runaway allocation or infinite loop, and is well-known
  to be escapable through Python's object introspection even with a
  builtins denylist. A per-execution container/VM (Docker, gVisor, firejail)
  is the more bulletproof choice for a genuinely adversarial multi-tenant
  SaaS, but this repo is a single local process already trusted to run
  live-browser AppleScript automation and default to `NullClient`-gated
  LLM calls (see `execution/browser_session.py`, `_llm_live`) — introducing
  a new infra dependency (a container runtime) for a tool serving one
  operator's own questions is disproportionate. `multiprocessing` +
  `resource` rlimits + a fresh `spawn` process per call is the same tier of
  defense-in-depth this codebase already uses elsewhere (the SQL side's
  `QueryPolicy` is also a deterministic denylist, not a proof of security),
  and matches the "file boundary is the real isolation, tenant_id scoping is
  defence-in-depth" philosophy already stated in this plan's own Global
  Constraints.
- **Known, accepted gaps carried forward, not silently dropped:**
  - `python_sandbox.py`'s `memory_mb` rlimit has no dedicated unit test
    (Task 3 — stated inline there, with reasoning).
  - The Python route only ever triggers off a DataFrame produced by the
    *SQL-synthesis* path (Task 4 caches `exec_res.data` from
    `_synthesize_and_execute_sql` specifically). The approved-query-reuse
    and skill-execution paths in `answer()` also fetch data but don't
    currently populate the cache — extending caching to those paths is a
    reasonable future enhancement but is out of scope here: it would need
    each of those paths' own preview-truncation logic reworked to retain
    the full DataFrame, which they currently discard by design, and this
    plan's job is to prove the Python-repair-loop mechanism works, not to
    maximize which paths feed it.
  - Task 8's manual verification, like plan 1's Task 7 and Task 9, can't
    rely on a live end-to-end trigger in this dev environment because of the
    pre-existing `BrowserSessionExecutor requires a database_id` config gap
    — documented rather than silently skipped, with the same fetch-mock
    verification technique plan 1 already used successfully.
  - `python_cells` reuses `AnswerMode.ADAPTED_APPROVED_QUERY` rather than
    introducing a new enum value, since the existing SQL-synthesis path
    already uses that same mode for "LLM wrote code from context to answer
    this" and the frontend's badge rendering (`m.answer_mode`) is
    unconditional/generic (per plan 1's Task 7 finding) — adding a
    dedicated `PYTHON_COMPUTED_ANALYSIS` mode would be a reasonable future
    refinement if the distinction ever needs to be user-visible, but isn't
    required for this plan's stated goal.
