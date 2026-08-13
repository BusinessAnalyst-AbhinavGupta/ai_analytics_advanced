# Skills Portability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a skill a portable analytical *method* that runs against any organisation's warehouse, executed on merit rather than as a fallback for an empty Brain, with its results fed back as reviewable knowledge.

**Architecture:** A skill declares an abstract **data contract** — the shape of data it needs, in its own vocabulary. Each tenant supplies a **binding** mapping that contract onto real tables and columns. The SQL template is written against contract names and is byte-identical for every organisation; the binding is tenant-private and gitignored. The registry only offers the router skills whose contract the current tenant can actually satisfy. Successful executions land in the Brain as `CANDIDATE` nodes.

**Tech Stack:** Python 3.14, stdlib `json`/`re`/`pathlib`, `unittest` + `pytest`.

## Global Constraints

- **No new dependencies.** No YAML library — the existing regex frontmatter parser is extended, not replaced.
- **The core repository must contain no tenant's physical schema.** No table names, column names, or filter literals belonging to one organisation. Per `AGENTS.md` Part 1 §2 and §"Git Ignored Tenants". This is the constraint the whole plan exists to satisfy.
- **Fail loudly on an unsatisfiable skill.** A skill whose contract a tenant cannot meet is never offered to the router and never executed — it is not silently attempted and left to fail at the warehouse.
- **Unresolved placeholders are errors.** SQL containing an unsubstituted `{{...}}` must never reach an executor.
- **No silent failures.** Every `except` this plan touches logs at WARNING or higher via `logging.getLogger(__name__)`.
- Run all commands from the repo root with `.venv/bin/python`.

---

## File Structure

**Created:**
- `analytics_platform/skills/contract.py` — contract parsing, binding resolution, satisfiability
- `analytics_platform/skills/bindings.py` — loading a tenant's binding file
- `tests/test_skill_substitution.py`, `tests/test_skill_registry.py`, `tests/test_skill_contract.py`, `tests/test_skill_integration.py`
- `tenants/<tenant_id>/skill_bindings.json` — the DTDL binding (gitignored), beside that tenant's `tenant.db`
- `.agents/skills/funnel-conversion-analysis/references/*.sql` — rewritten against contract names

**Modified:**
- `analytics_platform/skills/engine.py` — substitution, JSON parsing, execution guard
- `analytics_platform/skills/registry.py` — absolute paths, `consumer` filter, logged load errors
- `analytics_platform/stakeholder.py` — skill selection becomes orthogonal to Brain hits; write-back
- `.gitignore` — tenant binding files

---

### Task 1: Fix placeholder substitution

**Why:** The engine substitutes `$key` and `${key}`. Every SQL template uses `{{KEY}}`. There is no overlap, so no parameter has ever been substituted and the literal `{{STEP_1_PAGE}}` is sent to the warehouse — the skill path has never produced a result. Worse, it fails at the *database*, which reads as a warehouse problem rather than a template problem. Substitution must handle the syntax actually in use, and unresolved placeholders must be caught before execution.

**Files:**
- Modify: `analytics_platform/skills/engine.py:92-129`
- Test: `tests/test_skill_substitution.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `substitute(template: str, params: Dict[str, Any]) -> str` — module-level function in `engine.py`
  - `find_unresolved(sql: str) -> List[str]` — module-level function
  - `SkillExecutionResult` gains no new fields; `execute` returns `ok=False` with a specific error when placeholders remain.

- [ ] **Step 1: Write the failing test**

Create `tests/test_skill_substitution.py`:

```python
"""Skill SQL templating: {{KEY}} substitution and unresolved-placeholder detection."""
from __future__ import annotations

import unittest

from analytics_platform.skills.engine import find_unresolved, substitute


class SubstituteTest(unittest.TestCase):
    def test_mustache_placeholder_is_replaced(self):
        self.assertEqual(substitute("page = '{{STEP_1_PAGE}}'", {"STEP_1_PAGE": "home"}),
                         "page = 'home'")

    def test_whitespace_inside_braces_is_tolerated(self):
        self.assertEqual(substitute("x = {{ STEP_1_PAGE }}", {"STEP_1_PAGE": "home"}),
                         "x = home")

    def test_dotted_contract_names_are_replaced(self):
        self.assertEqual(
            substitute("FROM {{event_stream.table}}",
                       {"event_stream.table": "analytics.events"}),
            "FROM analytics.events")

    def test_repeated_placeholders_are_all_replaced(self):
        self.assertEqual(substitute("{{a}} and {{a}}", {"a": "x"}), "x and x")

    def test_dollar_syntax_still_works(self):
        self.assertEqual(substitute("x = $limit", {"limit": 10}), "x = 10")
        self.assertEqual(substitute("x = ${limit}", {"limit": 10}), "x = 10")

    def test_non_string_values_are_stringified(self):
        self.assertEqual(substitute("n = {{n}}", {"n": 42}), "n = 42")

    def test_unknown_placeholder_is_left_intact(self):
        self.assertEqual(substitute("x = {{missing}}", {}), "x = {{missing}}")

    def test_regex_metacharacters_in_values_are_literal(self):
        self.assertEqual(substitute("x = '{{v}}'", {"v": "a.*b"}), "x = 'a.*b'")


