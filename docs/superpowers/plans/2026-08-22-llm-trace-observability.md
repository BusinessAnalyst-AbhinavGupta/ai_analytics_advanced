# LLM + Retrieval Trace Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist every LLM call and every brain retrieval in a turn, verbatim, and expose them per answer.

**Architecture:** Wrap the boundaries rather than the call sites. A `TracingLLMClient` wraps whatever `make_role_client` returns; `CompanyBrain.search` is instrumented in place. Two contextvars carry the ambient turn state: `_stage` (set by `_step()`, which already fires at every named boundary) and `_sink` (set once per turn, holding the tenant store + trace_id). Records land in a new tenant-scoped `llm_traces` table keyed by the `trace_id` that `answer()` already mints.

**Tech Stack:** Python 3.14, SQLite via `analytics_platform.database.Store`, FastAPI, `unittest` (run under pytest), Next.js + vitest for the UI task.

## Global Constraints

- **Tracing must never break a turn.** Every write path catches `Exception`, logs, and continues. A dead trace write degrades to a missing record, never a failed answer.
- **The wrapper returns the real response before it records.** A recorder crash cannot swallow a good `LLMResponse`.
- **No redaction.** Prompts embed the tenant's own warehouse data and are written to that tenant's own database.
- **Truncate loudly, never silently.** Fields cap at 64,000 chars with `"<field>_truncated": true` and `"<field>_len": <original>` in the payload.
- **Isolation is per-database.** Traces go to `stores.for_tenant(tenant_id)`, never a shared file, never a `WHERE tenant_id` filter on a shared table.
- **`answer()`'s returned payload must not change.** Same equivalence `tests/test_answer_stream.py` already pins for the generator refactor.
- Tests are `unittest.TestCase` classes run under pytest. Run with `.venv/bin/python -m pytest`.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Work directly on `main`. Do not create branches or worktrees.

## Spec deviation resolved here

The spec says the junior and senior analysts get capture "for free" from wrapping
`make_role_client`. Mapping the code showed that is only half true:
`make_role_client(settings, role_ai)` (`llm/client.py:94`) receives **no tenant_id
and no Store**, so the wrapper has nowhere to write on its own.

Resolution: the wrapper reads an ambient `_sink` contextvar. Junior and senior are
therefore *wrapped* for free but record nothing until they set a sink — a two-line
change each, deliberately out of scope here. Task 2 asserts this explicitly so the
behaviour is pinned rather than assumed.

---

### Task 1: The tracing module and its table

**Files:**
- Create: `analytics_platform/tracing.py`
- Modify: `analytics_platform/database.py` (add `llm_traces` to `TENANT_SCHEMA`, after the `telemetry` table at ~line 106)
- Test: `tests/test_tracing.py`

**Interfaces:**
- Consumes: `analytics_platform.database.Store`, `dump_json`; `analytics_platform.domain.now_iso`
- Produces:
  - `TraceSink(store: Store, tenant_id: str, trace_id: str, max_field: int = 64_000)`
  - `TraceSink.record(kind: str, payload: dict, *, duration_ms: float = 0.0, tokens_in: int = 0, tokens_out: int = 0, ok: bool = True) -> None`
  - `set_stage(stage: str) -> Token`, `current_stage() -> str`
  - `use_sink(sink: Optional[TraceSink]) -> Token`, `current_sink() -> Optional[TraceSink]`
  - `record(kind: str, payload: dict, **kw) -> None` — module-level, no-ops when no sink
  - `clip(value: str, limit: int) -> tuple[str, bool, int]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_tracing.py`:

```python
"""The trace sink: what it writes, what it clips, and what it refuses to break.

The single most important assertion in this file is
`test_a_failing_write_does_not_raise`. Tracing is an observability feature
bolted onto the answer path; the moment it can take a turn down it has cost
more than it is worth.
"""
from __future__ import annotations

import json
import tempfile
import unittest

from analytics_platform.database import TENANT_SCHEMA, Store
from analytics_platform import tracing


class TraceSinkTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(f"{self._tmp.name}/t.db", schema=TENANT_SCHEMA)
        self.sink = tracing.TraceSink(self.store, "tnt_x", "trace-1")

    def tearDown(self):
        self._tmp.cleanup()

    def rows(self):
        return self.store.query_all(
            "SELECT trace_id, seq, stage, kind, payload, tokens_in, ok "
            "FROM llm_traces ORDER BY seq")

    def test_record_writes_a_row_with_an_incrementing_seq(self):
        self.sink.record("llm", {"prompt": "a"})
        self.sink.record("llm", {"prompt": "b"})
        rows = self.rows()
        self.assertEqual([r["seq"] for r in rows], [1, 2])
        self.assertEqual([r["trace_id"] for r in rows], ["trace-1", "trace-1"])
        self.assertEqual(json.loads(rows[0]["payload"])["prompt"], "a")

    def test_record_stamps_the_current_stage(self):
        token = tracing.set_stage("planning")
        try:
            self.sink.record("llm", {"prompt": "a"})
        finally:
            tracing.reset_stage(token)
        self.assertEqual(self.rows()[0]["stage"], "planning")

    def test_stage_defaults_to_unattributed(self):
        self.sink.record("llm", {"prompt": "a"})
        self.assertEqual(self.rows()[0]["stage"], "unattributed")

    def test_long_fields_are_clipped_and_marked(self):
        sink = tracing.TraceSink(self.store, "tnt_x", "trace-1", max_field=10)
        sink.record("llm", {"prompt": "x" * 50})
        payload = json.loads(self.rows()[0]["payload"])
        self.assertEqual(payload["prompt"], "x" * 10)
        self.assertTrue(payload["prompt_truncated"])
        self.assertEqual(payload["prompt_len"], 50)

    def test_short_fields_are_not_marked_truncated(self):
        self.sink.record("llm", {"prompt": "short"})
        payload = json.loads(self.rows()[0]["payload"])
        self.assertNotIn("prompt_truncated", payload)

    def test_a_failing_write_does_not_raise(self):
        self.store.conn.close()          # every write from here on will throw
        self.sink.record("llm", {"prompt": "a"})   # must not raise

    def test_module_level_record_is_a_noop_without_a_sink(self):
        tracing.record("llm", {"prompt": "a"})     # must not raise
        self.assertEqual(self.rows(), [])

    def test_module_level_record_uses_the_active_sink(self):
        token = tracing.use_sink(self.sink)
        try:
            tracing.record("llm", {"prompt": "a"})
        finally:
            tracing.reset_sink(token)
        self.assertEqual(len(self.rows()), 1)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tracing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analytics_platform.tracing'`

