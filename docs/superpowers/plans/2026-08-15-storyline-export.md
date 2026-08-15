# Storyline Export (Report Builder, Word/Markdown) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a stakeholder select a subset of turns from a chat conversation and export
them as a Markdown or Word (`.docx`) document, with a dependency-tracked Code Appendix
(a selected Python cell pulls in the SQL turn that produced the DataFrame it ran
against, even if that turn wasn't itself selected) and a proactive warning before export
if the selected content is large.

**Architecture:** A pure, DB-free assembly function (`assemble_storyline`) turns an
already-fetched conversation dict (the same shape `StakeholderService.get_conversation`
already returns) plus a list of selected `answer_id`s into a small, format-agnostic
intermediate representation (`StorylineContent`). Two renderers
(`render_markdown`, `render_docx`) turn that IR into bytes. A single new API endpoint
assembles + renders + returns the file. The frontend adds a toggleable "Report Builder"
panel to the existing chat surface: checkboxes per turn, a client-side token estimate,
and an Export button that downloads the result.

**Tech Stack:** Python (stdlib dataclasses for the IR), `python-docx` 1.2.0 for `.docx`
generation (new dependency — not previously used in this repo), FastAPI (existing),
React/Zustand (existing, following `frontend/src/store/useStore.ts`'s established
fetch-and-`setStakeholder` pattern).

## Global Constraints

- No feature branches — every task commits directly to `main` (standing project
  convention; persistent memory `no-feature-branches-work-on-main`).
- New columns on an existing table: a non-destructive `ALTER TABLE ... ADD COLUMN` in
  `_migrate()`, following the existing `queries_run`/`python_cells` migration as the
  template (`analytics_platform/database.py:219-229`).
- No silent failures: an export request for an unknown conversation or an `answer_id`
  not belonging to it is a 404/400 with a clear message, never a silently-empty
  document.
- The Code Appendix dependency rule: if a selected turn has a `python_cells` entry with
  `df_label` X, and some OTHER turn in the same conversation has `produced_df_label ==
  X`, that other turn's SQL is included in the Code Appendix even if its own
  `answer_id` was not selected — annotated as a dependency, not presented as if the user
  chose it.
- Token-estimate heuristic (used consistently, documented as an approximation, not an
  exact tokenizer count): `estimated_tokens = len(text) // 4`. Warning threshold:
  `50_000` estimated tokens. This is a proactive, non-blocking warning — export must
  still succeed even over the threshold; the UI just shows a banner first.
- `assemble_storyline` takes an already-fetched conversation dict, never a `tenant_id`/
  store handle — this keeps it a pure function, trivially unit-testable without a DB,
  and reusable by both renderers without duplicating dependency-resolution logic.
- Every tenant's data lives in its own SQLite file — the export endpoint scopes through
  `C.stakeholder.get_conversation(tenant_id, conversation_id)` exactly like every other
  conversation route already does; no new cross-tenant surface is introduced.

---

### Task 1: `produced_df_label` column — track which turn populated which cached frame

**Files:**
- Modify: `analytics_platform/database.py`
- Modify: `analytics_platform/stakeholder.py`
- Test: `tests/test_stakeholder.py`

**Interfaces:**
- Produces: `stakeholder_answers.produced_df_label` column (empty string when a turn
  didn't populate the cache); `_record(..., produced_df_label: str = "")`; the returned
  dict and `get_conversation()`'s per-message dict both gain a `produced_df_label` key.
  This is the join key Task 2's dependency tracking relies on.

- [ ] **Step 1: Write the failing test**

```python
    def test_sql_turn_records_the_df_label_it_populated_in_the_cache(self):
        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [
            MagicMock(text='{"category": "metric_lookup"}', tokens_in=5, tokens_out=5),
            MagicMock(text="SELECT COUNT(*) AS orders FROM events WHERE action = 'order'",
                      tokens_in=10, tokens_out=5),
            MagicMock(text='{"answer": "there are some orders"}', tokens_in=10, tokens_out=5),
        ]
        with patch("analytics_platform.stakeholder.make_role_client", return_value=mock_llm):
            self.ctx.tenants.set_analyst_config(
                self.tid, {"stakeholder": {"enabled": True, "provider": "mock", "model": "mock"}})
            self.ctx.stakeholder.add_datasource(
                self.tid, "wh", "athena", "public", tables=["events"])
            res = self.ctx.stakeholder.answer(
                self.tid, "how many orders", conversation_id="conv-1")

        self.assertEqual(res["produced_df_label"], "df_1")
        conv = self.ctx.stakeholder.get_conversation(self.tid, res["conversation_id"])
        self.assertEqual(conv["messages"][0]["produced_df_label"], "df_1")

    def test_python_turn_records_no_produced_df_label(self):
        import pandas as pd
        self.ctx.stakeholder.data_cache.put(
            self.tid, "conv-2", "df_1", "orders", pd.DataFrame({"revenue": [1, 2, 3]}))
        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [
            MagicMock(text='{"category": "metric_lookup"}', tokens_in=5, tokens_out=5),
            MagicMock(text='{"path": "python", "df_label": "df_1"}', tokens_in=5, tokens_out=5),
            MagicMock(text="```python\nresult = int(df_1['revenue'].sum())\n```",
                      tokens_in=10, tokens_out=5),
            MagicMock(text='{"answer": "the total is 6"}', tokens_in=10, tokens_out=5),
        ]
        with patch("analytics_platform.stakeholder.make_role_client", return_value=mock_llm):
            self.ctx.tenants.set_analyst_config(
                self.tid, {"stakeholder": {"enabled": True, "provider": "mock", "model": "mock"}})
            res = self.ctx.stakeholder.answer(
                self.tid, "what's the total", conversation_id="conv-2")

        self.assertEqual(res["produced_df_label"], "")
```

(As with every prior task's tests in this file, verify the exact intent-classification
mock payload and the SQL string against what other passing tests in `test_stakeholder.py`
already use, and adjust if the draft above doesn't match — the TDD failure step will
surface any mismatch immediately. The `conversation_id="conv-2"` literal in the second
test is fine to seed the cache under directly, since that test never calls
`_ensure_conversation` before seeding — `answer()` itself calls `_ensure_conversation`
first thing, which will find no such conversation, silently mint a **new** id, and the
cache read for that new id will correctly find nothing cached — so this second test is
actually exercising the "nothing cached, `_choose_compute_path` returns sql immediately"
path unless you first establish `conv-2` for real. To make it a genuine Python-path
test, call `self.ctx.stakeholder.answer(self.tid, "seed", conversation_id="")` once
first to mint a real conversation, seed the cache under the id it returns, then ask the
follow-up in that same conversation — mirroring the pattern Task 7's and Task 9's tests
in the prior plan already established for exactly this reason.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -k "produced_df_label" -v`
Expected: FAIL (`KeyError: 'produced_df_label'`).

- [ ] **Step 3: Add the migration**

In `analytics_platform/database.py`, inside `_migrate()`'s `if
_has("stakeholder_answers"):` block, add a fourth column check alongside the existing
three:

```python
            if "python_cells" not in sa_cols:
                conn.execute("ALTER TABLE stakeholder_answers ADD COLUMN python_cells TEXT")
            if "produced_df_label" not in sa_cols:
                conn.execute("ALTER TABLE stakeholder_answers ADD COLUMN produced_df_label TEXT")
```

- [ ] **Step 4: Thread `produced_df_label` through `_record()`**

In `analytics_platform/stakeholder.py`, add the parameter to `_record`'s signature
(after `python_cells`):

```python
                python_cells: Optional[List[Dict[str, Any]]] = None,
                produced_df_label: str = "",
                conversation_id: str = "") -> Dict[str, Any]:
```

Add the column to the `INSERT` (23 placeholders now) and its params tuple:

```python
        self.stores.for_tenant(tenant_id).execute(
            "INSERT INTO stakeholder_answers (id,tenant_id,question,user_id,category,answer,"
            "answer_mode,status,trace_id,created_at,source_node_ids,citations,facts,caveats,"
            "freshness,tokens_in,tokens_out,cost,escalated,queries_run,python_cells,"
            "produced_df_label,conversation_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (answer_id, tenant_id, question, user_id, category, answer, mode.value, status,
             trace, now_iso(), dump_json(source_ids), dump_json(citations or []),
             dump_json(facts or []), dump_json(caveats or []), freshness,
             tokens_in, tokens_out, cost, int(escalated), dump_json(queries_run or []),
             dump_json(python_cells or []), produced_df_label, conversation_id))
```

Add it to the returned dict:

```python
        return {"answer_id": answer_id, "tenant_id": tenant_id, "question": question,
                "category": category, "answer": answer, "answer_mode": mode.value,
                "status": status, "escalated": escalated, "citations": citations or [],
                "caveats": caveats or [], "facts": facts or [], "freshness": freshness,
                "cost": cost, "trace_id": trace, "queries_run": queries_run or [],
                "python_cells": python_cells or [], "produced_df_label": produced_df_label,
                "conversation_id": conversation_id}
```

- [ ] **Step 5: Set it on the SQL-caching path**

In `answer()`, the SQL-success block currently reads (find it — it's the block
containing `self.data_cache.next_label` and `self.data_cache.put`):

```python
            if exec_res is not None and exec_res.ok:
                if exec_res.data is not None and conversation_id:
                    label = self.data_cache.next_label(tenant_id, conversation_id)
                    self.data_cache.put(tenant_id, conversation_id, label, question[:200], exec_res.data)
```

Change it so `label` is always defined (empty string when nothing was cached), and pass
it through to `_record`:

```python
            if exec_res is not None and exec_res.ok:
                label = ""
                if exec_res.data is not None and conversation_id:
                    label = self.data_cache.next_label(tenant_id, conversation_id)
                    self.data_cache.put(tenant_id, conversation_id, label, question[:200], exec_res.data)
```

Then in the `_record(...)` call in the same block (the one with
`AnswerMode.ADAPTED_APPROVED_QUERY, "ANSWERED", False, [n.id for n in ...]`), add
`produced_df_label=label` as a keyword argument alongside the existing `queries_run=[sql]`.

- [ ] **Step 6: Add it to `get_conversation()`'s message dict**

```python
        messages = [{
            "answer_id": r["id"], "question": r["question"], "answer": r["answer"],
            "answer_mode": r["answer_mode"], "status": r["status"],
            "citations": load_json(r["citations"], []), "caveats": load_json(r["caveats"], []),
            "facts": load_json(r["facts"], []), "queries_run": load_json(r["queries_run"], []),
            "python_cells": load_json(r["python_cells"], []),
            "produced_df_label": r["produced_df_label"] or "",
            "escalated": bool(r["escalated"]), "cost": r["cost"], "created_at": r["created_at"],
        } for r in rows]
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -k "produced_df_label" -v`
Expected: PASS.

- [ ] **Step 8: Run the full backend suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass, zero regressions (this column is additive and defaults to `""`
everywhere it isn't explicitly set).

- [ ] **Step 9: Commit**

```bash
git add analytics_platform/database.py analytics_platform/stakeholder.py tests/test_stakeholder.py
git commit -m "feat(stakeholder): record which turn produced which cached DataFrame label"
```

---

### Task 2: `assemble_storyline` — pure content assembly with dependency tracking

**Files:**
- Create: `analytics_platform/storyline.py`
- Test: `tests/test_storyline.py`

**Interfaces:**
- Consumes: a conversation dict shaped exactly like `StakeholderService.get_conversation`'s
  return value (Task 1 added `produced_df_label` to each message).
- Produces: `StorylineTurn`, `CodeAppendixEntry`, `StorylineContent` dataclasses;
  `assemble_storyline(conversation: Dict[str, Any], answer_ids: List[str]) ->
  StorylineContent`. Task 3 and Task 4's renderers consume `StorylineContent` — this is
  the shared contract between them, so its field names are load-bearing for both.

- [ ] **Step 1: Write the failing tests**

```python
import unittest

from analytics_platform.storyline import (
    StorylineContent, StorylineTurn, CodeAppendixEntry, assemble_storyline,
)


def _msg(answer_id, question="Q", answer="A", facts=None, caveats=None,
         queries_run=None, python_cells=None, produced_df_label=""):
    return {
        "answer_id": answer_id, "question": question, "answer": answer,
        "facts": facts or [], "caveats": caveats or [],
        "queries_run": queries_run or [], "python_cells": python_cells or [],
        "produced_df_label": produced_df_label, "created_at": "2026-08-15T00:00:00Z",
    }


class TestAssembleStoryline(unittest.TestCase):
    def test_only_selected_turns_appear_in_order(self):
        conv = {"id": "c1", "title": "Test", "messages": [
            _msg("a1", question="First"), _msg("a2", question="Second"),
            _msg("a3", question="Third"),
        ]}
        content = assemble_storyline(conv, ["a3", "a1"])
        self.assertEqual([t.question for t in content.turns], ["First", "Third"])

    def test_empty_selection_yields_empty_content(self):
        conv = {"id": "c1", "title": "Test", "messages": [_msg("a1")]}
        content = assemble_storyline(conv, [])
        self.assertEqual(content.turns, [])
        self.assertEqual(content.code_appendix, [])
        self.assertEqual(content.estimated_tokens, 0)
        self.assertFalse(content.over_budget)

    def test_selected_sql_turn_adds_its_query_as_non_dependency_appendix_entry(self):
        conv = {"id": "c1", "title": "Test", "messages": [
            _msg("a1", queries_run=["SELECT 1"], produced_df_label="df_1"),
        ]}
        content = assemble_storyline(conv, ["a1"])
        self.assertEqual(len(content.code_appendix), 1)
        entry = content.code_appendix[0]
        self.assertEqual(entry.kind, "sql")
        self.assertEqual(entry.code, "SELECT 1")
        self.assertEqual(entry.source_answer_id, "a1")
        self.assertFalse(entry.is_dependency)

    def test_selected_python_turn_pulls_in_unselected_producing_turn_as_dependency(self):
        conv = {"id": "c1", "title": "Test", "messages": [
            _msg("a1", question="SQL turn", queries_run=["SELECT revenue FROM events"],
                 produced_df_label="df_1"),
            _msg("a2", question="Python turn",
                 python_cells=[{"code": "result = df_1['revenue'].sum()",
                                "df_label": "df_1", "result_summary": 6}]),
        ]}
        content = assemble_storyline(conv, ["a2"])  # a1 NOT selected
        self.assertEqual([t.question for t in content.turns], ["Python turn"])
        kinds = {(e.kind, e.source_answer_id, e.is_dependency) for e in content.code_appendix}
        self.assertIn(("python", "a2", False), kinds)
        self.assertIn(("sql", "a1", True), kinds)

    def test_dependency_turn_that_is_also_selected_is_not_double_marked(self):
        conv = {"id": "c1", "title": "Test", "messages": [
            _msg("a1", queries_run=["SELECT 1"], produced_df_label="df_1"),
            _msg("a2", python_cells=[{"code": "result = 1", "df_label": "df_1",
                                      "result_summary": 1}]),
        ]}
        content = assemble_storyline(conv, ["a1", "a2"])
        sql_entries = [e for e in content.code_appendix if e.kind == "sql"]
        self.assertEqual(len(sql_entries), 1)
        self.assertFalse(sql_entries[0].is_dependency)

    def test_token_estimate_and_budget_flag(self):
        conv = {"id": "c1", "title": "Test", "messages": [
            _msg("a1", question="Q" * 100, answer="A" * 100),
        ]}
        content = assemble_storyline(conv, ["a1"])
        self.assertGreater(content.estimated_tokens, 0)
        self.assertFalse(content.over_budget)

        big_conv = {"id": "c1", "title": "Test", "messages": [
            _msg("a1", answer="x" * 250_000),
        ]}
        big_content = assemble_storyline(big_conv, ["a1"])
        self.assertTrue(big_content.over_budget)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_storyline.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'analytics_platform.storyline'`).

- [ ] **Step 3: Implement `analytics_platform/storyline.py`**

```python
"""Pure, DB-free assembly of a selective export ("storyline") from an already-fetched
stakeholder conversation dict. No I/O here -- Task 3/4's renderers and the API layer
own fetching and formatting; this module only decides WHAT goes into the export and
resolves the Code Appendix's cross-turn dependencies.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List

CHARS_PER_TOKEN_ESTIMATE = 4
WARN_TOKEN_THRESHOLD = 50_000


@dataclass
class StorylineTurn:
    answer_id: str
    question: str
    answer: str
    facts: List[str]
    caveats: List[str]
    created_at: str


@dataclass
class CodeAppendixEntry:
    label: str          # df_label this code relates to, "" for a plain SQL-only turn
    kind: str            # "sql" | "python"
    code: str
    source_answer_id: str
    is_dependency: bool  # True if pulled in only because a selected turn needs it


@dataclass
class StorylineContent:
    conversation_title: str
    turns: List[StorylineTurn] = field(default_factory=list)
    code_appendix: List[CodeAppendixEntry] = field(default_factory=list)
    estimated_tokens: int = 0
    over_budget: bool = False


def assemble_storyline(conversation: Dict[str, Any], answer_ids: List[str]) -> StorylineContent:
    id_order = {aid: i for i, aid in enumerate(answer_ids)}
    all_messages = conversation.get("messages", [])
    by_id = {m["answer_id"]: m for m in all_messages}
    label_to_message = {
        m["produced_df_label"]: m for m in all_messages if m.get("produced_df_label")
    }

    selected = sorted(
        (m for m in all_messages if m["answer_id"] in id_order),
        key=lambda m: id_order[m["answer_id"]],
    )

    turns = [StorylineTurn(
        answer_id=m["answer_id"], question=m["question"], answer=m["answer"],
        facts=list(m.get("facts", [])), caveats=list(m.get("caveats", [])),
        created_at=m.get("created_at", ""),
    ) for m in selected]

    selected_ids = set(id_order)
    appendix: List[CodeAppendixEntry] = []
    dependency_answer_ids_added: set = set()

    for m in selected:
        for q in m.get("queries_run", []):
            appendix.append(CodeAppendixEntry(
                label=m.get("produced_df_label", ""), kind="sql", code=q,
                source_answer_id=m["answer_id"], is_dependency=False))
        for p in m.get("python_cells", []):
            appendix.append(CodeAppendixEntry(
                label=p.get("df_label", ""), kind="python", code=p.get("code", ""),
                source_answer_id=m["answer_id"], is_dependency=False))
            dep_msg = label_to_message.get(p.get("df_label"))
            if (dep_msg is not None
                    and dep_msg["answer_id"] not in selected_ids
                    and dep_msg["answer_id"] not in dependency_answer_ids_added):
                for q in dep_msg.get("queries_run", []):
                    appendix.append(CodeAppendixEntry(
                        label=p.get("df_label", ""), kind="sql", code=q,
                        source_answer_id=dep_msg["answer_id"], is_dependency=True))
                dependency_answer_ids_added.add(dep_msg["answer_id"])

    estimate_text = "\n".join(
        t.question + t.answer + " ".join(t.facts) + " ".join(t.caveats) for t in turns
    ) + "\n".join(e.code for e in appendix)
    estimated_tokens = len(estimate_text) // CHARS_PER_TOKEN_ESTIMATE

    return StorylineContent(
        conversation_title=conversation.get("title", ""),
        turns=turns, code_appendix=appendix,
        estimated_tokens=estimated_tokens,
        over_budget=estimated_tokens > WARN_TOKEN_THRESHOLD,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_storyline.py -v`
Expected: PASS (all 6 tests).

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/storyline.py tests/test_storyline.py
git commit -m "feat(storyline): pure content assembly with dependency-tracked code appendix"
```

---

### Task 3: Markdown renderer

**Files:**
- Modify: `analytics_platform/storyline.py`
- Test: `tests/test_storyline.py`

**Interfaces:**
- Consumes: `StorylineContent` (Task 2).
- Produces: `render_markdown(content: StorylineContent) -> str`.

- [ ] **Step 1: Write the failing test**

```python
    def test_render_markdown_includes_turns_and_dependency_annotated_appendix(self):
        content = StorylineContent(
            conversation_title="Q3 Funnel Review",
            turns=[StorylineTurn(answer_id="a1", question="Why did signups drop?",
                                  answer="Signups dropped 12% after the consent page.",
                                  facts=["computed via SQL"], caveats=[],
                                  created_at="2026-08-15T00:00:00Z")],
            code_appendix=[
                CodeAppendixEntry(label="df_1", kind="sql", code="SELECT 1",
                                  source_answer_id="a0", is_dependency=True),
                CodeAppendixEntry(label="df_1", kind="python", code="result = 1",
                                  source_answer_id="a1", is_dependency=False),
            ],
            estimated_tokens=42, over_budget=False,
        )
        md = render_markdown(content)
        self.assertIn("# Q3 Funnel Review", md)
        self.assertIn("Why did signups drop?", md)
        self.assertIn("Signups dropped 12%", md)
        self.assertIn("```sql", md)
        self.assertIn("```python", md)
        self.assertIn("(included as a dependency of df_1)", md)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_storyline.py -k render_markdown -v`
Expected: FAIL (`NameError`/`ImportError: render_markdown`).

- [ ] **Step 3: Implement `render_markdown`**

Add to `analytics_platform/storyline.py`:

```python
def render_markdown(content: StorylineContent) -> str:
    lines = [f"# {content.conversation_title or 'Storyline Export'}", ""]
    for t in content.turns:
        lines.append(f"## {t.question}")
        lines.append("")
        lines.append(t.answer)
        if t.facts:
            lines.append("")
            lines.append("**Facts:** " + "; ".join(t.facts))
        if t.caveats:
            lines.append("")
            lines.append("**Caveats:** " + "; ".join(t.caveats))
        lines.append("")
    if content.code_appendix:
        lines.append("## Code Appendix")
        lines.append("")
        for e in content.code_appendix:
            heading = f"### {e.label or e.source_answer_id} ({e.kind})"
            if e.is_dependency:
                heading += f" — (included as a dependency of {e.label})"
            lines.append(heading)
            lines.append(f"```{e.kind}")
            lines.append(e.code)
            lines.append("```")
            lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_storyline.py -k render_markdown -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/storyline.py tests/test_storyline.py
git commit -m "feat(storyline): Markdown renderer"
```

---

### Task 4: Word (`.docx`) renderer

**Files:**
- Modify: `requirements.txt`, `requirements-advanced.txt`
- Modify: `analytics_platform/storyline.py`
- Test: `tests/test_storyline.py`

**Interfaces:**
- Consumes: `StorylineContent` (Task 2), same as Task 3.
- Produces: `render_docx(content: StorylineContent) -> bytes`.

- [ ] **Step 1: Add the dependency**

In both `requirements.txt` and `requirements-advanced.txt`, add a line:

```
python-docx==1.2.0
```

Install it: `.venv/bin/pip install python-docx==1.2.0`

- [ ] **Step 2: Write the failing test**

```python
    def test_render_docx_produces_a_valid_document_with_turns_and_appendix(self):
        import io
        import docx

        content = StorylineContent(
            conversation_title="Q3 Funnel Review",
            turns=[StorylineTurn(answer_id="a1", question="Why did signups drop?",
                                  answer="Signups dropped 12% after the consent page.",
                                  facts=[], caveats=[], created_at="2026-08-15T00:00:00Z")],
            code_appendix=[CodeAppendixEntry(label="df_1", kind="sql", code="SELECT 1",
                                             source_answer_id="a1", is_dependency=False)],
            estimated_tokens=10, over_budget=False,
        )
        data = render_docx(content)
        self.assertIsInstance(data, bytes)
        doc = docx.Document(io.BytesIO(data))
        full_text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("Q3 Funnel Review", full_text)
        self.assertIn("Why did signups drop?", full_text)
        self.assertIn("Signups dropped 12%", full_text)
        self.assertIn("Code Appendix", full_text)
        self.assertIn("SELECT 1", full_text)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_storyline.py -k render_docx -v`
Expected: FAIL (`NameError`/`ImportError: render_docx`, or `ModuleNotFoundError: docx`
if Step 1 was skipped).

- [ ] **Step 4: Implement `render_docx`**

Add to `analytics_platform/storyline.py` (import at module top, alongside the existing
imports):

```python
import io

import docx
```

```python
def render_docx(content: StorylineContent) -> bytes:
    doc = docx.Document()
    doc.add_heading(content.conversation_title or "Storyline Export", level=1)
    for t in content.turns:
        doc.add_heading(t.question, level=2)
        doc.add_paragraph(t.answer)
        if t.facts:
            doc.add_paragraph("Facts: " + "; ".join(t.facts))
        if t.caveats:
            doc.add_paragraph("Caveats: " + "; ".join(t.caveats))
    if content.code_appendix:
        doc.add_heading("Code Appendix", level=1)
        for e in content.code_appendix:
            heading = f"{e.label or e.source_answer_id} ({e.kind})"
            if e.is_dependency:
                heading += f" — included as a dependency of {e.label}"
            doc.add_heading(heading, level=3)
            code_para = doc.add_paragraph(e.code)
            code_para.style = doc.styles["Normal"]
            for run in code_para.runs:
                run.font.name = "Courier New"
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_storyline.py -k render_docx -v`
Expected: PASS.

- [ ] **Step 6: Run the full storyline test file and the full suite**

Run: `.venv/bin/python -m pytest tests/test_storyline.py -v`
Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass, zero regressions.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt requirements-advanced.txt analytics_platform/storyline.py tests/test_storyline.py
git commit -m "feat(storyline): Word (.docx) renderer via python-docx"
```

---

### Task 5: API endpoint — `POST /stakeholder/{tenant_id}/conversations/{conversation_id}/export`

**Files:**
- Modify: `analytics_platform/api.py`
- Test: `tests/test_api.py` (or wherever the existing `/stakeholder/.../conversations`
  route tests live — search for `stakeholder_get_conversation`/
  `test_.*conversation` in the API test file to confirm the exact filename and the
  existing test-client fixture pattern before writing new tests, matching that pattern
  exactly rather than guessing)

**Interfaces:**
- Consumes: `assemble_storyline`, `render_markdown`, `render_docx` (Tasks 2-4);
  `C.stakeholder.get_conversation` (existing).
- Produces: the export HTTP endpoint. Response body is the file's raw bytes; headers
  carry `Content-Type` and `Content-Disposition` so a browser treats it as a download.

- [ ] **Step 1: Write the failing tests**

First, locate the existing test file and fixture for API-level conversation route tests
(search for `stakeholder_get_conversation` or `/conversations` in the test suite) and
match its exact `TestClient`/fixture setup style. Using that style, add:

```python
    def test_export_markdown_returns_a_markdown_document(self):
        # ... use this test file's existing pattern to create a tenant, seed a
        # conversation with at least one SQL-path answer (matching how other API
        # tests in this file already drive StakeholderService.answer or seed
        # stakeholder_answers rows directly), then:
        resp = client.post(
            f"/stakeholder/{tid}/conversations/{conv_id}/export",
            json={"answer_ids": [answer_id], "format": "markdown"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/markdown", resp.headers["content-type"])
        self.assertIn("attachment", resp.headers["content-disposition"])

    def test_export_docx_returns_an_openxml_document(self):
        resp = client.post(
            f"/stakeholder/{tid}/conversations/{conv_id}/export",
            json={"answer_ids": [answer_id], "format": "docx"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("officedocument.wordprocessingml.document", resp.headers["content-type"])

    def test_export_unknown_conversation_is_404(self):
        resp = client.post(
            f"/stakeholder/{tid}/conversations/does-not-exist/export",
            json={"answer_ids": ["x"], "format": "markdown"})
        self.assertEqual(resp.status_code, 404)

    def test_export_empty_answer_ids_is_400(self):
        resp = client.post(
            f"/stakeholder/{tid}/conversations/{conv_id}/export",
            json={"answer_ids": [], "format": "markdown"})
        self.assertEqual(resp.status_code, 400)
```

(This is a draft shape, not exact code — the real test file's tenant/client/conversation
setup conventions must be read first and matched. This is expected and normal for this
task, per this plan's established pattern in every prior API-touching task.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_api.py -k export -v` (adjust filename to
whatever Step 1 found)
Expected: FAIL (404 route not found, since the endpoint doesn't exist yet).

- [ ] **Step 3: Implement the endpoint**

In `analytics_platform/api.py`, add `Response` to the existing fastapi import:

```python
from fastapi import FastAPI, HTTPException, Header, Query, Request, Response, WebSocket, WebSocketDisconnect
```

Add a new Pydantic model near `ConversationPatchIn`:

```python
class StorylineExportIn(BaseModel):
    answer_ids: List[str]
    format: str = "markdown"   # "markdown" | "docx"
```

Add the route near the other `/conversations/{conversation_id}` routes, and add the
`storyline` import near the top of the file alongside the other `analytics_platform.*`
imports:

```python
from analytics_platform.storyline import assemble_storyline, render_markdown, render_docx
```

```python
    @app.post("/stakeholder/{tenant_id}/conversations/{conversation_id}/export")
    def stakeholder_export_storyline(tenant_id: str, conversation_id: str,
                                     body: StorylineExportIn) -> Response:
        tenant_or_404(tenant_id)
        conv = C.stakeholder.get_conversation(tenant_id, conversation_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        if not body.answer_ids:
            raise HTTPException(status_code=400, detail="answer_ids must not be empty")
        known_ids = {m["answer_id"] for m in conv["messages"]}
        unknown = [a for a in body.answer_ids if a not in known_ids]
        if unknown:
            raise HTTPException(status_code=400,
                                detail=f"unknown answer_id(s): {unknown}")
        content = assemble_storyline(conv, body.answer_ids)
        title_slug = "".join(c if c.isalnum() else "-" for c in (conv["title"] or "storyline")).strip("-") or "storyline"
        if body.format == "docx":
            data = render_docx(content)
            media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            filename = f"{title_slug}.docx"
        elif body.format == "markdown":
            data = render_markdown(content).encode("utf-8")
            media_type = "text/markdown"
            filename = f"{title_slug}.md"
        else:
            raise HTTPException(status_code=400,
                                detail=f"unsupported format: {body.format!r}")
        return Response(content=data, media_type=media_type,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_api.py -k export -v`
Expected: PASS.

- [ ] **Step 5: Run the full backend suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass, zero regressions.

- [ ] **Step 6: Commit**

```bash
git add analytics_platform/api.py tests/test_api.py
git commit -m "feat(api): storyline export endpoint (markdown/docx)"
```

---

### Task 6: Frontend — Report Builder panel (selection UI + client-side token estimate)

**Files:**
- Modify: `frontend/src/store/useStore.ts`
- Modify: `frontend/src/components/StakeholderChat.tsx`

**Interfaces:**
- Consumes: `StakeholderMessage` (existing, has `facts`? — check: the current
  `StakeholderMessage` type in `useStore.ts` does NOT list `facts`/`caveats`/
  `produced_df_label` — add `facts: string[]; caveats: string[]; produced_df_label?:
  string;` to the type so the panel and the later export call can use them).
- Produces: `stakeholder.reportBuilderOpen: boolean`,
  `stakeholder.selectedAnswerIds: string[]` (new store fields); `toggleReportBuilder()`,
  `toggleAnswerSelected(id: string)`, `selectAllAnswers()`, `clearSelectedAnswers()`
  (new store actions). Task 7 consumes these plus adds `exportStoryline`.

- [ ] **Step 1: Extend `StakeholderMessage` and add store state/actions**

In `frontend/src/store/useStore.ts`, extend the type:

```typescript
export type StakeholderMessage = {
  answer_id: string; question: string; answer: string; answer_mode: string;
  status: string; citations: any[]; caveats: string[]; facts: string[];
  queries_run: string[]; escalated: boolean; cost: number; created_at: string;
  chart_config?: any; chart_data?: any[]; feedback?: 'up' | 'down';
  python_cells?: Array<{ code: string; df_label: string; result_summary: unknown }>;
  produced_df_label?: string;
};
```

(`caveats`/`facts` were already present — only `produced_df_label` is new on the type.)

Add to the `stakeholder` slice's shape (alongside `messages: StakeholderMessage[]`):

```typescript
    reportBuilderOpen: boolean;
    selectedAnswerIds: string[];
```

Add to `AppState`, alongside `submitFeedback`:

```typescript
  toggleReportBuilder: () => void;
  toggleAnswerSelected: (answerId: string) => void;
  selectAllAnswers: () => void;
  clearSelectedAnswers: () => void;
```

Add to the store implementation (mirroring how `setStakeholder`/other actions read
`get().stakeholder` and call `set(...)` — read the existing implementation of e.g.
`starConversation` first to match the exact `set`/`get` idiom used in this file):

```typescript
  toggleReportBuilder: () => set(state => ({
    stakeholder: { ...state.stakeholder, reportBuilderOpen: !state.stakeholder.reportBuilderOpen },
  })),
  toggleAnswerSelected: (answerId) => set(state => {
    const cur = state.stakeholder.selectedAnswerIds;
    const next = cur.includes(answerId) ? cur.filter(id => id !== answerId) : [...cur, answerId];
    return { stakeholder: { ...state.stakeholder, selectedAnswerIds: next } };
  }),
  selectAllAnswers: () => set(state => ({
    stakeholder: {
      ...state.stakeholder,
      selectedAnswerIds: state.stakeholder.messages.map(m => m.answer_id).filter(Boolean),
    },
  })),
  clearSelectedAnswers: () => set(state => ({
    stakeholder: { ...state.stakeholder, selectedAnswerIds: [] },
  })),
```

Initialize `reportBuilderOpen: false, selectedAnswerIds: []` in the slice's initial
state object (alongside the existing `conversations: [], activeConversationId: ''`,
etc.) and in `startNewConversation`'s reset (if that action resets `messages` to `[]`,
also reset `selectedAnswerIds` to `[]` there, so switching conversations doesn't carry
over stale selections from a different thread).

- [ ] **Step 2: Build the Report Builder panel**

In `frontend/src/components/StakeholderChat.tsx`, add a new component above
`StakeholderChat`:

```tsx
function estimateTokens(messages: StakeholderMessage[], selectedIds: string[]): number {
  const text = messages
    .filter(m => selectedIds.includes(m.answer_id))
    .map(m => m.question + m.answer + (m.facts || []).join(' ') + (m.caveats || []).join(' '))
    .join('\n');
  return Math.floor(text.length / 4);
}

const WARN_TOKEN_THRESHOLD = 50_000; // must match analytics_platform/storyline.py's WARN_TOKEN_THRESHOLD

function ReportBuilderPanel() {
  const { messages, selectedAnswerIds } = useStore(state => state.stakeholder);
  const toggleAnswerSelected = useStore(state => state.toggleAnswerSelected);
  const selectAllAnswers = useStore(state => state.selectAllAnswers);
  const clearSelectedAnswers = useStore(state => state.clearSelectedAnswers);
  const exportStoryline = useStore(state => state.exportStoryline);
  const [format, setFormat] = useState<'markdown' | 'docx'>('markdown');
  const [exporting, setExporting] = useState(false);

  const estimated = estimateTokens(messages, selectedAnswerIds);
  const overBudget = estimated > WARN_TOKEN_THRESHOLD;

  return (
    <div style={{ width: '300px', flexShrink: 0, borderLeft: '1px solid rgba(255,255,255,0.08)', display: 'flex', flexDirection: 'column', height: '100%', padding: '1rem' }}>
      <h3 style={{ marginBottom: '0.75rem' }}>Report Builder</h3>
      <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '0.75rem' }}>
        <button onClick={selectAllAnswers} style={{ fontSize: '0.8rem', padding: '0.3rem 0.6rem', background: 'none', border: '1px solid rgba(255,255,255,0.15)', borderRadius: '6px', color: 'var(--text-secondary)', cursor: 'pointer' }}>Select all</button>
        <button onClick={clearSelectedAnswers} style={{ fontSize: '0.8rem', padding: '0.3rem 0.6rem', background: 'none', border: '1px solid rgba(255,255,255,0.15)', borderRadius: '6px', color: 'var(--text-secondary)', cursor: 'pointer' }}>Clear</button>
      </div>
      <div style={{ flex: 1, overflowY: 'auto' }}>
        {messages.filter(m => m.answer_id).map(m => (
          <label key={m.answer_id} style={{ display: 'flex', gap: '0.5rem', alignItems: 'flex-start', marginBottom: '0.6rem', fontSize: '0.85rem', cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={selectedAnswerIds.includes(m.answer_id)}
              onChange={() => toggleAnswerSelected(m.answer_id)}
            />
            <span style={{ color: 'var(--text-secondary)' }}>{m.question}</span>
          </label>
        ))}
      </div>
      <div style={{ fontSize: '0.8rem', color: overBudget ? 'var(--error)' : 'var(--text-muted)', marginBottom: '0.5rem' }}>
        ~{estimated.toLocaleString()} estimated tokens
        {overBudget && ' — this is a large export, consider selecting fewer turns'}
      </div>
      <select value={format} onChange={e => setFormat(e.target.value as 'markdown' | 'docx')} style={{ marginBottom: '0.5rem', padding: '0.4rem', background: 'rgba(0,0,0,0.2)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', color: '#fff' }}>
        <option value="markdown">Markdown</option>
        <option value="docx">Word (.docx)</option>
      </select>
      <button
        disabled={selectedAnswerIds.length === 0 || exporting}
        onClick={async () => { setExporting(true); try { await exportStoryline(format); } finally { setExporting(false); } }}
        style={{ background: 'var(--accent-primary)', padding: '0.6rem', borderRadius: '8px', border: 'none', color: '#fff', cursor: 'pointer', fontWeight: 600 }}
      >
        {exporting ? 'Exporting…' : `Export (${selectedAnswerIds.length})`}
      </button>
    </div>
  );
}
```

Add `import { useState } from 'react';` is already present (the file already imports
`useState`). Add the panel to `StakeholderChat`'s render, toggled by a header button:

```tsx
export function StakeholderChat() {
  const { question, loading, messages, reportBuilderOpen } = useStore(state => state.stakeholder);
  const setStakeholder = useStore(state => state.setStakeholder);
  const askStakeholder = useStore(state => state.askStakeholder);
  const submitFeedback = useStore(state => state.submitFeedback);
  const toggleReportBuilder = useStore(state => state.toggleReportBuilder);
  const threadEndRef = useRef<HTMLDivElement>(null);
```

And inside the returned JSX, add a toggle button near the top of the main column (e.g.
right after the opening `<div style={{ flex: 1, display: 'flex', flexDirection:
'column', minWidth: 0 }}>`) and the panel itself as a sibling of that column:

```tsx
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <div style={{ display: 'flex', justifyContent: 'flex-end', padding: '0.75rem 1.5rem 0' }}>
          <button onClick={toggleReportBuilder} style={{ fontSize: '0.85rem', padding: '0.4rem 0.8rem', background: 'none', border: '1px solid rgba(255,255,255,0.15)', borderRadius: '6px', color: 'var(--text-secondary)', cursor: 'pointer' }}>
            {reportBuilderOpen ? 'Hide' : 'Report Builder'}
          </button>
        </div>
        {/* ... existing thread + input div unchanged ... */}
      </div>
      {reportBuilderOpen && <ReportBuilderPanel />}
```

- [ ] **Step 3: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: this will FAIL at this point — `exportStoryline` doesn't exist on the store
yet (Task 7 adds it). That's expected; note it and proceed, or stub a no-op
`exportStoryline: async () => {}` in the store now so this task's type-check passes
cleanly on its own, then Task 7 replaces the stub with the real implementation. Prefer
the stub — it keeps every task's own type-check green independently.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/store/useStore.ts frontend/src/components/StakeholderChat.tsx
git commit -m "feat(frontend): Report Builder panel with turn selection and token estimate"
```

---

### Task 7: Frontend — wire `exportStoryline` to the backend and trigger a download

**Files:**
- Modify: `frontend/src/store/useStore.ts`

**Interfaces:**
- Consumes: `POST /stakeholder/{tenant_id}/conversations/{conversation_id}/export`
  (Task 5); `stakeholder.selectedAnswerIds`, `stakeholder.activeConversationId`
  (existing/Task 6).
- Produces: real `exportStoryline(format: 'markdown' | 'docx') -> Promise<void>`,
  replacing Task 6's stub.

- [ ] **Step 1: Implement `exportStoryline`**

Replace the Task 6 stub in the store implementation with (matching the existing
fetch-action style used by `deleteConversation`/`submitFeedback` in this same file —
read one of those first for the exact `get()`/`tenantId` access pattern):

```typescript
  exportStoryline: async (format) => {
    const { tenantId } = get();
    const { activeConversationId, selectedAnswerIds } = get().stakeholder;
    if (!activeConversationId || selectedAnswerIds.length === 0) return;
    const res = await fetch(
      `http://localhost:8000/stakeholder/${tenantId}/conversations/${activeConversationId}/export`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answer_ids: selectedAnswerIds, format }),
      });
    if (!res.ok) return;
    const blob = await res.blob();
    const disposition = res.headers.get('content-disposition') || '';
    const match = disposition.match(/filename="([^"]+)"/);
    const filename = match ? match[1] : `storyline.${format === 'docx' ? 'docx' : 'md'}`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  },
```

Update the `exportStoryline` entry in `AppState`'s type to match the real signature:
`exportStoryline: (format: 'markdown' | 'docx') => Promise<void>;`

- [ ] **Step 2: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Manual verification**

There is no automated browser download-observation tool in this environment (per the
established pattern from plan 1's Task 9 and plan 2's Task 8, live end-to-end flows are
verified manually). With the dev servers running, verify the WIRING without needing a
real file save dialog: in the browser console (or a small injected script), spy on
`URL.createObjectURL` (`const orig = URL.createObjectURL; URL.createObjectURL = (b) =>
{ console.log('blob type', b.type, 'size', b.size); return orig(b); };`), open a
conversation with at least one answered turn, open the Report Builder panel, check a
turn, click Export, and confirm the console log fires with a non-zero `size` and the
expected `type` (`text/markdown` or the docx MIME type depending on the format picked).
This confirms the fetch → blob → download-trigger chain works without depending on the
browser's actual file-save UI, which automated tools cannot observe.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/store/useStore.ts
git commit -m "feat(frontend): wire storyline export to the backend, trigger browser download"
```

---

### Task 8: End-to-end test — dependency tracking survives a real two-turn conversation

**Files:**
- Test: `tests/test_stakeholder.py` (or `tests/test_storyline.py` — whichever already
  has access to a full `answer()`-driven conversation fixture; prefer
  `test_stakeholder.py` since it already has the `self.ctx`/`self.tid` conversation
  fixtures this test needs)

**Interfaces:**
- Consumes: `answer()` (existing, now sets `produced_df_label` per Task 1),
  `get_conversation()` (existing), `assemble_storyline` (Task 2).
- Produces: one integration test proving the whole chain — SQL turn → cached DataFrame
  → Python turn → export selecting ONLY the Python turn still pulls the SQL turn's
  query into the Code Appendix as a dependency.

- [ ] **Step 1: Write the failing test**

```python
    def test_export_of_python_only_turn_pulls_in_its_sql_dependency(self):
        import pandas as pd
        from analytics_platform.storyline import assemble_storyline

        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [
            MagicMock(text='{"category": "metric_lookup"}', tokens_in=5, tokens_out=5),
            MagicMock(text="SELECT revenue FROM events", tokens_in=10, tokens_out=5),
            MagicMock(text='{"answer": "here is the revenue"}', tokens_in=10, tokens_out=5),
        ]
        with patch("analytics_platform.stakeholder.make_role_client", return_value=mock_llm):
            self.ctx.tenants.set_analyst_config(
                self.tid, {"stakeholder": {"enabled": True, "provider": "mock", "model": "mock"}})
            self.ctx.stakeholder.add_datasource(
                self.tid, "wh", "athena", "public", tables=["events"])
            turn1 = self.ctx.stakeholder.answer(self.tid, "what's the revenue", conversation_id="")

        conv_id = turn1["conversation_id"]
        mock_llm.generate.side_effect = [
            MagicMock(text='{"category": "metric_lookup"}', tokens_in=5, tokens_out=5),
            MagicMock(text='{"path": "python", "df_label": "df_1"}', tokens_in=5, tokens_out=5),
            MagicMock(text="```python\nresult = int(df_1['revenue'].sum())\n```",
                      tokens_in=10, tokens_out=5),
            MagicMock(text='{"answer": "the total is 6"}', tokens_in=10, tokens_out=5),
        ]
        with patch("analytics_platform.stakeholder.make_role_client", return_value=mock_llm):
            turn2 = self.ctx.stakeholder.answer(
                self.tid, "what's the total", conversation_id=conv_id)

        conv = self.ctx.stakeholder.get_conversation(self.tid, conv_id)
        content = assemble_storyline(conv, [turn2["answer_id"]])  # only the Python turn

        self.assertEqual([t.answer_id for t in content.turns], [turn2["answer_id"]])
        sql_entries = [e for e in content.code_appendix if e.kind == "sql"]
        self.assertEqual(len(sql_entries), 1)
        self.assertTrue(sql_entries[0].is_dependency)
        self.assertEqual(sql_entries[0].source_answer_id, turn1["answer_id"])
```

(As with Task 1 and every prior multi-turn test in this plan and the B2 plan before it,
verify the mocked intent-classification/SQL text against real passing tests in this
file first, and adjust the SQL/column names to match the actual test fixture's
warehouse schema — the B2 plan's Task 9 already discovered the fixture uses an
`events` table with a `revenue` column, not `orders`/`amount`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -k "export_of_python_only_turn" -v`
Expected: FAIL if Task 1 isn't correctly wired (no `produced_df_label` on turn 1, so the
dependency lookup finds nothing). If Tasks 1-2 are correctly integrated, this may PASS
immediately — in that case, treat it as a confirming integration test rather than
forcing an artificial failure; note this in the report rather than fabricating a
red step.

- [ ] **Step 3: Fix only if it fails**

If the test fails, the bug is almost certainly in Task 1's wiring (a turn's
`produced_df_label` not actually surviving the round-trip through `_record()` →
`get_conversation()`), not in this test. Diagnose against Task 1's implementation before
changing anything; this task should not need new production code if Tasks 1-7 are
correctly integrated.

- [ ] **Step 4: Run the full backend suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass, zero regressions.

- [ ] **Step 5: Commit**

```bash
git add tests/test_stakeholder.py
git commit -m "test(storyline): e2e proof that Python-only export pulls in its SQL dependency"
```

---