class FindUnresolvedTest(unittest.TestCase):
    def test_detects_a_leftover_placeholder(self):
        self.assertEqual(find_unresolved("SELECT {{STEP_1_PAGE}}"), ["STEP_1_PAGE"])

    def test_detects_several_and_deduplicates(self):
        self.assertEqual(sorted(find_unresolved("{{a}} {{b}} {{a}}")), ["a", "b"])

    def test_fully_substituted_sql_is_clean(self):
        self.assertEqual(find_unresolved("SELECT 1 FROM t WHERE x = 'y'"), [])

    def test_json_braces_are_not_mistaken_for_placeholders(self):
        self.assertEqual(find_unresolved("SELECT json_parse('{\"a\": 1}')"), [])


class ExecuteGuardTest(unittest.TestCase):
    def test_execution_is_refused_when_placeholders_remain(self):
        from analytics_platform.skills.engine import SkillEngine
        from analytics_platform.skills.registry import SkillBundle, SkillMetaData

        class ExplodingExecutor:
            def execute(self, sql, ec):
                raise AssertionError("executor must not be reached")

        skill = SkillBundle(
            meta=SkillMetaData(name="s", description="d", skill_dir="."),
            instructions="", sql_templates={"a.sql": "SELECT {{MISSING}}"})
        result = SkillEngine().execute(skill, {}, ExplodingExecutor(), ec=None)
        self.assertFalse(result.ok)
        self.assertIn("MISSING", result.error)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_skill_substitution.py -v`
Expected: FAIL with `ImportError: cannot import name 'find_unresolved'`.

- [ ] **Step 3: Write the implementation**

In `analytics_platform/skills/engine.py`, add above the `SkillEngine` class:

```python
import logging

logger = logging.getLogger(__name__)

# Templates use {{NAME}}. NAME may contain dots so a contract key such as
# `event_stream.table` substitutes like any other parameter.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")


def substitute(template: str, params: Dict[str, Any]) -> str:
    """Replace {{NAME}}, $NAME and ${NAME} with params. Unknown names are left alone
    so `find_unresolved` can report them rather than silently emitting empty SQL."""
    def _mustache(match: "re.Match[str]") -> str:
        key = match.group(1)
        return str(params[key]) if key in params else match.group(0)

    sql = _PLACEHOLDER_RE.sub(_mustache, template)
    for key, value in params.items():
        val = str(value)
        # re.escape the value so regex metacharacters in data stay literal.
        sql = re.sub(r"\$" + re.escape(key) + r"(?!\w)", val.replace("\\", "\\\\"), sql)
        sql = sql.replace(f"${{{key}}}", val)
    return sql


def find_unresolved(sql: str) -> List[str]:
    """Placeholder names still present after substitution. Non-empty means the SQL
    is not safe to execute."""
    return sorted({m.group(1) for m in _PLACEHOLDER_RE.finditer(sql)})
```

Then replace the substitution loop inside `SkillEngine.execute` with:

```python
        for file_name, sql_template in sorted_templates:
            sql = substitute(sql_template, params)

            unresolved = find_unresolved(sql)
            if unresolved:
                # Sending {{X}} to the warehouse reads as a database error rather
                # than a templating error, which is how this went unnoticed.
                msg = (f"{file_name} has unsubstituted placeholders: "
                       f"{', '.join(unresolved)}")
                logger.warning("skill %s: %s", skill.meta.name, msg)
                return SkillExecutionResult(ok=False, error=msg, queries_run=queries_run)

            queries_run.append(sql)
            result = executor.execute(sql, ec)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_skill_substitution.py -v`
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/skills/engine.py tests/test_skill_substitution.py
git commit -m "fix(skills): substitute {{NAME}} placeholders and refuse unresolved SQL"
```

---

### Task 2: Harden the registry

**Why:** Three problems. `SkillRegistry(base_path=".agents/skills")` is CWD-relative, so which skills exist depends on where the process was launched. The registry loads every `SKILL.md` it finds — including `markitdown`, a Claude Code CLI skill with nothing to do with analytics — and offers it to the LLM router as a candidate answer strategy. And load failures are swallowed by `except Exception: pass`, so a malformed skill is indistinguishable from an absent one.

**Files:**
- Modify: `analytics_platform/skills/registry.py`, `analytics_platform/stakeholder.py:56`
- Test: `tests/test_skill_registry.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `SkillRegistry(base_path: Optional[str] = None, consumer: str = "analytics")` — `base_path` defaults to `<repo root>/.agents/skills`, resolved from `__file__`, not the CWD.
  - `SkillMetaData` gains `consumer: str = ""` and `requires: Dict[str, Any] = {}`.
  - Skills whose frontmatter `consumer` is set to something other than the registry's consumer are excluded. A skill with **no** `consumer` is included, so existing skills keep working.

- [ ] **Step 1: Write the failing test**

Create `tests/test_skill_registry.py`:

```python
"""Registry: repo-anchored paths, consumer filtering, logged load failures."""
from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from analytics_platform.skills.registry import SkillRegistry


def _write_skill(root: Path, name: str, frontmatter: str, sql: str = "SELECT 1") -> None:
    d = root / name
    (d / "references").mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n# {name}\n", encoding="utf-8")
    (d / "references" / "q.sql").write_text(sql, encoding="utf-8")


class ConsumerFilterTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write_skill(self.root, "funnel",
                     "name: funnel\ndescription: funnel analysis\nconsumer: analytics")
        _write_skill(self.root, "markitdown",
                     "name: markitdown\ndescription: convert files\nconsumer: claude-code")
        _write_skill(self.root, "legacy",
                     "name: legacy\ndescription: no consumer declared")

    def tearDown(self):
        self._tmp.cleanup()

    def test_analytics_skills_are_loaded(self):
        reg = SkillRegistry(base_path=str(self.root))
        reg.load_skills()
        self.assertIn("funnel", reg.skills)

    def test_other_consumers_are_excluded(self):
        reg = SkillRegistry(base_path=str(self.root))
        reg.load_skills()
        self.assertNotIn("markitdown", reg.skills)

    def test_skills_without_a_consumer_are_included(self):
        reg = SkillRegistry(base_path=str(self.root))
        reg.load_skills()
        self.assertIn("legacy", reg.skills)

    def test_meta_list_matches_the_loaded_skills(self):
        reg = SkillRegistry(base_path=str(self.root))
        reg.load_skills()
        self.assertEqual({m.name for m in reg.meta}, set(reg.skills))

    def test_sql_templates_are_loaded(self):
        reg = SkillRegistry(base_path=str(self.root))
        reg.load_skills()
        self.assertEqual(reg.get_skill("funnel").sql_templates, {"q.sql": "SELECT 1"})


class RepoAnchoringTest(unittest.TestCase):
    def test_default_path_is_absolute(self):
        self.assertTrue(Path(SkillRegistry().base_path).is_absolute())

    def test_default_path_does_not_depend_on_cwd(self):
        import os
        here = SkillRegistry().base_path
        cwd = os.getcwd()
        try:
            os.chdir(tempfile.gettempdir())
            self.assertEqual(SkillRegistry().base_path, here)
        finally:
            os.chdir(cwd)