- [ ] **Step 3: Add the table to `TENANT_SCHEMA`**

In `analytics_platform/database.py`, immediately after the `CREATE TABLE IF NOT EXISTS telemetry (...);` block, add:

```sql
CREATE TABLE IF NOT EXISTS llm_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, tenant_id TEXT, trace_id TEXT, seq INTEGER,
    stage TEXT, kind TEXT, payload TEXT,
    duration_ms REAL, tokens_in INTEGER, tokens_out INTEGER, ok INTEGER
);
CREATE INDEX IF NOT EXISTS idx_llm_traces_trace ON llm_traces(tenant_id, trace_id, seq);
```

No `ALTER TABLE` migration is needed: `Store.__init__` applies the schema on
every open, and `CREATE TABLE IF NOT EXISTS` brings existing tenant databases up
to date the next time they are opened.

- [ ] **Step 4: Write `analytics_platform/tracing.py`**

```python
"""Ambient turn state, and the sink every trace record goes through.

Two contextvars carry what a wrapper cannot be handed directly. `_stage` is set
by the pipeline at boundaries it already emits step events for, so an LLM call
made during planning is labelled `planning` without the call site saying so.
`_sink` holds the tenant's store and this turn's trace id, because
`make_role_client` is given neither.

Everything here is best-effort by construction. Tracing is bolted onto the answer
path; a sink that can raise is a sink that can take a turn down, and an
observability feature is never worth an answer.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from typing import Any, Dict, Optional, Tuple

from .database import Store, dump_json
from .domain import now_iso

logger = logging.getLogger(__name__)

UNATTRIBUTED = "unattributed"
MAX_FIELD = 64_000

_stage: ContextVar[str] = ContextVar("turn_stage", default=UNATTRIBUTED)
_sink: ContextVar[Optional["TraceSink"]] = ContextVar("trace_sink", default=None)


def set_stage(stage: str) -> Token:
    return _stage.set(stage or UNATTRIBUTED)


def reset_stage(token: Token) -> None:
    _stage.reset(token)


def current_stage() -> str:
    return _stage.get()


def use_sink(sink: Optional["TraceSink"]) -> Token:
    return _sink.set(sink)


def reset_sink(token: Token) -> None:
    _sink.reset(token)


def current_sink() -> Optional["TraceSink"]:
    return _sink.get()


def record(kind: str, payload: Dict[str, Any], **kw: Any) -> None:
    """Record through the active sink, or do nothing. Never raises."""
    sink = _sink.get()
    if sink is None:
        return
    sink.record(kind, payload, **kw)


def clip(value: str, limit: int) -> Tuple[str, bool, int]:
    """(clipped, was_truncated, original_length)."""
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text, False, len(text)
    return text[:limit], True, len(text)


class TraceSink:
    """One turn's worth of trace rows, written to one tenant's database."""

    def __init__(self, store: Store, tenant_id: str, trace_id: str,
                 max_field: int = MAX_FIELD):
        self.store = store
        self.tenant_id = tenant_id
        self.trace_id = trace_id
        self.max_field = max_field
        self._seq = 0

    def _clip_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, value in payload.items():
            if not isinstance(value, str):
                out[key] = value
                continue
            text, truncated, length = clip(value, self.max_field)
            out[key] = text
            if truncated:
                out[f"{key}_truncated"] = True
                out[f"{key}_len"] = length
        return out

    def record(self, kind: str, payload: Dict[str, Any], *,
               duration_ms: float = 0.0, tokens_in: int = 0,
               tokens_out: int = 0, ok: bool = True) -> None:
        self._seq += 1
        try:
            self.store.execute(
                "INSERT INTO llm_traces (ts,tenant_id,trace_id,seq,stage,kind,"
                "payload,duration_ms,tokens_in,tokens_out,ok) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (now_iso(), self.tenant_id, self.trace_id, self._seq,
                 current_stage(), kind, dump_json(self._clip_payload(payload)),
                 float(duration_ms), int(tokens_in), int(tokens_out),
                 1 if ok else 0))
        except Exception as exc:  # noqa: BLE001 - a trace is never worth a turn
            logger.warning("trace record dropped (tenant=%s trace=%s kind=%s): %s",
                           self.tenant_id, self.trace_id, kind, exc)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tracing.py -v`
Expected: PASS, 8 tests

- [ ] **Step 6: Commit**

```bash
git add analytics_platform/tracing.py analytics_platform/database.py tests/test_tracing.py
git commit -m "$(cat <<'EOF'
feat(tracing): a sink for turn traces that cannot take a turn down

Two contextvars carry what a boundary wrapper cannot be handed: the current
pipeline stage, and this turn's sink. Every write is best-effort -- a sink
that can raise is a sink that can fail an answer, and an observability
feature is never worth that.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `TracingLLMClient` and the `make_role_client` seam

**Files:**
- Create: `analytics_platform/llm/tracing.py`
- Modify: `analytics_platform/llm/client.py:94-107` (`make_role_client`)
- Test: `tests/test_llm_tracing.py`

**Interfaces:**
- Consumes: `TraceSink`, `tracing.record`, `tracing.use_sink/reset_sink` from Task 1; `LLMResponse`, `LLMClient` from `llm/client.py`
- Produces: `TracingLLMClient(inner: LLMClient)` implementing `generate(...) -> LLMResponse`

- [ ] **Step 1: Write the failing test**

Create `tests/test_llm_tracing.py`:

```python
"""The wrapper is transparent, and it is total.

Transparent: `generate` returns the inner client's exact response object, so no
caller can tell it is there. Total: it records every call, including the ones a
retry loop makes, because the point of tracing at the boundary rather than the
call site is that nobody has to remember to trace.
"""
from __future__ import annotations

import json
import tempfile
import unittest