class LoadFailureTest(unittest.TestCase):
    def test_a_malformed_skill_is_logged_not_swallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken").mkdir()
            (root / "broken" / "SKILL.md").write_bytes(b"\xff\xfe not utf-8")
            reg = SkillRegistry(base_path=str(root))
            with self.assertLogs("analytics_platform.skills.registry",
                                 level=logging.WARNING):
                reg.load_skills()

    def test_a_skill_without_a_name_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skill(root, "nameless", "description: has no name")
            reg = SkillRegistry(base_path=str(root))
            reg.load_skills()
            self.assertEqual(reg.skills, {})

    def test_a_missing_directory_yields_no_skills(self):
        reg = SkillRegistry(base_path="/nonexistent/path/xyz")
        self.assertEqual(reg.load_skills(), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_skill_registry.py -v`
Expected: FAIL — `markitdown` is loaded (no consumer filter), and the default `base_path` is the relative string `.agents/skills`.

- [ ] **Step 3: Write the implementation**

Rewrite the top of `analytics_platform/skills/registry.py`:

```python
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# <repo root>/.agents/skills — anchored to this file so the set of available
# skills does not depend on the process's working directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SKILLS_PATH = _REPO_ROOT / ".agents" / "skills"


@dataclass
class SkillMetaData:
    name: str
    description: str
    skill_dir: str
    consumer: str = ""              # "" means "any"; non-matching values are excluded
    requires: Dict[str, Any] = field(default_factory=dict)   # data contract (Task 3)
```

Replace `SkillRegistry.__init__` and `load_skills`:

```python
class SkillRegistry:
    def __init__(self, base_path: Optional[str] = None, consumer: str = "analytics"):
        self.base_path = str(Path(base_path).resolve()) if base_path else str(DEFAULT_SKILLS_PATH)
        self.consumer = consumer
        self.skills: Dict[str, SkillBundle] = {}
        self.meta: List[SkillMetaData] = []

    def load_skills(self) -> List[SkillMetaData]:
        root = Path(self.base_path)
        self.skills.clear()
        self.meta.clear()
        if not root.exists() or not root.is_dir():
            logger.info("no skills directory at %s", root)
            return []

        for skill_dir in sorted(root.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                content = skill_md.read_text(encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not read %s: %s", skill_md, exc)
                continue

            try:
                name, description, consumer, requires = self._parse_frontmatter(content)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not parse frontmatter in %s: %s", skill_md, exc)
                continue

            if not name:
                logger.warning("skipping %s: frontmatter has no `name`", skill_md)
                continue
            if consumer and consumer != self.consumer:
                logger.debug("skipping %s: consumer %r != %r", name, consumer, self.consumer)
                continue

            sql_templates: Dict[str, str] = {}
            ref_dir = skill_dir / "references"
            if ref_dir.is_dir():
                for sql_file in sorted(ref_dir.glob("*.sql")):
                    try:
                        sql_templates[sql_file.name] = sql_file.read_text(encoding="utf-8")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("could not read %s: %s", sql_file, exc)

            meta = SkillMetaData(name=name, description=description,
                                 skill_dir=str(skill_dir), consumer=consumer,
                                 requires=requires)
            self.skills[name] = SkillBundle(meta=meta, instructions=content,
                                            sql_templates=sql_templates)
            self.meta.append(meta)

        logger.info("loaded %d skill(s) for consumer %r from %s",
                    len(self.skills), self.consumer, root)
        return self.meta
```

Replace `_parse_frontmatter` to return four values and parse the nested `requires` block:

```python
    def _parse_frontmatter(self, content: str) -> Tuple[str, str, str, Dict[str, Any]]:
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not match:
            return "", "", "", {}
        fm = match.group(1)

        def scalar(key: str) -> str:
            m = re.search(rf"^{key}:\s*(.+)$", fm, re.MULTILINE)
            return m.group(1).strip() if m else ""

        return scalar("name"), scalar("description"), scalar("consumer"), \
            self._parse_requires(fm)

    @staticmethod
    def _parse_requires(fm: str) -> Dict[str, Any]:
        """Parse the two-level `requires:` block without a YAML dependency.

            requires:
              event_stream:
                entity: session identifier
                occurred_at: event timestamp
        """
        lines = fm.splitlines()
        try:
            start = next(i for i, l in enumerate(lines) if l.strip() == "requires:")
        except StopIteration:
            return {}

        requires: Dict[str, Dict[str, str]] = {}
        group: Optional[str] = None
        for line in lines[start + 1:]:
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            if indent == 0:
                break  # back to a top-level key: the block is over
            stripped = line.strip()
            if ":" not in stripped:
                continue
            key, _, value = stripped.partition(":")
            key, value = key.strip(), value.strip()
            if indent <= 2:
                group = key
                requires[group] = {}
            elif group is not None:
                requires[group][key] = value
        return requires
```

- [ ] **Step 4: Anchor the stakeholder's registry**

In `analytics_platform/stakeholder.py:56`, replace:

```python
        self.skill_registry = SkillRegistry()
```

It already takes the new default. Confirm no caller passes a relative path:

Run: `grep -rn "SkillRegistry(" --include="*.py" analytics_platform/ tests/`

- [ ] **Step 5: Add the consumer marker to the real skills**

In `.agents/skills/funnel-conversion-analysis/SKILL.md`, add to the frontmatter after `description:`:

```yaml
consumer: analytics
```

In `.agents/skills/markitdown/SKILL.md`, add:

```yaml
consumer: claude-code
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_skill_registry.py -v`
Expected: 10 passed.

- [ ] **Step 7: Verify the real registry now excludes markitdown**

```bash
.venv/bin/python -c "
from analytics_platform.skills.registry import SkillRegistry
r = SkillRegistry(); r.load_skills()
print('loaded:', sorted(r.skills))
"
```

Expected: `loaded: ['funnel-conversion-analysis']`.

- [ ] **Step 8: Commit**

```bash
git add analytics_platform/skills/registry.py .agents/skills tests/test_skill_registry.py
git commit -m "fix(skills): anchor the registry to the repo and filter by consumer"
```

---

### Task 3: The data contract

**Why:** This is the change that makes the tool multi-organisation. Today a skill fuses two things with different owners and lifecycles: the analytical method (how you measure funnel conversion — universal, yours to maintain) and the schema binding (which table holds events, what the step column is called — one customer's, and currently committed to the shared repository). Splitting them means the method ships once and every tenant supplies a mapping, so onboarding an organisation is configuration rather than a SQL fork.

**Files:**
- Create: `analytics_platform/skills/contract.py`, `analytics_platform/skills/bindings.py`
- Test: `tests/test_skill_contract.py`

**Interfaces:**
- Consumes: `SkillMetaData.requires` (Task 2).
- Produces:
  - `load_binding(tenant_id: str, data_dir: str = "tenants") -> Dict[str, Any]` — reads `<data_dir>/<tenant_id>/skill_bindings.json`, `{}` when absent
  - `can_satisfy(requires: Dict, binding: Dict) -> Tuple[bool, List[str]]` — `(ok, missing_keys)`
  - `resolve_namespace(requires: Dict, binding: Dict) -> Dict[str, str]` — flattens a binding into substitution keys like `event_stream.table`, `event_stream.entity`, `event_stream.filters`

  Task 5 consumes all three.

- [ ] **Step 1: Write the failing test**

Create `tests/test_skill_contract.py`:

```python
"""Skill data contracts: a portable method plus a per-tenant binding."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from analytics_platform.skills.bindings import load_binding
from analytics_platform.skills.contract import can_satisfy, resolve_namespace

_REQUIRES = {
    "event_stream": {
        "entity": "session identifier",
        "step_page": "page identifier for a funnel step",
        "occurred_at": "event timestamp",
    }
}

_BINDING = {
    "event_stream": {
        "table": "analytics.web_events",
        "columns": {"entity": "session_id", "step_page": "page_name",
                    "occurred_at": "event_ts"},
        "filters": ["is_internal = false", "region = 'eu'"],
    }
}


class CanSatisfyTest(unittest.TestCase):
    def test_a_complete_binding_satisfies_the_contract(self):
        self.assertEqual(can_satisfy(_REQUIRES, _BINDING), (True, []))

    def test_a_missing_column_is_reported(self):
        binding = json.loads(json.dumps(_BINDING))
        del binding["event_stream"]["columns"]["step_page"]
        ok, missing = can_satisfy(_REQUIRES, binding)
        self.assertFalse(ok)
        self.assertEqual(missing, ["event_stream.step_page"])

    def test_a_missing_group_reports_every_column(self):
        ok, missing = can_satisfy(_REQUIRES, {})
        self.assertFalse(ok)
        self.assertEqual(sorted(missing),
                         ["event_stream.entity", "event_stream.occurred_at",
                          "event_stream.step_page"])

    def test_a_missing_table_is_reported(self):
        binding = json.loads(json.dumps(_BINDING))
        del binding["event_stream"]["table"]
        ok, missing = can_satisfy(_REQUIRES, binding)
        self.assertFalse(ok)
        self.assertIn("event_stream.table", missing)

    def test_an_empty_contract_is_always_satisfied(self):
        self.assertEqual(can_satisfy({}, {}), (True, []))


class ResolveNamespaceTest(unittest.TestCase):
    def test_table_resolves(self):
        ns = resolve_namespace(_REQUIRES, _BINDING)
        self.assertEqual(ns["event_stream.table"], "analytics.web_events")

    def test_columns_resolve_to_physical_names(self):
        ns = resolve_namespace(_REQUIRES, _BINDING)
        self.assertEqual(ns["event_stream.entity"], "session_id")
        self.assertEqual(ns["event_stream.step_page"], "page_name")

    def test_filters_join_into_a_sql_predicate(self):
        ns = resolve_namespace(_REQUIRES, _BINDING)
        self.assertEqual(ns["event_stream.filters"],
                         "is_internal = false AND region = 'eu'")

    def test_absent_filters_become_a_true_predicate(self):
        binding = {"event_stream": {"table": "t", "columns": {
            "entity": "e", "step_page": "p", "occurred_at": "o"}}}
        self.assertEqual(resolve_namespace(_REQUIRES, binding)["event_stream.filters"],
                         "1 = 1")

    def test_no_namespace_key_is_ever_empty(self):
        for key, value in resolve_namespace(_REQUIRES, _BINDING).items():
            self.assertTrue(value, f"{key} resolved to an empty string")


class LoadBindingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "T1").mkdir()
        (self.root / "T1" / "skill_bindings.json").write_text(
            json.dumps({"funnel": _BINDING}), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_loads_a_tenants_bindings(self):
        self.assertEqual(load_binding("T1", str(self.root))["funnel"], _BINDING)

    def test_an_absent_file_yields_an_empty_mapping(self):
        self.assertEqual(load_binding("T2", str(self.root)), {})

    def test_malformed_json_yields_an_empty_mapping_and_logs(self):
        import logging
        (self.root / "T3").mkdir()
        (self.root / "T3" / "skill_bindings.json").write_text("{ nope", encoding="utf-8")
        with self.assertLogs("analytics_platform.skills.bindings",
                             level=logging.WARNING):
            self.assertEqual(load_binding("T3", str(self.root)), {})

    def test_path_traversal_in_the_tenant_id_is_refused(self):
        self.assertEqual(load_binding("../../etc", str(self.root)), {})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_skill_contract.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'analytics_platform.skills.contract'`.

- [ ] **Step 3: Write contract.py**

Create `analytics_platform/skills/contract.py`:

```python
"""Data contracts: what a skill needs, in the skill's own vocabulary.

A skill declares an abstract shape ("an event stream with an entity, a step and a
timestamp"). A tenant supplies a binding mapping that shape onto real tables and
columns. The SQL template is written against contract names and is identical for
every organisation, which is what lets one codebase serve many companies —
`AGENTS.md` Part 1 §2.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

# Predicate used when a tenant declares no filters, so the template's WHERE clause
# is always syntactically valid.
NO_FILTER = "1 = 1"


def can_satisfy(requires: Dict[str, Any],
                binding: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """(ok, missing) for this contract against this binding.

    Called before a skill is offered to the router, so an unsatisfiable skill is
    never proposed rather than failing later at the warehouse.
    """
    missing: List[str] = []
    for group, columns in (requires or {}).items():
        bound = (binding or {}).get(group) or {}
        if not bound.get("table"):
            missing.append(f"{group}.table")
        bound_columns = bound.get("columns") or {}
        for column in (columns or {}):
            if not bound_columns.get(column):
                missing.append(f"{group}.{column}")
    return (not missing), sorted(set(missing))


def resolve_namespace(requires: Dict[str, Any],
                      binding: Dict[str, Any]) -> Dict[str, str]:
    """Flatten a binding into `{{group.key}}` substitution values.

    Produces `group.table`, one entry per contract column, and `group.filters`
    (the tenant's filter list joined with AND).
    """
    namespace: Dict[str, str] = {}
    for group, columns in (requires or {}).items():
        bound = (binding or {}).get(group) or {}
        namespace[f"{group}.table"] = str(bound.get("table") or "")
        bound_columns = bound.get("columns") or {}
        for column in (columns or {}):
            namespace[f"{group}.{column}"] = str(bound_columns.get(column) or "")
        filters = [str(f).strip() for f in (bound.get("filters") or []) if str(f).strip()]
        namespace[f"{group}.filters"] = " AND ".join(filters) if filters else NO_FILTER
    return namespace
```

- [ ] **Step 4: Write bindings.py**

Create `analytics_platform/skills/bindings.py`:

```python
"""Loading a tenant's skill bindings.

Bindings describe one organisation's physical schema, so they live under
`tenants/<id>/` and are gitignored — the shared repository stays tenant-agnostic
(`AGENTS.md` Part 1 §2).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

BINDINGS_FILENAME = "skill_bindings.json"


def load_binding(tenant_id: str, data_dir: str = "tenants") -> Dict[str, Any]:
    """All skill bindings for a tenant, keyed by skill name. {} when absent."""
    root = Path(data_dir).resolve()
    path = (root / tenant_id / BINDINGS_FILENAME).resolve()

    # A tenant id is an identifier, never a path. Refuse anything that escapes.
    if not str(path).startswith(str(root) + "/"):
        logger.warning("refusing binding path outside %s for tenant %r", root, tenant_id)
        return {}

    if not path.exists():
        logger.debug("no skill bindings for tenant %s at %s", tenant_id, path)
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read skill bindings at %s: %s", path, exc)
        return {}

    if not isinstance(data, dict):
        logger.warning("skill bindings at %s are not a JSON object", path)
        return {}
    return data
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_skill_contract.py -v`
Expected: 14 passed.

- [ ] **Step 6: Commit**

```bash
git add analytics_platform/skills/contract.py analytics_platform/skills/bindings.py tests/test_skill_contract.py
git commit -m "feat(skills): data contracts and per-tenant schema bindings"
```

---

### Task 4: Move the tenant-specific SQL out of core

**Why:** `canonical_happy_flow.sql` hardcodes `FROM eshop_data.es_events_v2`, `nc = 'de'`, and columns like `identifiers_page_name` and `internalemployee`. That is one organisation's warehouse committed to the shared repository and offered to every tenant — a direct violation of `AGENTS.md` Part 1 §2. Rewriting the templates against contract names makes the same file correct for every customer.

**Files:**
- Modify: `.agents/skills/funnel-conversion-analysis/SKILL.md`, both files in `.agents/skills/funnel-conversion-analysis/references/`
- Create: `tenants/<tenant_id>/skill_bindings.json`
- Modify: `.gitignore`

- [ ] **Step 1: Declare the contract**

Read the two SQL files and list every physical table, column and filter literal they reference:

```bash
grep -nE "FROM |JOIN |WHERE |AND |[a-z_]+ =" .agents/skills/funnel-conversion-analysis/references/*.sql
```

Add a `requires:` block to `.agents/skills/funnel-conversion-analysis/SKILL.md` frontmatter naming each one in abstract terms. Using the columns visible today:

```yaml
---
name: funnel-conversion-analysis
description: Use when analyzing customer conversion funnels, building happy flows, or identifying step-by-step drop-off rates between natural language start and end points in clickstream data
consumer: analytics
requires:
  event_stream:
    entity: identifier for the session or user whose journey is being traced
    page: page identifier for a funnel step
    action: action identifier for a funnel step
    label: label identifier for a funnel step
    occurred_at: event timestamp used to order steps
---
```

If the SQL references a column not in this list, add it. The contract must be complete — a column that is not declared cannot be bound.

- [ ] **Step 2: Rewrite the templates against contract names**

In both SQL files, replace every tenant-specific identifier with its contract placeholder:

| Was | Becomes |
|---|---|
| `eshop_data.es_events_v2` | `{{event_stream.table}}` |
| `identifiers_page_name` | `{{event_stream.page}}` |
| `action_name` | `{{event_stream.action}}` |
| `label_name` | `{{event_stream.label}}` |
| `log_time_ms` | `{{event_stream.occurred_at}}` |
| the session/user id column | `{{event_stream.entity}}` |
| `nc = 'de' AND internalemployee = ...` | `{{event_stream.filters}}` |

Leave the analysis parameters (`{{STEP_1_PAGE}}`, `{{STEP_1_ACTION}}`, `{{STEP_1_LABEL}}` …) exactly as they are — those are extracted per question, not per tenant.

- [ ] **Step 3: Verify no tenant identifiers remain in core**

```bash
grep -rniE "eshop_data|es_events_v2|identifiers_page_name|internalemployee|nc *= *'de'" .agents/ analytics_platform/
```

Expected: no matches. Any hit is a value that still needs a contract placeholder.

- [ ] **Step 4: Write the DTDL binding**

Create `tenants/tnt_d23cd823d4c6/skill_bindings.json` using the values removed in Step 2.
(That directory is DTDL's tenant id — the same one holding its `tenant.db` after the
tenant-store-isolation plan's `adopt-db` step. Bindings live beside the database they
describe, so a company's whole footprint is one directory.)

```json
{
  "funnel-conversion-analysis": {
    "event_stream": {
      "table": "eshop_data.es_events_v2",
      "columns": {
        "entity": "session_id",
        "page": "identifiers_page_name",
        "action": "action_name",
        "label": "label_name",
        "occurred_at": "log_time_ms"
      },
      "filters": ["nc = 'de'", "internalemployee = false"]
    }
  }
}
```

Replace `session_id` and the filter literals with the actual values from the original SQL — read the file in git history if you have already edited it:

```bash
git show HEAD:.agents/skills/funnel-conversion-analysis/references/canonical_happy_flow.sql
```

- [ ] **Step 5: Gitignore tenant bindings**

Add to `.gitignore`:

```
# Tenant-specific schema bindings (AGENTS.md: the repo holds only the universal tool)
tenants/*/skill_bindings.json
```

Confirm the file is untracked:

```bash
git status --porcelain tenants/tnt_d23cd823d4c6/skill_bindings.json
git check-ignore -v tenants/tnt_d23cd823d4c6/skill_bindings.json
```

Expected: no output from the first (ignored), a matching rule from the second.

- [ ] **Step 6: Commit**

```bash
git add .agents/skills/funnel-conversion-analysis .gitignore
git commit -m "refactor(skills): rewrite the funnel skill against a data contract

The SQL no longer contains any organisation's table names, column names or filter
literals; those move to a gitignored per-tenant binding. AGENTS.md Part 1 §2."
```

---

### Task 5: Skills become an orthogonal axis, and write back

**Why:** Two structural problems. Skill matching runs only in the `else` branch after the Brain returns nothing, so the better-curated the Brain gets, the less the analyst can reason — a funnel question that happens to match one approved query never gets the funnel methodology applied. And a successful skill run — a parameterised, methodologically-sound, actually-executed analysis, the highest-quality candidate the system can produce — is returned to the user and discarded rather than entering the review queue.

**Files:**
- Modify: `analytics_platform/stakeholder.py:40-61, 120, 210-267`, `analytics_platform/skills/engine.py` (satisfiability in `match`)
- Test: `tests/test_skill_integration.py`

**Interfaces:**
- Consumes: `load_binding`, `can_satisfy`, `resolve_namespace` (Task 3); `CompanyBrain.create` (existing).
- Produces:
  - `StakeholderService.available_skills(tenant_id: str) -> List[SkillMetaData]` — only skills this tenant can satisfy
  - `StakeholderService._skill_namespace(tenant_id, skill) -> Dict[str, str]`
  - `StakeholderService._write_back_skill_run(...) -> Optional[str]` — creates a `CANDIDATE` FINDING and returns its id

- [ ] **Step 1: Write the failing test**

Create `tests/test_skill_integration.py`:

```python
"""Skills as an orthogonal axis, gated on satisfiability, feeding the review queue."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import pandas as pd

from analytics_platform.domain import NodeKind, ReviewStatus
from tests.helpers import make_ctx

_WAREHOUSE = {"events": pd.DataFrame({"sid": [1, 2], "pg": ["home", "pay"],
                                      "ts": [1, 2]})}


class AvailableSkillsTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from analytics_platform.stakeholder import StakeholderService
        self.ctx = make_ctx(_WAREHOUSE)
        self.ctx.tenants.create("t1", name="T1")
        self._tmp = tempfile.TemporaryDirectory()
        self.tenants_dir = Path(self._tmp.name)

        skills = self.tenants_dir / "_skills"
        (skills / "funnel" / "references").mkdir(parents=True)
        (skills / "funnel" / "SKILL.md").write_text(
            "---\nname: funnel\ndescription: funnel analysis\nconsumer: analytics\n"
            "requires:\n  event_stream:\n    entity: session id\n    page: page name\n"
            "---\n", encoding="utf-8")
        (skills / "funnel" / "references" / "q.sql").write_text(
            "SELECT {{event_stream.entity}} FROM {{event_stream.table}} "
            "WHERE {{event_stream.filters}} AND {{event_stream.page}} = '{{PAGE}}'",
            encoding="utf-8")

        self.svc = StakeholderService(self.ctx.store, tenants=self.ctx.tenants,
                                      executor=self.ctx.executor,
                                      observability=self.ctx.obs,
                                      settings=self.ctx.settings,
                                      skills_path=str(skills),
                                      tenants_dir=str(self.tenants_dir))

    def tearDown(self):
        self.ctx.close()
        self._tmp.cleanup()

    def _bind(self, tenant: str, binding: dict) -> None:
        d = self.tenants_dir / tenant
        d.mkdir(exist_ok=True)
        (d / "skill_bindings.json").write_text(json.dumps(binding), encoding="utf-8")

    def test_an_unbound_tenant_is_offered_no_skills(self):
        self.assertEqual(self.svc.available_skills("t1"), [])

    def test_a_bound_tenant_is_offered_the_skill(self):
        self._bind("t1", {"funnel": {"event_stream": {
            "table": "events", "columns": {"entity": "sid", "page": "pg"}}}})
        self.assertEqual([m.name for m in self.svc.available_skills("t1")], ["funnel"])

    def test_an_incomplete_binding_is_not_offered(self):
        self._bind("t1", {"funnel": {"event_stream": {
            "table": "events", "columns": {"entity": "sid"}}}})   # no `page`
        self.assertEqual(self.svc.available_skills("t1"), [])

    def test_the_namespace_resolves_to_physical_names(self):
        self._bind("t1", {"funnel": {"event_stream": {
            "table": "events", "columns": {"entity": "sid", "page": "pg"},
            "filters": ["1 = 1"]}}})
        ns = self.svc._skill_namespace("t1", self.svc.skill_registry.get_skill("funnel"))
        self.assertEqual(ns["event_stream.table"], "events")
        self.assertEqual(ns["event_stream.page"], "pg")


class WriteBackTest(unittest.TestCase):
    def setUp(self):
        from analytics_platform.stakeholder import StakeholderService
        self.ctx = make_ctx(_WAREHOUSE)
        self.ctx.tenants.create("t1", name="T1")
        self.svc = StakeholderService(self.ctx.store, tenants=self.ctx.tenants,
                                      executor=self.ctx.executor,
                                      observability=self.ctx.obs,
                                      settings=self.ctx.settings)

    def tearDown(self):
        self.ctx.close()

    def test_a_skill_run_becomes_a_candidate_finding(self):
        node_id = self.svc._write_back_skill_run(
            "t1", question="How does checkout convert?", skill_name="funnel",
            answer="Conversion is 62%.", queries_run=["SELECT 1"])
        node = self.ctx.pipeline.brain("t1").get(node_id)
        self.assertEqual(node.kind, NodeKind.FINDING)
        self.assertEqual(node.status, ReviewStatus.CANDIDATE)

    def test_it_is_never_auto_approved(self):
        self.svc._write_back_skill_run("t1", question="q", skill_name="funnel",
                                       answer="a", queries_run=["SELECT 1"])
        approved = self.ctx.store.query_all(
            "SELECT id FROM knowledge_nodes WHERE tenant_id=? AND status=?",
            ("t1", ReviewStatus.APPROVED.value))
        self.assertEqual(approved, [])

    def test_the_sql_is_preserved_for_the_reviewer(self):
        node_id = self.svc._write_back_skill_run(
            "t1", question="q", skill_name="funnel", answer="a",
            queries_run=["SELECT 42"])
        node = self.ctx.pipeline.brain("t1").get(node_id)
        self.assertIn("SELECT 42", json.dumps(node.payload))

    def test_the_originating_skill_is_recorded(self):
        node_id = self.svc._write_back_skill_run(
            "t1", question="q", skill_name="funnel", answer="a", queries_run=[])
        node = self.ctx.pipeline.brain("t1").get(node_id)
        self.assertEqual(node.payload.get("skill"), "funnel")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_skill_integration.py -v`
Expected: FAIL — `StakeholderService.__init__() got an unexpected keyword argument 'skills_path'`.

- [ ] **Step 3: Add the constructor arguments and satisfiability**

In `analytics_platform/stakeholder.py`, add `skills_path: Optional[str] = None` and `tenants_dir: str = "tenants"` to `StakeholderService.__init__`, and replace the registry construction:

```python
        self.tenants_dir = tenants_dir
        self.skill_registry = SkillRegistry(base_path=skills_path)
        self.skill_registry.load_skills()
        self.skill_engine = SkillEngine()
```

Add these methods:

```python
    def available_skills(self, tenant_id: str) -> List[Any]:
        """Skills whose data contract this tenant's binding satisfies.

        An unsatisfiable skill is never offered to the router: proposing a method
        the warehouse cannot support produces a confusing SQL error instead of an
        honest "no skill applies".
        """
        binding = load_binding(tenant_id, self.tenants_dir)
        out = []
        for meta in self.skill_registry.meta:
            ok, missing = can_satisfy(meta.requires, binding.get(meta.name, {}))
            if ok:
                out.append(meta)
            else:
                logger.debug("skill %s unavailable for tenant %s; unbound: %s",
                             meta.name, tenant_id, ", ".join(missing))
        return out

    def _skill_namespace(self, tenant_id: str, skill: Any) -> Dict[str, str]:
        """Contract placeholders resolved to this tenant's physical schema."""
        binding = load_binding(tenant_id, self.tenants_dir)
        return resolve_namespace(skill.meta.requires, binding.get(skill.meta.name, {}))
```

Add the imports at the top of `stakeholder.py`:

```python
import logging

from .skills.bindings import load_binding
from .skills.contract import can_satisfy, resolve_namespace

logger = logging.getLogger(__name__)
```

- [ ] **Step 4: Add write-back**

Add to `StakeholderService`:

```python
    def _write_back_skill_run(self, tenant_id: str, question: str, skill_name: str,
                              answer: str, queries_run: List[str]) -> Optional[str]:
        """File a completed skill analysis as a CANDIDATE finding.

        A skill execution is the highest-quality candidate the platform produces —
        a parameterised, methodologically-grounded, actually-executed analysis.
        Discarding it is why the Brain only ever grows from the junior worker.
        CANDIDATE, never approved: AGENTS.md Part 1 §3.
        """
        try:
            node = self.brain(tenant_id).create(
                NodeKind.FINDING, title=question[:200], summary=answer,
                payload={"skill": skill_name, "queries_run": list(queries_run),
                         "origin": "stakeholder.skill"},
                created_by="stakeholder", status=ReviewStatus.CANDIDATE)
        except Exception as exc:  # noqa: BLE001 - never fail the user's answer
            logger.warning("could not write back skill run for tenant %s: %s",
                           tenant_id, exc, exc_info=True)
            return None
        self.obs.event(tenant_id=tenant_id, stage="stakeholder.skill_writeback",
                       actor="stakeholder", resource=node.id, status="OK",
                       meta={"skill": skill_name})
        return node.id
```

Ensure `NodeKind` and `ReviewStatus` are imported in `stakeholder.py`.

- [ ] **Step 5: Make skill selection orthogonal to Brain hits**

In `answer()`, move the skill block so it runs *before* the `if query_nodes:` branch rather than inside the trailing `else`. The order becomes:

1. classify → escalate if high-risk (unchanged)
2. retrieve from the Brain (unchanged)
3. **if the LLM is live, try a skill match over `available_skills(tenant_id)`** — a matched, satisfiable skill wins, because a method that fits the question beats a single approved query that merely shares vocabulary
4. otherwise fall back to approved `query_nodes`, then `defn_nodes`, then raw synthesis (all unchanged)

Change the `match` call to use the filtered list:

```python
            skill_match = self.skill_engine.match(
                question, self.available_skills(tenant_id), llm)
```

Merge the contract namespace into the extracted parameters so the SQL resolves — replace the `exec_res = ...` line:

```python
                    params = {**self._skill_namespace(tenant_id, skill), **params}
                    ec = ExecutionContext(tenant_id=tenant_id, question=question,
                                          dialect="athena")
                    exec_res = self.skill_engine.execute(skill, params, self.executor, ec)
```

The tenant namespace goes first so an LLM-extracted parameter can never override a bound table name.

Finally, on the success path, add the write-back before the `return out` — after `out["chart_data"] = ...`:

```python
                    candidate_id = self._write_back_skill_run(
                        tenant_id, question, skill.meta.name, answer,
                        exec_res.queries_run)
                    if candidate_id:
                        out.setdefault("source_ids", []).append(candidate_id)
                        out["caveats"] = list(out.get("caveats", [])) + [
                            "filed for senior review as a candidate finding"]
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_skill_integration.py -v`
Expected: 8 passed.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: `tests/test_stakeholder.py` may assert that skills only run when the Brain is empty — that was the defect. Update those assertions and comment the change.

- [ ] **Step 8: Commit**

```bash
git add analytics_platform/ tests/test_skill_integration.py
git commit -m "feat(skills): satisfiability gating, contract binding, and write-back

Skills are selected on merit rather than as an empty-Brain fallback, are only
offered when the tenant's binding satisfies their contract, and file their results
as CANDIDATE findings so the Brain compounds."
```

---

## Verification

- [ ] `.venv/bin/python -m pytest tests/ -q` — all green
- [ ] `grep -rniE "eshop_data|es_events_v2|identifiers_page_name|internalemployee" .agents/ analytics_platform/` — no matches
- [ ] `git check-ignore -v tenants/tnt_d23cd823d4c6/skill_bindings.json` — matched by a rule
- [ ] `SkillRegistry().load_skills()` returns only analytics skills
- [ ] A tenant with no binding is offered no skills, and the answer path still returns a sensible fallback
- [ ] A successful skill run creates exactly one `CANDIDATE` FINDING