from analytics_platform import tracing
from analytics_platform.database import TENANT_SCHEMA, Store
from analytics_platform.llm.client import LLMResponse
from analytics_platform.llm.tracing import TracingLLMClient


class _Inner:
    def __init__(self, response=None, raises=False):
        self.response = response or LLMResponse(
            text="hello", provider="p", model="m", tokens_in=7, tokens_out=3)
        self.raises = raises
        self.calls = []

    def generate(self, prompt, system_prompt="", *, temperature=0.0, **kw):
        self.calls.append((prompt, system_prompt, temperature))
        if self.raises:
            raise RuntimeError("gateway down")
        return self.response


class TracingClientTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(f"{self._tmp.name}/t.db", schema=TENANT_SCHEMA)
        self.sink = tracing.TraceSink(self.store, "tnt_x", "trace-1")
        self._token = tracing.use_sink(self.sink)

    def tearDown(self):
        tracing.reset_sink(self._token)
        self._tmp.cleanup()

    def payloads(self):
        return [json.loads(r["payload"]) for r in self.store.query_all(
            "SELECT payload FROM llm_traces ORDER BY seq")]

    def test_returns_the_inner_response_object_unchanged(self):
        inner = _Inner()
        got = TracingLLMClient(inner).generate("q", "sys", temperature=0.2)
        self.assertIs(got, inner.response)

    def test_passes_arguments_through_untouched(self):
        inner = _Inner()
        TracingLLMClient(inner).generate("q", "sys", temperature=0.2)
        self.assertEqual(inner.calls, [("q", "sys", 0.2)])

    def test_records_the_prompt_and_the_verbatim_response(self):
        TracingLLMClient(_Inner()).generate("q", "sys")
        p = self.payloads()[0]
        self.assertEqual(p["prompt"], "q")
        self.assertEqual(p["system_prompt"], "sys")
        self.assertEqual(p["response_text"], "hello")
        self.assertEqual(p["model"], "m")

    def test_records_every_call_including_retries(self):
        client = TracingLLMClient(_Inner())
        for _ in range(3):
            client.generate("q")
        self.assertEqual(len(self.payloads()), 3)

    def test_a_raising_inner_client_still_records_and_still_raises(self):
        client = TracingLLMClient(_Inner(raises=True))
        with self.assertRaises(RuntimeError):
            client.generate("q")
        p = self.payloads()[0]
        self.assertFalse(p["ok"])
        self.assertIn("gateway down", p["error"])

    def test_no_sink_means_no_records_and_no_error(self):
        tracing.reset_sink(self._token)
        self._token = tracing.use_sink(None)
        got = TracingLLMClient(_Inner()).generate("q")
        self.assertEqual(got.text, "hello")
        self.assertEqual(self.payloads(), [])


class MakeRoleClientTest(unittest.TestCase):
    def test_make_role_client_returns_a_tracing_client(self):
        from analytics_platform.config import Settings
        from analytics_platform.llm.client import make_role_client
        client = make_role_client(Settings(), None)
        self.assertIsInstance(client, TracingLLMClient)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_llm_tracing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analytics_platform.llm.tracing'`

- [ ] **Step 3: Write `analytics_platform/llm/tracing.py`**

```python
"""The LLM boundary, observed.

Wrapping here rather than at the seven call sites is the whole design: a call
site can be forgotten, and it will be forgotten by the person adding the eighth
call, which is discovered exactly when the trace is needed. A wrapper has one
code path and no opt-out.

The response is returned before anything is recorded, so a recorder that fails
cannot swallow a good answer.
"""
from __future__ import annotations

from time import perf_counter
from typing import Any, Optional

from .. import tracing
from .client import LLMResponse


class TracingLLMClient:
    """Wraps an LLMClient and records each call through the ambient sink."""

    def __init__(self, inner: Any):
        self.inner = inner

    # The protocol's own attributes stay readable through the wrapper, so code
    # that sniffs `client.name` (the offline-detection path does) still works.
    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)

    def generate(self, prompt: str, system_prompt: str = "", *,
                 temperature: float = 0.0, **kwargs: Any) -> LLMResponse:
        t0 = perf_counter()
        response: Optional[LLMResponse] = None
        error = ""
        try:
            response = self.inner.generate(prompt, system_prompt,
                                           temperature=temperature, **kwargs)
            return response
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._record(prompt, system_prompt, temperature, response, error,
                         (perf_counter() - t0) * 1000.0)

    def _record(self, prompt: str, system_prompt: str, temperature: float,
                response: Optional[LLMResponse], error: str,
                duration_ms: float) -> None:
        ok = bool(response is not None and getattr(response, "ok", True) and not error)
        payload = {
            "prompt": prompt or "",
            "system_prompt": system_prompt or "",
            "response_text": getattr(response, "text", "") or "",
            "provider": getattr(response, "provider", "") or "",
            "model": getattr(response, "model", "") or "",
            "temperature": temperature,
            "ok": ok,
        }
        if error:
            payload["error"] = error
        tracing.record("llm", payload, duration_ms=duration_ms,
                       tokens_in=getattr(response, "tokens_in", 0) or 0,
                       tokens_out=getattr(response, "tokens_out", 0) or 0, ok=ok)
```

- [ ] **Step 4: Wire it into `make_role_client`**

In `analytics_platform/llm/client.py`, change the final return of
`make_role_client` (line 106-107) from:

```python
    return make_client(provider=provider, model=model, api_key=api_key,
                       ollama_base_url=ollama_base_url)
```

to:

```python
    from .tracing import TracingLLMClient
    return TracingLLMClient(make_client(provider=provider, model=model,
                                        api_key=api_key,
                                        ollama_base_url=ollama_base_url))
```

The import is function-local to avoid a circular import: `llm/tracing.py` imports
`analytics_platform.tracing`, which imports `database`, which must not depend on
`llm` at module load.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_llm_tracing.py -v`
Expected: PASS, 7 tests

- [ ] **Step 6: Run the full suite — nothing else may change**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS, same count as before this task (1038 passed, 1 skipped).
If `_llm_live` or any offline check regressed, the `__getattr__` passthrough is
why it must be there — do not remove it.

- [ ] **Step 7: Commit**

```bash
git add analytics_platform/llm/tracing.py analytics_platform/llm/client.py tests/test_llm_tracing.py
git commit -m "$(cat <<'EOF'
feat(tracing): observe the LLM boundary, not the seven call sites

A call site can be forgotten, and it gets forgotten by whoever adds the
eighth call -- discovered exactly when the trace is needed. The wrapper has
one code path and no opt-out, returns the inner response object untouched,
and records a raising call before re-raising it.

make_role_client is the single construction point, so the junior and senior
are wrapped too. They record nothing until they set a sink, which is theirs
to do and out of scope here.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Stage attribution and the `recalling` step

**Files:**
- Modify: `analytics_platform/domain.py:360-372` (`PIPELINE_STEPS`, `STEP_LABELS`)
- Modify: `analytics_platform/stakeholder.py` — `_step()` (~line 389), `_answer_steps` (~line 405-424)
- Test: `tests/test_trace_stages.py`

**Interfaces:**
- Consumes: `tracing.set_stage`, `tracing.use_sink`, `TraceSink` from Task 1
- Produces: `"recalling"` as the first entry of `PIPELINE_STEPS`; `StakeholderService._recalling_detail(intent: str, query_nodes: list, defn_nodes: list) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_trace_stages.py`:

```python
"""Stage attribution, and the step that was missing from the trail.

`_extract_search_intent` and `_retrieve` run before the pipeline emits its first
event, so the two operations most worth seeing -- the question rewrite and the
brain search -- were invisible in the live trail and would have been
`unattributed` in the trace. The `recalling` step fixes both, and it goes first
because it runs first.
"""
from __future__ import annotations

import json

from analytics_platform.domain import PIPELINE_STEPS, STEP_LABELS

from tests.test_extract_flow import CUBE_1, NARRATIVE, PY_CELL, SequencedLLM, _FlowCase


class RecallingStepTest(_FlowCase):
    def test_recalling_is_the_first_pipeline_step(self):
        self.assertEqual(PIPELINE_STEPS[0], "recalling")
        self.assertIn("recalling", STEP_LABELS)

    def test_the_stream_emits_recalling_before_understanding(self):
        self.approve_base()
        self.svc._llm = SequencedLLM([CUBE_1, PY_CELL, NARRATIVE])
        steps = [ev["payload"]["step"]
                 for ev in self.svc.answer_stream(self.tid, "how many sessions?",
                                                  conversation_id=self.c1)
                 if ev["type"] == "step"]
        self.assertIn("recalling", steps)
        self.assertLess(steps.index("recalling"), steps.index("understanding"))

    def test_recalling_detail_names_the_intent_and_the_node_counts(self):
        detail = self.svc._recalling_detail("session conversion", [1, 2], [3])
        self.assertIn("session conversion", detail)
        self.assertIn("2", detail)
        self.assertIn("1", detail)


class StageAttributionTest(_FlowCase):
    def traces(self):
        store = self.ctx.stores.for_tenant(self.tid)
        return [(r["stage"], r["kind"], json.loads(r["payload"]))
                for r in store.query_all(
                    "SELECT stage, kind, payload FROM llm_traces ORDER BY seq")]

    def test_a_turn_writes_traces_under_named_stages(self):
        self.approve_base()
        self.svc._llm = SequencedLLM([CUBE_1, PY_CELL, NARRATIVE])
        self.svc.answer(self.tid, "how many sessions?", conversation_id=self.c1)
        stages = {stage for stage, kind, _ in self.traces() if kind == "llm"}
        self.assertIn("planning", stages)
        self.assertNotIn("unattributed", stages)

    def test_a_planner_retry_shows_up_as_two_planning_records(self):
        """The spec's claim that a retry is readable from the trace. The first
        planner response is unparseable, which is exactly the failure that used
        to leave no evidence behind."""
        self.approve_base()
        self.svc._llm = SequencedLLM(["not json at all", CUBE_1, PY_CELL, NARRATIVE])
        self.svc.answer(self.tid, "how many sessions?", conversation_id=self.c1)
        planning = [p for stage, kind, p in self.traces()
                    if stage == "planning" and kind == "llm"]
        self.assertGreaterEqual(len(planning), 2)
        self.assertEqual(planning[0]["response_text"], "not json at all")

    def test_a_sink_that_cannot_write_still_answers_the_turn(self):
        """The constraint the whole feature is subordinate to. A trace is never
        worth an answer, so a store that throws on every insert must cost the
        records and nothing else."""
        class _Exploding:
            def execute(self, *a, **kw):
                raise RuntimeError("disk gone")
            def query_all(self, *a, **kw):
                return []
            def query_one(self, *a, **kw):
                return None

        real_for_tenant = self.ctx.stores.for_tenant

        def _boom(tenant_id):
            store = real_for_tenant(tenant_id)
            return _Exploding() if tenant_id == self.tid else store

        self.approve_base()
        self.svc._llm = SequencedLLM([CUBE_1, PY_CELL, NARRATIVE])
        original = self.svc.stores.for_tenant
        try:
            # Only the sink's store explodes; everything else keeps its real store.
            sink_store = _Exploding()
            import analytics_platform.tracing as tracing_mod
            real_sink = tracing_mod.TraceSink
            tracing_mod.TraceSink = lambda store, tid, trace, **kw: real_sink(
                sink_store, tid, trace, **kw)
            out = self.svc.answer(self.tid, "how many sessions?",
                                  conversation_id=self.c1)
        finally:
            tracing_mod.TraceSink = real_sink
            self.svc.stores.for_tenant = original
        self.assertEqual(out["status"], "ANSWERED")

    def test_traces_carry_this_turns_trace_id(self):
        self.approve_base()
        self.svc._llm = SequencedLLM([CUBE_1, PY_CELL, NARRATIVE])
        out = self.svc.answer(self.tid, "how many sessions?", conversation_id=self.c1)
        store = self.ctx.stores.for_tenant(self.tid)
        answer = store.query_one(
            "SELECT trace_id FROM stakeholder_answers WHERE id=?", (out["answer_id"],))
        traced = store.query_all(
            "SELECT DISTINCT trace_id FROM llm_traces")
        self.assertEqual([r["trace_id"] for r in traced], [answer["trace_id"]])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_trace_stages.py -v`
Expected: FAIL — `AssertionError: 'understanding' != 'recalling'`

- [ ] **Step 3: Add the step to `domain.py`**

Change `PIPELINE_STEPS` (line ~360) to put `recalling` first:

```python
PIPELINE_STEPS = ("recalling", "understanding", "planning", "checking_workspace",
                  "retrieving", "analysing", "interpreting")
```

And add its label to `STEP_LABELS`:

```python
STEP_LABELS = {
    "recalling": "Recalling what we know",
    "understanding": "Understanding the question",
    "planning": "Planning the turn",
    "checking_workspace": "Checking the workspace",
    "retrieving": "Retrieving",
    "analysing": "Analysing",
    "interpreting": "Interpreting",
}
```

- [ ] **Step 4: Make `_step()` set the stage**

In `analytics_platform/stakeholder.py`, `_step()` is a `@staticmethod`. Add the
stage set so every emitted boundary moves the contextvar:

```python
    @staticmethod
    def _step(step: str, state: str = "done", detail: str = "",
              t0: Optional[float] = None) -> Dict[str, Any]:
        assert step in PIPELINE_STEPS, step
        if state == "start":
            tracing.set_stage(step)
        return {"type": "step", "payload": asdict(StepEvent(
            step=step, state=state, detail=detail,
            elapsed_ms=((perf_counter() - t0) * 1000.0) if t0 is not None else 0.0))}
```

Only `"start"` sets it, so the stage stays put through the work that follows the
event and is replaced by the next stage's start. The token is intentionally
discarded: the sink is reset per turn, so a stage that outlives its turn is not
reachable.

Add the import at the top of `stakeholder.py`:

```python
from . import tracing
```

- [ ] **Step 5: Open a sink and emit `recalling` in `_answer_steps`**

In `_answer_steps`, replace lines 422-424:

```python
        llm = make_role_client(self.settings, cfg.stakeholder)
        search_intent = self._extract_search_intent(llm, question)
        query_nodes, defn_nodes = self._retrieve(tenant_id, search_intent)
```

with:

```python
        llm = make_role_client(self.settings, cfg.stakeholder)
        sink_token = tracing.use_sink(tracing.TraceSink(
            self.stores.for_tenant(tenant_id), tenant_id, trace))
        try:
            t0 = perf_counter()
            yield self._step("recalling", "start")
            search_intent = self._extract_search_intent(llm, question)
            query_nodes, defn_nodes = self._retrieve(tenant_id, search_intent)
            yield self._step("recalling", "done",
                             self._recalling_detail(search_intent, query_nodes,
                                                    defn_nodes), t0)
            out = yield from self._answer_steps_traced(
                llm, tenant_id, conversation_id, question, user_id, category,
                trace, query_nodes, defn_nodes)
            return out
        finally:
            tracing.reset_sink(sink_token)
```

Move the remainder of the existing `_answer_steps` body — everything from the
`is_high_risk` check onward — into a new generator `_answer_steps_traced` with
that signature. This keeps the sink open for the whole turn and closed on every
exit path, including the early returns and any exception.

Add the detail helper beside the other `_*_detail` static methods:

```python
    @staticmethod
    def _recalling_detail(intent: str, query_nodes: List[Any],
                          defn_nodes: List[Any]) -> str:
        """The rewritten search string is the retrieval key. Show it: when the
        wrong knowledge comes back, this string is usually the reason."""
        return (f"searched for '{intent}' -- {len(query_nodes)} approved "
                f"quer{'y' if len(query_nodes) == 1 else 'ies'}, "
                f"{len(defn_nodes)} definition"
                f"{'' if len(defn_nodes) == 1 else 's'}")
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_trace_stages.py -v`
Expected: PASS, 7 tests

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS. `tests/test_answer_stream.py` asserts on step sequences and may
need its expected list extended with `recalling` — if so that is a legitimate
update to the test, not a regression; make it and say so in the commit.

- [ ] **Step 8: Commit**

```bash
git add analytics_platform/domain.py analytics_platform/stakeholder.py tests/test_trace_stages.py tests/test_answer_stream.py
git commit -m "$(cat <<'EOF'
feat(stakeholder): a `recalling` step, and stages on every trace

The question rewrite and both brain searches ran before the pipeline emitted
its first event, so the two operations most worth watching were invisible in
the live trail and would have traced as `unattributed`. `recalling` goes
first in PIPELINE_STEPS because it runs first, and its detail states the
rewritten search string -- when the wrong knowledge comes back, that string
is usually the reason.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Instrument brain retrieval

**Files:**
- Modify: `analytics_platform/brain/store.py:181-224` (`CompanyBrain.search`)
- Test: `tests/test_trace_retrieval.py`

**Interfaces:**
- Consumes: `tracing.record` from Task 1
- Produces: trace records with `kind == "retrieval"` and keys `query`, `node_kind`, `limit`, `candidate_count`, `candidate_cap_hit`, `lexical_ids`, `dense_ids`, `fused_order`, `returned_ids`, `embedding_available`

- [ ] **Step 1: Write the failing test**

Create `tests/test_trace_retrieval.py`:

```python
"""Retrieval, made legible.

The failure this exists to expose: a 2-4 word rewrite is the sole key for both
searches, and when it surfaces the wrong nodes nothing records why. Putting the
query string next to the ids each leg returned is the whole point.
"""
from __future__ import annotations

import json
import tempfile
import unittest

from analytics_platform import tracing
from analytics_platform.brain.index import BrainIndex
from analytics_platform.brain.store import CompanyBrain
from analytics_platform.database import TENANT_SCHEMA, Store
from analytics_platform.domain import KnowledgeNode, NodeKind, ReviewStatus


class RetrievalTraceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(f"{self._tmp.name}/t.db", schema=TENANT_SCHEMA)
        self.brain = CompanyBrain(self.store, "tnt_x", index=BrainIndex(self.store))
        node = self.brain.create(NodeKind.DEFINITION, title="Checkout consent page",
                                 summary="the consent step of checkout",
                                 status=ReviewStatus.APPROVED)
        self.node_id = node.id
        self.sink = tracing.TraceSink(self.store, "tnt_x", "trace-1")
        self._token = tracing.use_sink(self.sink)

    def tearDown(self):
        tracing.reset_sink(self._token)
        self._tmp.cleanup()

    def retrieval_payloads(self):
        return [json.loads(r["payload"]) for r in self.store.query_all(
            "SELECT payload FROM llm_traces WHERE kind='retrieval' ORDER BY seq")]

    def test_a_search_records_the_query_and_what_came_back(self):
        self.brain.search("consent", kind=NodeKind.DEFINITION, limit=3)
        p = self.retrieval_payloads()[0]
        self.assertEqual(p["query"], "consent")
        self.assertEqual(p["node_kind"], "DEFINITION")
        self.assertEqual(p["limit"], 3)
        self.assertIn(self.node_id, p["lexical_ids"])
        self.assertIn(self.node_id, p["returned_ids"])

    def test_a_search_records_the_candidate_count(self):
        self.brain.search("consent", kind=NodeKind.DEFINITION, limit=3)
        self.assertEqual(self.retrieval_payloads()[0]["candidate_count"], 1)
        self.assertFalse(self.retrieval_payloads()[0]["candidate_cap_hit"])

    def test_embeddings_off_is_recorded_not_hidden(self):
        self.brain.search("consent", kind=NodeKind.DEFINITION, limit=3)
        p = self.retrieval_payloads()[0]
        self.assertFalse(p["embedding_available"])
        self.assertEqual(p["dense_ids"], [])

    def test_a_search_that_finds_nothing_still_records(self):
        self.brain.search("zzzznotathing", kind=NodeKind.DEFINITION, limit=3)
        p = self.retrieval_payloads()[0]
        self.assertEqual(p["returned_ids"], [])
        self.assertEqual(p["query"], "zzzznotathing")

    def test_browsing_mode_is_not_traced_as_a_relevance_claim(self):
        self.brain.search("", kind=NodeKind.DEFINITION, limit=3)
        self.assertEqual(self.retrieval_payloads(), [])

    def test_search_still_returns_its_results(self):
        got = self.brain.search("consent", kind=NodeKind.DEFINITION, limit=3)
        self.assertEqual([n.id for n in got], [self.node_id])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_trace_retrieval.py -v`
Expected: FAIL — `IndexError: list index out of range` (nothing recorded yet)

- [ ] **Step 3: Instrument `CompanyBrain.search`**

Add the import at the top of `analytics_platform/brain/store.py`:

```python
from .. import tracing
```

Then rewrite the body of `search` from the `if self.index is None:` guard to the
final return. Every early return records before returning, so a search that finds
nothing is as visible as one that succeeds:

```python
        def _trace(lexical, dense, fused_order, returned) -> None:
            tracing.record("retrieval", {
                "query": query,
                "node_kind": kind.value if kind is not None else "",
                "limit": limit,
                "candidate_count": len(nodes),
                "candidate_cap_hit": len(rows) == max(limit * 25, 500),
                "lexical_ids": list(lexical),
                "dense_ids": list(dense),
                "fused_order": list(fused_order),
                "returned_ids": [n.id for n in returned],
                "embedding_available": bool(
                    self.index is not None and self.index.embedding_available),
            })

        if self.index is None:
            logger.warning("search(%r) on tenant %s has no BrainIndex — returning no "
                           "results rather than unrelated recent nodes; this tenant's "
                           "brain needs an index (see Task 8)", query, self.tenant_id)
            _trace([], [], [], [])
            return []

        recall = max(limit * 4, 40)
        lexical = self.index.lexical_search(query, self.tenant_id, candidate_ids, recall)
        dense = self.index.vector_search(query, self.tenant_id, candidate_ids, recall)

        if not lexical and not dense:
            if not self.index.embedding_available:
                logger.info("no lexical hits for %r on tenant %s and embeddings are "
                            "unavailable", query, self.tenant_id)
            _trace(lexical, dense, [], [])
            return []

        fused = rrf_fuse([lexical, dense])
        confidence_by_id = {n.id: n.confidence for n in nodes}
        ordered = rank_nodes(fused, confidence_by_id)
        out = [by_id[i] for i in ordered if i in by_id][:limit]
        _trace(lexical, dense, ordered, out)
        return out
```

The `if not query: return nodes[:limit]` browsing branch above is left alone and
records nothing — it is explicitly not a relevance claim, and tracing it as one
would be a lie in the trace.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_trace_retrieval.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Run the brain and stakeholder suites**

Run: `.venv/bin/python -m pytest tests/test_brain_retrieval.py tests/test_brain_index.py tests/test_stakeholder.py -q`
Expected: PASS, unchanged counts.

- [ ] **Step 6: Commit**

```bash
git add analytics_platform/brain/store.py tests/test_trace_retrieval.py
git commit -m "$(cat <<'EOF'
feat(brain): record what each search asked for and what each leg returned

A 2-4 word rewrite is the sole key for both searches in a turn, and when it
surfaces the wrong nodes nothing recorded why. Every terminating path in
search() now records -- including the two that return [] -- because a search
that found nothing is exactly the one worth explaining.

Browsing mode (empty query) records nothing. It is not a relevance claim and
tracing it as one would put a lie in the trace.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: The per-answer trace endpoint

**Files:**
- Modify: `analytics_platform/stakeholder.py` (add `trace_for_answer`, near `extract_frame` ~line 2709)
- Modify: `analytics_platform/api.py` (add the route beside the other `/analyses` routes ~line 805)
- Test: `tests/test_trace_api.py`

**Interfaces:**
- Consumes: `llm_traces` rows from Task 1; `stakeholder_answers.trace_id`
- Produces: `StakeholderService.trace_for_answer(tenant_id: str, answer_id: str, stage: str = "") -> Optional[Dict[str, Any]]` returning `{"answer_id", "trace_id", "records": [...]}`, or `None` when the answer does not exist; route `GET /tenants/{tenant_id}/answers/{answer_id}/trace`

- [ ] **Step 1: Write the failing test**

Create `tests/test_trace_api.py`:

```python
"""Reading a turn back.

A trace is only worth writing if you can find it from the answer that bothered
you, which is why this reads by answer_id and not by trace_id: the answer is the
thing a person has in front of them.
"""
from __future__ import annotations

from tests.test_api import call
from tests.test_extract_flow import CUBE_1, NARRATIVE, PY_CELL, SequencedLLM, _FlowCase


class TraceEndpointTest(_FlowCase):
    def answer_once(self):
        self.approve_base()
        self.svc._llm = SequencedLLM([CUBE_1, PY_CELL, NARRATIVE])
        return self.svc.answer(self.tid, "how many sessions?",
                               conversation_id=self.c1)["answer_id"]

    def test_returns_the_records_for_that_answer(self):
        answer_id = self.answer_once()
        body = call(self.app, "get",
                    f"/tenants/{self.tid}/answers/{answer_id}/trace")
        self.assertEqual(body["answer_id"], answer_id)
        self.assertTrue(body["trace_id"])
        self.assertTrue(body["records"])
        self.assertIn(body["records"][0]["kind"], ("llm", "retrieval"))

    def test_records_come_back_in_sequence(self):
        answer_id = self.answer_once()
        body = call(self.app, "get",
                    f"/tenants/{self.tid}/answers/{answer_id}/trace")
        seqs = [r["seq"] for r in body["records"]]
        self.assertEqual(seqs, sorted(seqs))

    def test_stage_filter_narrows_the_result(self):
        answer_id = self.answer_once()
        body = call(self.app, "get",
                    f"/tenants/{self.tid}/answers/{answer_id}/trace?stage=planning")
        self.assertTrue(all(r["stage"] == "planning" for r in body["records"]))

    def test_unknown_answer_is_404(self):
        call(self.app, "get", f"/tenants/{self.tid}/answers/nope/trace",
             expect_status=404)

    def test_unknown_tenant_is_404(self):
        answer_id = self.answer_once()
        call(self.app, "get", f"/tenants/tnt_nope/answers/{answer_id}/trace",
             expect_status=404)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_trace_api.py -v`
Expected: FAIL — 404 on every case, the route does not exist

- [ ] **Step 3: Add `trace_for_answer` to `StakeholderService`**

```python
    def trace_for_answer(self, tenant_id: str, answer_id: str,
                         stage: str = "") -> Optional[Dict[str, Any]]:
        """Every LLM call and every retrieval behind one answer, in order.

        Returns None when the answer does not exist, so the route can 404 rather
        than hand back an empty trace that reads like "this answer thought
        nothing".
        """
        store = self.stores.for_tenant(tenant_id)
        row = store.query_one(
            "SELECT trace_id FROM stakeholder_answers WHERE id=? AND tenant_id=?",
            (answer_id, tenant_id))
        if row is None:
            return None
        trace_id = row["trace_id"] or ""
        sql = ("SELECT seq, ts, stage, kind, payload, duration_ms, tokens_in, "
               "tokens_out, ok FROM llm_traces WHERE tenant_id=? AND trace_id=?")
        params: List[Any] = [tenant_id, trace_id]
        if stage:
            sql += " AND stage=?"
            params.append(stage)
        sql += " ORDER BY seq"
        records = [{"seq": r["seq"], "ts": r["ts"], "stage": r["stage"],
                    "kind": r["kind"], "payload": load_json(r["payload"]) or {},
                    "duration_ms": r["duration_ms"], "tokens_in": r["tokens_in"],
                    "tokens_out": r["tokens_out"], "ok": bool(r["ok"])}
                   for r in store.query_all(sql, tuple(params))]
        return {"answer_id": answer_id, "trace_id": trace_id, "records": records}
```

`load_json` is already imported in `stakeholder.py` from `.database`.

- [ ] **Step 4: Add the route to `api.py`**

Place it immediately after the `list_analyses` route (~line 805):

```python
    @app.get("/tenants/{tenant_id}/answers/{answer_id}/trace")
    def answer_trace(tenant_id: str, answer_id: str,
                     stage: str = "") -> Dict[str, Any]:
        """Every LLM call and retrieval behind one answer, verbatim."""
        tenant_or_404(tenant_id)
        out = C.stakeholder.trace_for_answer(tenant_id, answer_id, stage=stage)
        if out is None:
            raise HTTPException(404, "Unknown answer")
        return out
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_trace_api.py -v`
Expected: PASS, 5 tests

- [ ] **Step 6: Commit**

```bash
git add analytics_platform/stakeholder.py analytics_platform/api.py tests/test_trace_api.py
git commit -m "$(cat <<'EOF'
feat(api): read a turn's whole trace back from its answer

Keyed by answer_id rather than trace_id, because the answer is the thing a
person actually has in front of them when they want to know what happened. An
unknown answer 404s rather than returning an empty trace, which would read as
"this answer thought nothing".

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Retention

**Files:**
- Modify: `analytics_platform/retention.py:30-38` (`RETENTION_TABLES`)
- Test: `tests/test_governance_retention.py` (extend the existing file)

**Interfaces:**
- Consumes: the `llm_traces` table from Task 1
- Produces: no new symbols; `("llm_traces", "ts")` appended to `RETENTION_TABLES`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_governance_retention.py`:

```python
class LlmTraceRetentionTest(unittest.TestCase):
    """Traces are the largest rows the platform writes -- a 64KB prompt times
    seven calls times every turn. They have to age out with everything else."""

    def test_llm_traces_is_in_the_retention_table_list(self):
        from analytics_platform.retention import RETENTION_TABLES
        self.assertIn(("llm_traces", "ts"), RETENTION_TABLES)

    def test_a_purge_actually_deletes_aged_trace_rows(self):
        """List membership is not the behaviour -- deletion is."""
        import tempfile
        from analytics_platform.database import TENANT_SCHEMA, Store
        from analytics_platform.retention import _cutoff_iso

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(f"{tmp}/t.db", schema=TENANT_SCHEMA)
            for ts in ("2020-01-01T00:00:00Z", "2999-01-01T00:00:00Z"):
                store.execute(
                    "INSERT INTO llm_traces (ts,tenant_id,trace_id,seq,stage,kind,"
                    "payload,duration_ms,tokens_in,tokens_out,ok) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, "tnt_x", "tr", 1, "planning", "llm", "{}", 0.0, 0, 0, 1))
            store.execute("DELETE FROM llm_traces WHERE ts < ?", (_cutoff_iso(30),))
            left = store.query_all("SELECT ts FROM llm_traces")
            self.assertEqual([r["ts"] for r in left], ["2999-01-01T00:00:00Z"])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_governance_retention.py::LlmTraceRetentionTest -v`
Expected: FAIL — `AssertionError: ('llm_traces', 'ts') not found in [...]`

- [ ] **Step 3: Add the entry**

In `analytics_platform/retention.py`, append to `RETENTION_TABLES`:

```python
RETENTION_TABLES = [
    ("telemetry", "ts"),
    ("llm_traces", "ts"),
    ("analysis_runs", "generated_at"),
    ("questions", "created_at"),
    ("stakeholder_answers", "created_at"),
    ("stakeholder_feedback", "created_at"),
    ("stakeholder_conversations", "updated_at"),
    ("research_docs", "created_at"),
]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_governance_retention.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/retention.py tests/test_governance_retention.py
git commit -m "$(cat <<'EOF'
feat(retention): age out llm_traces with everything else

Traces are the largest rows the platform writes: a 64KB prompt ceiling times
seven calls times every turn. The purge already walks RETENTION_TABLES, so
this is one line and no new machinery.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: The trace disclosure panel

**Files:**
- Create: `frontend/components/analyst/TracePanel.tsx`
- Modify: the disclosure tab container added in `4aa012f` — locate with `grep -rl "methodology" frontend/components`
- Test: `frontend/components/analyst/TracePanel.test.tsx`

**Interfaces:**
- Consumes: `GET /tenants/{tenant_id}/answers/{answer_id}/trace` from Task 5
- Produces: `<TracePanel tenantId={string} answerId={string} />`

- [ ] **Step 1: Find the existing disclosure container**

Run: `grep -rn "methodology" frontend/components frontend/app | head`
Read the file that renders the data/sql/code/methodology tabs. Follow its tab
registration pattern exactly — this task adds a tab, not a new mechanism.

- [ ] **Step 2: Write the failing test**

Create `frontend/components/analyst/TracePanel.test.tsx`:

```tsx
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { TracePanel } from './TracePanel'

const trace = {
  answer_id: 'ans_1',
  trace_id: 'trace-1',
  records: [
    { seq: 1, ts: '2026-08-22T10:00:00Z', stage: 'recalling', kind: 'llm',
      duration_ms: 120, tokens_in: 40, tokens_out: 8, ok: true,
      payload: { prompt: 'Extract the core topic', response_text: 'checkout drop-off',
                 model: 'm', temperature: 0 } },
    { seq: 2, ts: '2026-08-22T10:00:01Z', stage: 'recalling', kind: 'retrieval',
      duration_ms: 5, tokens_in: 0, tokens_out: 0, ok: true,
      payload: { query: 'checkout drop-off', returned_ids: ['kn_1'],
                 lexical_ids: ['kn_1'], dense_ids: [], embedding_available: false } },
  ],
}

describe('TracePanel', () => {
  it('groups records by stage', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, json: async () => trace,
    }))
    render(<TracePanel tenantId="t1" answerId="ans_1" />)
    await waitFor(() => expect(screen.getByText(/recalling/i)).toBeInTheDocument())
  })

  it('shows the retrieval query string', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, json: async () => trace,
    }))
    render(<TracePanel tenantId="t1" answerId="ans_1" />)
    await waitFor(() =>
      expect(screen.getByText(/checkout drop-off/)).toBeInTheDocument())
  })

  it('renders an empty trace without crashing', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, json: async () => ({ ...trace, records: [] }),
    }))
    render(<TracePanel tenantId="t1" answerId="ans_1" />)
    await waitFor(() =>
      expect(screen.getByText(/no trace/i)).toBeInTheDocument())
  })
})
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd frontend && npx vitest run components/analyst/TracePanel.test.tsx`
Expected: FAIL — cannot resolve `./TracePanel`

- [ ] **Step 4: Write `TracePanel.tsx`**

Fetch on mount, group `records` by `stage` preserving `seq` order, and render one
collapsed `<details>` per record. Show `stage`, `kind`, `model`, `duration_ms` and
tokens in the summary line. In the body render `system_prompt`, `prompt` and
`response_text` in `<pre>` blocks for `kind === 'llm'`; for `kind === 'retrieval'`
render `query` prominently with `lexical_ids`, `dense_ids` and `returned_ids`
beneath it. When `payload.prompt_truncated` is set, show
`truncated from {payload.prompt_len} chars`. Render `No trace recorded for this
answer.` when `records` is empty. Collapse the whole panel by default.

Match the styling and data-fetching idiom of the sibling disclosure panels found
in Step 1 rather than introducing a new one.

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd frontend && npx vitest run components/analyst/TracePanel.test.tsx`
Expected: PASS, 3 tests

- [ ] **Step 6: Register the tab and run the frontend suite**

Add the panel as a tab in the container from Step 1, labelled `Trace`.

Run: `cd frontend && npx vitest run`
Expected: PASS, all suites.

- [ ] **Step 7: Commit**

```bash
git add frontend/components/analyst/TracePanel.tsx frontend/components/analyst/TracePanel.test.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): a trace tab beside the other disclosures

Collapsed by default -- a full trace is tens of KB and nobody wants it open
by accident. Retrieval records lead with the query string, because that is
the field you open this panel to read.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] Run the whole Python suite: `.venv/bin/python -m pytest tests/ -q`
- [ ] Run the frontend suite: `cd frontend && npx vitest run`
- [ ] Confirm `answer()`'s payload is unchanged — `tests/test_answer_stream.py::TestCompatibility` must still pass untouched.
- [ ] Manually confirm a real turn writes traces:
  ```bash
  .venv/bin/python -c "
  import sqlite3, json
  c = sqlite3.connect('tenants/tnt_d23cd823d4c6/tenant.db')
  for r in c.execute('SELECT seq,stage,kind,length(payload) FROM llm_traces ORDER BY seq LIMIT 20'):
      print(r)
  "
  ```
