# Plan B — The Analyst Surface: assistant-ui, Streamed Steps, and the Storyline

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put a real analyst surface on top of Plan A's analyst. Replace the hand-rolled chat with assistant-ui, stream the pipeline's named steps as they happen, render the per-turn Analysis artifact as collapsible *Data used / SQL / Analysis code / Methodology* disclosures, draw the chart the Python step already specified, and turn a selection of turns into a genuine narrative storyline exportable as Markdown, Word, PDF, and PowerPoint.

**Architecture:** Plan A restructured `answer()` into six named steps and recorded an `analysis` artifact per turn. Plan B turns those two facts into a UI. `answer_stream()` becomes the real implementation — a generator yielding `step` events and a final `answer` event — and `answer()` becomes a thin wrapper that drains it, so every existing caller and test is untouched. A streaming `POST` endpoint relays those events to the browser, where an assistant-ui `ExternalStoreRuntime` adapter maps the conversation onto assistant-ui's thread model. The chat shell, composer, markdown, and message primitives come from the library; what we build is the analytical layer the library has no opinion about — the step trail, the provenance disclosures, the chart, and the storyline selection.

**Tech Stack:** Next.js 16.3.0 / React 19.2.8 / TypeScript 5 (strict) / zustand 5.0.14 / recharts 3.10.1 / assistant-ui (new) / Vitest + Testing Library (new, see Decisions) — and on the backend Python 3 / FastAPI / Starlette `StreamingResponse` / `python-pptx` + `reportlab` (new, both optional).

**Scope:** This is **Plan B of two**. It assumes Plan A is merged: `analysis`, `extract_meta`, the CSV download route, and the six named pipeline steps all exist. **Plan B must not re-open Plan A's decisions** — it does not touch the semantic layer, the Data Manager, the DuckDB workspace, or the extract store except to read what they produced.

---

## Context

**What Plan A deliberately left undone.** Plan A ends with a fully functional analyst behind an unchanged, hand-rolled chat UI. Every turn now records an `AnalysisArtifact` — question, plan rationale, semantics matched, unresolved terms, data requirement, coverage verdict, datasets used, warehouse SQL, workspace SQL, Python code, result summary, chart spec, key findings, assumptions — and none of it is visible. `frontend/src/components/StakeholderChat.tsx` renders `answer`, `queries_run`, and `python_cells` and nothing else. The most valuable thing the system now knows about itself is invisible to the person who has to defend the number in a meeting.

**Why assistant-ui and not more hand-rolling.** `StakeholderChat.tsx` is 288 lines of inline-styled JSX that reimplements a thread, a composer, a scroll-to-bottom effect, and a collapsible code block. It has no markdown rendering, no streaming, no message virtualisation, no keyboard handling beyond `Enter`, and no accessibility story. Every hour spent improving it is an hour not spent on the analytical layer, and the chat surface is a solved open-source problem. The engineering budget belongs to semantics, provenance, and correctness — the chat frame is bought, not built.

**What is genuinely ours to build.** assistant-ui gives a thread and a composer. It has no opinion about a six-step analytical pipeline, a coverage verdict, a fan-out attribution caveat, or a storyline. Those are the four components this plan writes, and they are the only reason the product is not a chat wrapper.

**Transparency without clutter is a hard constraint, not a preference.** The answer is prose. Everything else — the datasets, the SQL, the Python, the methodology — is one click away and closed by default. A stakeholder reading an answer should see a paragraph and a chart; a stakeholder challenged on that answer should be able to open every layer beneath it in under five seconds. A UI that dumps SQL into the thread by default fails the first reader; a UI that hides it entirely fails the second.

**A storyline is not a concatenation.** The existing `assemble_storyline` (`analytics_platform/storyline.py:63`) collects the selected turns and their code into a structured document. That is the right *assembly*, and it stays. What it does not do is *narrate*: eight selected turns produce eight disconnected question-and-answer blocks, with the same caveat repeated six times and no through-line. A narrative pass that sequences findings, de-duplicates caveats, and writes the connective tissue is the difference between an export and a deliverable.

**Streaming is about honesty, not polish.** A retrieve turn now costs a planning LLM call, a schema build that may profile tables inline, a warehouse round trip through a human's browser tab, and an analysis call. That is a long time to show a spinner labelled "Asking...". Showing *"checking the workspace → retrieving 412,003 rows → analysing"* is not decoration: it tells the user what the system is spending their time and money on, and it makes a stall diagnosable instead of mysterious.

---

## Decisions taken

| Decision | Choice | Why |
|---|---|---|
| Chat framework | **assistant-ui**, with `ExternalStoreRuntime` | Our state already lives in zustand and our backend is not the AI SDK protocol. `ExternalStoreRuntime` is the adapter designed for exactly that; `LocalRuntime` would mean handing assistant-ui ownership of state it cannot manage |
| Transport | **`POST` with a streamed body**, read via `fetch` + `response.body.getReader()` — *not* `EventSource` | This deviates from Plan A's sketch of `GET …/answer/stream`, deliberately. `EventSource` is GET-only, which would put the user's question into a URL — logged by every proxy and in `tmp/api.log` — and gives no way to send `conversation_id` in a body. The wire format stays SSE-shaped (`event:` / `data:` lines) so a future `EventSource` or `sse-starlette` swap is trivial |
| `answer()` compatibility | **`answer_stream()` is the implementation; `answer()` drains it** | `answer()`'s signature and return value do not change, so all of Plan A's tests and the existing `POST /answer` route stay green. Any other split would mean two code paths that drift |
| Frontend tests | **Add Vitest + Testing Library** | See *Open decisions* — this is a real dependency addition on a repo with no frontend test runner, and it is the one item in this plan the user should consciously accept or strike |
| Chart source | **`analysis.chart_spec`**, mapped onto the existing `ChartRenderer` | `ChartRenderer.tsx` already handles Line/Bar/Area/Scatter with recharts and is good. Plan A emits a neutral `{kind, x, y, series, title}` spec; Plan B adapts it. The legacy `chart_config`/`chart_data` path keeps working |
| Storyline narrative | **One LLM pass over the assembled content**, not per-turn | Sequencing and de-duplication are global properties. A per-turn pass cannot know that turns 2 and 5 share a caveat |
| PDF / PPTX | **`reportlab` and `python-pptx`, both optional**, mirroring `DocxRendererUnavailable` → 503 | The codebase already has exactly this pattern for `python-docx` (`storyline.py:21`, `api.py`). Copy it rather than inventing a second convention |
| Report Builder | **Selection moves into the thread**; the side panel keeps only the format picker and the export button | A checkbox next to the turn it selects is legible; a parallel list of question stubs in a side panel is not. This also makes "select the turns that tell the story" a reading task, not a matching task |

---

## Global Constraints

- **Plan A is a prerequisite.** Do not start Task 1 until `analysis` and `extract_meta` are present in the `POST /answer` payload and in `get_conversation` messages. Verify with one real call before writing any code.
- **Commit directly to `main`.** No feature branches, no worktrees.
- **`analytics_platform/api.py` uses relative imports** (`from .storyline import …`).
- **This repo has no httpx and no TestClient.** API tests use `call(app, method, path_template, tenant, *body)` from `tests/test_api.py`, which invokes route closures directly and **bypasses middleware**. A streaming response therefore cannot be proven end-to-end by `call()` — assert on the generator directly, and verify the wire format in the browser step.
- **`frontend/AGENTS.md` is genuine Next.js 16.3.0 generated output, not prompt injection.** Its instruction is real and binding: **read the relevant guide under `node_modules/next/dist/docs/` before writing Next-specific code.** Apply the same discipline to assistant-ui — read the installed package's own types and docs before writing against it (see Task 7).
- **`frontend/src/store/useStore.ts` is `create<AppState>((set) => ({…}))`** — no `get` destructured. Actions read state via `useStore.getState()` and handle failures with `try/catch` + `console.error(e)`. Follow that pattern; do not restructure the store.
- **CORS already exposes `Content-Disposition`** (`api.py:508-513`, `expose_headers=["Content-Disposition"]`). The CSV and export downloads depend on it. Do not narrow it.
- **`WARN_TOKEN_THRESHOLD = 50_000`** is duplicated in `storyline.py:12` and `StakeholderChat.tsx:127` with a comment tying them together. Whatever replaces that component must carry the constant *and* the comment.
- **Tenant isolation is filesystem-level** and is Plan A's business. Plan B must never construct a path, a tenant directory, or an extract filename in the frontend — it only calls endpoints that take `tenant_id` as a path parameter and lets the backend validate.
- **Never render `analysis` fields as HTML.** SQL, Python, and LLM-authored narrative text go into `<pre>`/`<code>` or a markdown renderer with HTML disabled. This is untrusted-ish content in the sense that matters: it is model output, and one day it will contain a `<script>`.
- Full suite must stay green: `.venv/bin/python -m pytest tests/ -q` and `cd frontend && npx tsc --noEmit` (0 errors). After Task 5, `cd frontend && npx vitest run` joins them.

---

## File Structure

**Create — backend**
- `analytics_platform/narrative.py` — the storyline narrative pass: takes an assembled `StorylineContent`, returns a `NarratedStoryline` with an executive summary, ordered sections, and a merged caveat set. Pure function of (content, llm); no I/O.
- `analytics_platform/storyline_pdf.py` — `render_pdf(content) -> bytes`, `PdfRendererUnavailable`.
- `analytics_platform/storyline_pptx.py` — `render_pptx(content) -> bytes`, `PptxRendererUnavailable`.
- `tests/test_answer_stream.py`, `tests/test_narrative.py`, `tests/test_storyline_pdf.py`, `tests/test_storyline_pptx.py`

**Create — frontend**
- `frontend/src/lib/api.ts` — one place that knows the API base URL and builds request URLs. Every `fetch('http://localhost:8000/…')` in the store moves behind it.
- `frontend/src/lib/streamAnswer.ts` — the streaming client: POSTs a question, parses the SSE-shaped body, invokes `onStep` / `onAnswer` / `onError`.
- `frontend/src/types/analysis.ts` — TypeScript mirrors of `AnalysisArtifact`, `ExtractMeta`, `CoverageVerdict`, `ChartSpec`, and the step-event union. The single source of truth for the payload shape.
- `frontend/src/runtime/useStakeholderRuntime.ts` — the `ExternalStoreRuntime` adapter bridging zustand ⇄ assistant-ui.
- `frontend/src/components/analyst/StepTrail.tsx` — the live pipeline trail.
- `frontend/src/components/analyst/AnalysisDisclosures.tsx` — ▸ Data used / ▸ SQL / ▸ Analysis code / ▸ Methodology.
- `frontend/src/components/analyst/Disclosure.tsx` — the one collapsible primitive all of the above share (replaces `CollapsibleCode`).
- `frontend/src/components/analyst/AnalysisChart.tsx` — `chart_spec` → `ChartConfig` → `ChartRenderer`.
- `frontend/src/components/analyst/ExtractDownload.tsx` — the CSV button + row-count/truncation line.
- `frontend/src/components/analyst/AnalystMessage.tsx` — the assistant message body: prose, chart, caveats, disclosures, feedback, storyline checkbox.
- `frontend/src/components/AnalystThread.tsx` — the assistant-ui thread shell; replaces `StakeholderChat.tsx`.
- `frontend/vitest.config.ts`, `frontend/src/test/setup.ts`
- `frontend/src/**/__tests__/*.test.tsx` per component task

**Modify**
- `analytics_platform/stakeholder.py` — `answer_stream()` generator; `answer()` becomes its wrapper.
- `analytics_platform/api.py` — the streaming route; `format` accepts `pdf`/`pptx`; a `narrate` flag on export.
- `analytics_platform/storyline.py` — `StorylineContent` gains the narrated fields; renderers consume them when present.
- `frontend/src/store/useStore.ts` — `askStakeholder` streams; new `steps` state; `analysis`/`extract_meta` on `StakeholderMessage`; export gains `pdf`/`pptx`.
- `frontend/src/app/page.tsx` (or wherever `StakeholderChat` is mounted — `grep -rn "StakeholderChat" frontend/src`) — mount `AnalystThread`.
- `frontend/package.json`, `requirements.txt`

**Delete**
- `frontend/src/components/StakeholderChat.tsx` — in Task 13, only after `AnalystThread` is verified working. Not before.

---

## Task Map

| # | Task | Why it exists |
|---|---|---|
| 1 | `answer_stream()` generator | six named steps exist but nothing can observe them |
| 2 | Streaming `POST` endpoint | get the steps to the browser without putting questions in URLs |
| 3 | Narrative storyline pass | an export must read as a document, not eight stapled answers |
| 4 | PDF + PPTX renderers | the two formats a stakeholder actually presents from |
| 5 | Frontend test harness | six UI tasks with no regression net otherwise |
| 6 | API layer, payload types, streaming client | one typed contract, not `any` scattered through the store |
| 7 | assistant-ui + `ExternalStoreRuntime` adapter | the bought half of the chat |
| 8 | `AnalystThread` replaces the hand-rolled chat | the built half |
| 9 | `StepTrail` | show what the pipeline is doing, live |
| 10 | `AnalysisDisclosures` | the artifact becomes inspectable — the point of Plan A |
| 11 | Chart from `chart_spec` + extract download | the Python step's chart, and the raw data |
| 12 | Storyline selection in-thread + new formats | selection next to what it selects |
| 13 | Delete the old chat, full verification | no two chat implementations |

---

### Task 1: `answer_stream()` — the pipeline as a generator

**Files:**
- Modify: `analytics_platform/stakeholder.py` (`answer()` and the six step boundaries Plan A carved)
- Test: Create `tests/test_answer_stream.py`

**Interfaces:**
- Produces, in `analytics_platform/domain.py`:
  ```python
  # The six steps, in pipeline order. The UI renders them in this order and
  # greys out the ones a given turn skipped -- a reuse turn never retrieves.
  PIPELINE_STEPS = ("understanding", "planning", "checking_workspace",
                    "retrieving", "analysing", "interpreting")

  @dataclass
  class StepEvent:
      step: str                       # one of PIPELINE_STEPS
      state: str                      # "start" | "done" | "skipped"
      label: str                      # human-facing: "Checking the workspace"
      detail: str = ""                # "reusing df_1 (412,003 rows) -- no warehouse query needed"
      elapsed_ms: float = 0.0
  ```
- Produces, on `StakeholderService`:
  ```python
  def answer_stream(self, tenant_id: str, question: str, *, user_id: str = "",
                    conversation_id: str = "") -> Iterator[Dict[str, Any]]: ...
      # yields {"type": "step",   "payload": asdict(StepEvent)}   -- zero or more
      # yields {"type": "answer", "payload": <the existing answer dict>} -- exactly once, last

  def answer(self, tenant_id, question, *, user_id="", conversation_id="") -> Dict[str, Any]:
      # unchanged signature and return value -- now a wrapper that drains answer_stream
  ```

**The compatibility rule that makes this safe.** `answer()` keeps its exact signature and return value:

```python
def answer(self, tenant_id, question, *, user_id="", conversation_id=""):
    out = None
    for ev in self.answer_stream(tenant_id, question, user_id=user_id,
                                 conversation_id=conversation_id):
        if ev["type"] == "answer":
            out = ev["payload"]
    return out
```

Every existing test, the existing `POST /answer` route, and every internal caller are therefore untouched. **There must be exactly one implementation** — do not leave a parallel non-streaming path that can drift.

**`detail` is where the honesty lives.** A step that says only "Analysing" is a spinner with extra steps. Populate `detail` from what the pipeline already computed:
- `understanding` → the metrics the semantic layer matched, or `"no defined metric matched 'churn'"` when `unresolved_terms` is non-empty
- `planning` → the chosen grain and the analysis mode, e.g. `"one row per session_id; analysing in DuckDB"`
- `checking_workspace` → `verdict.reason` verbatim — Plan A wrote it for a human, so show it to one
- `retrieving` → `"412,003 rows"`, or `"skipped -- the workspace already covers this"` with `state="skipped"`
- `analysing` → `"DuckDB re-cut over df_1"` or `"Python: 3 cells"`
- `interpreting` → `""`

**Failures are events too.** An exception inside the pipeline must still yield a terminal `answer` event carrying the existing error-shaped answer dict — never end the stream on a step. A client that receives steps and then nothing has no way to distinguish a crash from a slow warehouse.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_answer_stream.py
def test_answer_still_returns_the_same_dict(service, tenant):
    """The compatibility guarantee. If this breaks, every Plan A test breaks."""
    out = service.answer(tenant, "what is revenue by country?", conversation_id="c1")
    assert out["answer"] and out["answer_id"] and "analysis" in out

def test_stream_emits_steps_then_exactly_one_answer(service, tenant):
    evs = list(service.answer_stream(tenant, "what is revenue by country?", conversation_id="c1"))
    assert evs[-1]["type"] == "answer"
    assert [e["type"] for e in evs].count("answer") == 1
    assert any(e["type"] == "step" for e in evs[:-1])

def test_steps_arrive_in_pipeline_order(service, tenant):
    steps = [e["payload"]["step"] for e in service.answer_stream(tenant, "q", conversation_id="c1")
             if e["type"] == "step"]
    order = [PIPELINE_STEPS.index(s) for s in steps]
    assert order == sorted(order)

def test_a_reuse_turn_marks_retrieving_as_skipped(service, tenant, warm_workspace):
    """Plan A's headline behaviour, now visible: no warehouse query, and the UI
    can say so rather than showing an empty step."""
    evs = list(service.answer_stream(tenant, "break that down by device", conversation_id="c1"))
    retrieving = next(e["payload"] for e in evs
                      if e["type"] == "step" and e["payload"]["step"] == "retrieving")
    assert retrieving["state"] == "skipped"
    assert evs[-1]["payload"]["queries_run"] == []

def test_the_workspace_step_detail_is_the_coverage_reason(service, tenant, warm_workspace):
    ws = next(e["payload"] for e in service.answer_stream(tenant, "q", conversation_id="c1")
              if e["type"] == "step" and e["payload"]["step"] == "checking_workspace")
    assert "df_1" in ws["detail"]

def test_an_unresolved_metric_shows_up_in_the_understanding_step(service, tenant):
    u = next(e["payload"] for e in service.answer_stream(tenant, "what is our churn rate?",
                                                         conversation_id="c1")
             if e["type"] == "step" and e["payload"]["step"] == "understanding")
    assert "churn" in u["detail"]

def test_a_pipeline_failure_still_terminates_with_an_answer_event(service, tenant, broken_executor):
    evs = list(service.answer_stream(tenant, "q", conversation_id="c1"))
    assert evs[-1]["type"] == "answer"
    assert evs[-1]["payload"]["status"] in ("error", "failed") or evs[-1]["payload"]["caveats"]

def test_steps_carry_elapsed_time(service, tenant):
    done = [e["payload"] for e in service.answer_stream(tenant, "q", conversation_id="c1")
            if e["type"] == "step" and e["payload"]["state"] == "done"]
    assert all(s["elapsed_ms"] >= 0 for s in done) and done
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_answer_stream.py -q`
Expected: FAIL — `AttributeError: 'StakeholderService' object has no attribute 'answer_stream'`

- [ ] **Step 3: Implement**

Move the body of `answer()` into `answer_stream()` and insert `yield` at the six boundaries Plan A already named. Do not restructure the pipeline logic itself — this task is a control-flow change and nothing else. Use a small local helper so each step is two lines at the call site:

```python
def _step(step, label, detail="", state="done", t0=None):
    return {"type": "step", "payload": asdict(StepEvent(
        step=step, state=state, label=label, detail=detail,
        elapsed_ms=(perf_counter() - t0) * 1000 if t0 else 0.0))}
```

Wrap the whole body in `try/except` so the terminal `answer` event is emitted from a `finally`-style path on failure.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS — including every Plan A test, unchanged.

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/stakeholder.py analytics_platform/domain.py tests/test_answer_stream.py && git commit -m "feat(stakeholder): expose the analyst pipeline as a step-event stream"
```

---

### Task 2: The streaming endpoint

**Files:**
- Modify: `analytics_platform/api.py` (beside the stakeholder block at ~1041)
- Test: `tests/test_api.py` (extend)

**Interfaces:**
- Produces: `POST /stakeholder/{tenant_id}/answer/stream`, body `StakeholderIn` (the same model `POST /answer` uses — reuse it, do not define a second), response `StreamingResponse` with `media_type="text/event-stream"`.
- Wire format, SSE-shaped so a later `EventSource`/`sse-starlette` swap costs nothing:
  ```
  event: step
  data: {"step":"checking_workspace","state":"done","label":"Checking the workspace","detail":"reused df_1 …","elapsed_ms":12.4}

  event: answer
  data: {...the full answer payload, identical to POST /answer...}
  ```

**Why POST and not the `GET …/answer/stream` Plan A sketched.** `EventSource` only issues GET, which forces the user's question into the query string — where it lands in `tmp/api.log`, in any proxy log, and in browser history — and leaves nowhere clean for `conversation_id`. A `POST` read through `fetch` + `response.body.getReader()` costs about fifteen lines of client code and avoids all of it. Record this deviation in the commit message.

**Implementation notes:**
- `StakeholderService.answer_stream` is a **sync** generator. Return `StreamingResponse(gen, media_type="text/event-stream")` with a sync iterator — Starlette runs it in a threadpool. Do **not** make the service async.
- Headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no`, `Connection: keep-alive`. Without `X-Accel-Buffering` an nginx in front of this buffers the whole stream and the feature silently degrades to a slow blocking request.
- Serialise with the same JSON helper the rest of `api.py` uses, and ensure **no raw newline** survives into a `data:` line — one `json.dumps` per event, `\n\n` terminator.
- `tenant_or_404(tenant_id)` **before** constructing the generator, so an unknown tenant is a clean 404 and not a 200 with an error inside the stream.
- Wrap the generator so an exception mid-stream emits `event: error` with a JSON `{"detail": …}` and then closes. The client treats a stream that ends without an `answer` event as a failure.
- Leave `POST /answer` exactly as it is. It is the fallback path and several tests use it.

- [ ] **Step 1: Write the failing tests**

```python
def test_stream_route_yields_sse_framed_events(app, tenant):
    """call() bypasses middleware and does not consume a StreamingResponse body,
    so assert on the iterator the route returns."""
    r = call(app, "POST", "/stakeholder/{t}/answer/stream", tenant,
             {"question": "what is revenue by country?", "conversation_id": "c1"})
    assert r.media_type == "text/event-stream"
    chunks = list(r.body_iterator)
    text = "".join(c.decode() if isinstance(c, bytes) else c for c in chunks)
    assert "event: step" in text
    assert text.rstrip().endswith("\n") or "event: answer" in text
    assert text.count("event: answer") == 1

def test_stream_answer_event_matches_the_blocking_route(app, tenant):
    ...parse the answer event's JSON; assert its keys match POST /answer's keys...

def test_stream_404s_for_an_unknown_tenant_before_streaming(app):
    with pytest.raises(HTTPException) as e:
        call(app, "POST", "/stakeholder/{t}/answer/stream", "no-such-tenant", {"question": "q"})
    assert e.value.status_code == 404

def test_no_data_line_contains_a_raw_newline(app, tenant):
    ...an answer whose text contains "\n" must still frame correctly...
    for line in text.splitlines():
        if line.startswith("data: "):
            json.loads(line[6:])          # each data line is complete JSON on its own
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_api.py -q -k stream`
Expected: FAIL — route not found.

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/api.py tests/test_api.py && git commit -m "feat(api): stream analyst pipeline steps over a POST SSE endpoint

Deviates from the plan sketch of GET /answer/stream: EventSource is GET-only,
which would put the user's question in the URL and in every proxy log."
```

---

### Task 3: The storyline narrative pass

**Files:**
- Create: `analytics_platform/narrative.py`
- Modify: `analytics_platform/storyline.py` (`StorylineContent` gains narrated fields)
- Test: Create `tests/test_narrative.py`

**Why.** `assemble_storyline` produces the right *material* — turns, findings, caveats, a code appendix — and no *document*. Eight selected turns export as eight disconnected Q&A blocks with the same truncation caveat repeated six times. A stakeholder cannot present that.

**Interfaces:**
- Produces:
  ```python
  @dataclass
  class NarratedSection:
      heading: str
      body: str                       # the connective prose for this section
      answer_ids: List[str]           # the turns this section draws on -- provenance survives
      chart_spec: Optional[Dict[str, Any]] = None

  @dataclass
  class NarratedStoryline:
      title: str
      executive_summary: str          # 3-5 sentences: the answer, up front
      sections: List[NarratedSection]
      caveats: List[str]              # de-duplicated union across every included turn
      ok: bool = True
      error: str = ""

  def narrate(content: StorylineContent, llm) -> NarratedStoryline: ...
  ```
- `StorylineContent` gains `narrative: Optional[NarratedStoryline] = None`. Every renderer uses it **when present** and falls back to today's turn-by-turn layout when it is `None`. Narration must never be the only way to export.

**Hard requirements on the prompt — these are what separate a narrative from a summary:**
- **Every claim must trace to an `answer_id`.** The prompt is given the turns with their ids and must attach ids to each section. A section with no ids is a hallucination and is dropped by the caller, not by the model.
- **Do not invent numbers.** Figures come from the turns verbatim. State this and validate it: after generation, check that every number-shaped token in the narrative appears in the source content, and drop the section (logging at WARNING) if it does not. A storyline with a fabricated figure is worse than no storyline.
- **De-duplicate caveats by meaning, not by string.** "extract truncated at 1,000,000 rows" appearing on five turns is one caveat.
- **Order by argument, not by chronology.** The order questions were asked is the order of a person exploring; the order a reader needs is the order of an argument.
- **`ok=False` on any failure**, with the caller falling back to the un-narrated document. An export must never fail because an LLM call failed.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_narrative.py
def test_narrate_returns_sections_with_answer_ids(content_3_turns, mock_llm):
    n = narrate(content_3_turns, mock_llm)
    assert n.ok and n.sections
    assert all(s.answer_ids for s in n.sections)

def test_every_referenced_answer_id_is_real(content_3_turns, mock_llm_with_bogus_id):
    n = narrate(content_3_turns, mock_llm_with_bogus_id)
    known = {t.answer_id for t in content_3_turns.turns}
    assert all(set(s.answer_ids) <= known for s in n.sections)

def test_repeated_caveats_are_merged(content_with_same_caveat_on_3_turns, mock_llm):
    assert len(narrate(content_with_same_caveat_on_3_turns, mock_llm).caveats) == 1

def test_a_fabricated_number_drops_the_section(content_3_turns, mock_llm_inventing_a_figure):
    n = narrate(content_3_turns, mock_llm_inventing_a_figure)
    assert all("47.3" not in s.body for s in n.sections)

def test_an_llm_failure_degrades_instead_of_raising(content_3_turns, failing_llm):
    n = narrate(content_3_turns, failing_llm)
    assert n.ok is False and n.error

def test_renderers_fall_back_when_narrative_is_absent(content_3_turns):
    md = render_markdown(content_3_turns)          # narrative is None
    assert content_3_turns.turns[0].question in md

def test_markdown_uses_the_narrative_when_present(content_with_narrative):
    md = render_markdown(content_with_narrative)
    assert content_with_narrative.narrative.executive_summary in md
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_narrative.py -q`
Expected: FAIL — `ModuleNotFoundError: analytics_platform.narrative`

- [ ] **Step 3: Implement**

Add `narrate: bool = False` to `StorylineExportIn` and call `narrate()` from the export route when set. Default off, so an export never silently becomes slower and more expensive than the user expects.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/narrative.py analytics_platform/storyline.py analytics_platform/api.py tests/test_narrative.py && git commit -m "feat(storyline): narrate selected turns into a sequenced document"
```

---

### Task 4: PDF and PowerPoint renderers

**Files:**
- Create: `analytics_platform/storyline_pdf.py`, `analytics_platform/storyline_pptx.py`
- Modify: `analytics_platform/api.py` (extend the `format` branch), `requirements.txt`
- Test: Create `tests/test_storyline_pdf.py`, `tests/test_storyline_pptx.py`

**Copy the existing optional-dependency pattern exactly.** `storyline.py:21` defines `DocxRendererUnavailable(RuntimeError)`; the export route catches it and raises 503 with a message naming the missing package. Mirror it: `PdfRendererUnavailable`, `PptxRendererUnavailable`, import the third-party module lazily *inside* the render function, and let the route translate. Do not invent a second convention, and do not make either package a hard import at module load — the suite must pass on a machine with neither installed.

**Interfaces:**
- `render_pdf(content: StorylineContent) -> bytes` — reportlab `platypus`: title, executive summary, one section per `NarratedSection` (or per turn on fallback), caveats, and the code appendix in a monospace style. Reuse `_one_line` and `_fence_for` from `storyline.py` rather than re-deriving them.
- `render_pptx(content: StorylineContent) -> bytes` — python-pptx: a title slide, one slide per section (heading + 3-5 bullets, not the full prose), a caveats slide, and **no code appendix** — a code dump in a deck is noise. If a section has a `chart_spec`, add its title and a placeholder note; rendering an image server-side is out of scope and must be stated in the code, not silently skipped.

**Guard against the header bug the docx route already solved.** The filename logic in `api.py`'s export route (ASCII slug + RFC 5987 `filename*=UTF-8''…`, bounded to 60 chars) is shared by all four formats. Extend the `ext`/`media_type` branch only — do not copy the filename block into a new place.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_storyline_pdf.py
def test_pdf_starts_with_the_pdf_magic_bytes(content_3_turns):
    assert render_pdf(content_3_turns)[:5] == b"%PDF-"

def test_pdf_is_skipped_cleanly_when_reportlab_is_absent(content_3_turns, no_reportlab):
    with pytest.raises(PdfRendererUnavailable):
        render_pdf(content_3_turns)

def test_pdf_includes_the_executive_summary_when_narrated(content_with_narrative):
    ...extract text; assert the summary's first sentence is present...

# tests/test_storyline_pptx.py
def test_pptx_is_a_zip_container(content_3_turns):
    assert render_pptx(content_3_turns)[:2] == b"PK"

def test_one_slide_per_section_plus_title_and_caveats(content_with_narrative):
    prs = Presentation(io.BytesIO(render_pptx(content_with_narrative)))
    assert len(prs.slides) == len(content_with_narrative.narrative.sections) + 2

def test_pptx_omits_the_code_appendix(content_3_turns):
    ...assert no slide text contains "SELECT"...
```

Plus, in `tests/test_api.py`:

```python
def test_export_supports_pdf_and_pptx(app, tenant):
    for fmt, magic in (("pdf", b"%PDF-"), ("pptx", b"PK")):
        r = call(app, "POST", "/stakeholder/{t}/conversations/c1/export", tenant,
                 {"answer_ids": ["a1"], "format": fmt})
        assert r.body[:5].startswith(magic[:2])

def test_an_unsupported_format_is_still_a_400(app, tenant):
    with pytest.raises(HTTPException) as e:
        call(app, "POST", "/stakeholder/{t}/conversations/c1/export", tenant,
             {"answer_ids": ["a1"], "format": "keynote"})
    assert e.value.status_code == 400
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_storyline_pdf.py tests/test_storyline_pptx.py -q`
Expected: FAIL — modules not found.

- [ ] **Step 3: Implement**

```bash
.venv/bin/pip install reportlab python-pptx && .venv/bin/pip show reportlab python-pptx | grep -i -E "^(Name|Version)"
```

Pin both into `requirements.txt` in the same commit, in the same style as the existing entries.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/storyline_pdf.py analytics_platform/storyline_pptx.py analytics_platform/api.py requirements.txt tests/ && git commit -m "feat(storyline): PDF and PowerPoint export"
```

---

### Task 5: Frontend test harness

**Files:**
- Create: `frontend/vitest.config.ts`, `frontend/src/test/setup.ts`
- Modify: `frontend/package.json`
- Test: `frontend/src/components/__tests__/ChartRenderer.test.tsx` (a real test of existing code, to prove the harness works)

**Read *Open decisions* before starting this task.** This is the one dependency addition in Plan B that is a judgement call rather than a consequence. If the user strikes it, skip this task and delete the "Step 1: write the failing test" step from Tasks 7-12, verifying those tasks through `tsc --noEmit` and the browser instead — and say so explicitly in each commit message rather than quietly dropping the tests.

**Why it is worth it.** Tasks 9-12 render a provenance artifact whose whole value is being trustworthy. A chart-spec adapter that silently maps the wrong axis, or a disclosure that renders `python_code` where `workspace_sql` belongs, is exactly the class of bug that no type checker catches and that destroys the credibility the rest of this system was built for.

**Interfaces:**
- `frontend/package.json` scripts gain `"test": "vitest run"` and `"test:watch": "vitest"`.
- devDependencies gain `vitest`, `@vitejs/plugin-react`, `jsdom`, `@testing-library/react`, `@testing-library/jest-dom`, `@testing-library/user-event`.
- `vitest.config.ts`: `environment: "jsdom"`, `setupFiles: ["./src/test/setup.ts"]`, and the `@/*` → `./src/*` alias mirrored from `tsconfig.json` (Vitest does not read `tsconfig` paths on its own — a missing alias makes every import in every test fail with a confusing module-not-found).
- `src/test/setup.ts` imports `@testing-library/jest-dom/vitest`.

**A recharts caveat that will otherwise eat an hour:** `ResponsiveContainer` renders nothing in jsdom because the container has zero size. Tests touching `ChartRenderer` must either mock `ResponsiveContainer` or assert on the adapter's *config output* rather than the rendered SVG. Task 11 is written to assert on the adapter, for this reason.

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/src/components/__tests__/ChartRenderer.test.tsx
import { render, screen } from '@testing-library/react';
import { ChartRenderer } from '@/components/ChartRenderer';

test('renders a placeholder when there is no data', () => {
  render(<ChartRenderer data={[]} config={{ type: 'BarChart', xKey: 'x', series: [] }} />);
  expect(screen.getByText('No data available')).toBeInTheDocument();
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run`
Expected: FAIL — vitest not installed.

- [ ] **Step 3: Implement**

```bash
cd frontend && npm i -D vitest @vitejs/plugin-react jsdom @testing-library/react @testing-library/jest-dom @testing-library/user-event
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd frontend && npx vitest run && npx tsc --noEmit`
Expected: PASS, 0 type errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/vitest.config.ts frontend/src/test frontend/src/components/__tests__ && git commit -m "chore(frontend): add vitest + testing-library harness"
```

---

### Task 6: The API layer, the payload types, and the streaming client

**Files:**
- Create: `frontend/src/lib/api.ts`, `frontend/src/lib/streamAnswer.ts`, `frontend/src/types/analysis.ts`
- Modify: `frontend/src/store/useStore.ts`
- Test: Create `frontend/src/lib/__tests__/streamAnswer.test.ts`

**Why types first.** Everything from Task 9 on renders `analysis`. Typed once, in one file, means a backend field rename surfaces as a compile error instead of an `undefined` in a stakeholder's face. Today `StakeholderMessage.chart_config` is `any` and `chart_data` is `any[]`; do not extend that pattern to the artifact.

**Interfaces:**
- `frontend/src/types/analysis.ts` — mirrors of Plan A's dataclasses. Every field optional-safe (`?`) where an older persisted row may lack it, because `get_conversation` replays rows written before Plan A shipped:
  ```ts
  export type ChartSpec = { kind: string; x: string; y: string | string[];
                            series?: string; title?: string };
  export type CoverageVerdict = { decision: 'reuse'|'extend'|'retrieve'; label?: string;
                                  missing_columns?: string[];
                                  missing_time_ranges?: [string, string][]; reason?: string };
  export type ExtractMeta = { label: string; description?: string; grain?: string[];
                              columns?: string[]; row_count?: number; truncated?: boolean;
                              grain_violated?: boolean; sql?: string;
                              attributions?: AttributionRule[]; created_at?: string };
  export type AnalysisArtifact = { question?: string; plan_rationale?: string;
                                   semantics_used?: string[]; unresolved_terms?: string[];
                                   requirement?: Record<string, unknown>;
                                   coverage?: CoverageVerdict; datasets_used?: string[];
                                   warehouse_sql?: string[]; workspace_sql?: string[];
                                   python_code?: string[]; result_summary?: unknown;
                                   chart_spec?: ChartSpec | null; key_findings?: string[];
                                   assumptions?: string[]; created_at?: string };
  export type PipelineStep = 'understanding'|'planning'|'checking_workspace'
                           | 'retrieving'|'analysing'|'interpreting';
  export type StepEvent = { step: PipelineStep; state: 'start'|'done'|'skipped';
                            label: string; detail?: string; elapsed_ms?: number };
  ```
  `StakeholderMessage` gains `analysis?: AnalysisArtifact` and `extract_meta?: ExtractMeta`.
- `frontend/src/lib/api.ts`:
  ```ts
  export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000';
  export function apiUrl(path: string): string;              // joins, no double slashes
  export function extractDownloadUrl(t: string, c: string, label: string): string;
  ```
  Move every hardcoded `http://localhost:8000` in `useStore.ts` behind `apiUrl`. This is a mechanical find-and-replace and belongs here, not spread across later tasks.
- `frontend/src/lib/streamAnswer.ts`:
  ```ts
  export type StreamHandlers = {
    onStep: (e: StepEvent) => void;
    onAnswer: (m: StakeholderMessage) => void;
    onError: (detail: string) => void;
  };
  export async function streamAnswer(
    tenantId: string, question: string, conversationId: string,
    handlers: StreamHandlers, signal?: AbortSignal): Promise<void>;
  ```

**The parser is where the bugs live — write it deliberately.** A `ReadableStream` chunk boundary lands anywhere, including mid-JSON. Buffer, split on `\n\n`, and keep the trailing partial frame in the buffer for the next chunk. Never `JSON.parse` a fragment. Handle three terminal conditions distinctly: an `answer` event (success), an `error` event (failure with a detail), and the stream ending with neither (failure, detail `"the connection closed before an answer arrived"`).

**Fall back rather than break.** If `res.body` is null or `getReader` is unavailable, fall back to `POST /answer` and call `onAnswer` with the result. The blocking route still exists precisely so streaming can be optional.

- [ ] **Step 1: Write the failing tests**

```ts
// frontend/src/lib/__tests__/streamAnswer.test.ts
function streamOf(...chunks: string[]): Response { /* build a Response with a ReadableStream */ }

test('parses step events then the answer', async () => {
  const steps: StepEvent[] = []; let answer: any = null;
  vi.spyOn(global, 'fetch').mockResolvedValue(streamOf(
    'event: step\ndata: {"step":"planning","state":"done","label":"Planning"}\n\n',
    'event: answer\ndata: {"answer_id":"a1","answer":"hi"}\n\n'));
  await streamAnswer('t', 'q', 'c1', { onStep: e => steps.push(e),
                                       onAnswer: m => (answer = m), onError: () => {} });
  expect(steps).toHaveLength(1);
  expect(answer.answer_id).toBe('a1');
});

test('a frame split across chunk boundaries is reassembled', async () => {
  vi.spyOn(global, 'fetch').mockResolvedValue(streamOf(
    'event: answer\ndata: {"answer_id":"a', '1","answer":"hi"}\n\n'));
  ...expect onAnswer called once with answer_id 'a1'...
});

test('an error event reports the detail and never calls onAnswer', async () => { ... });

test('a stream that ends without an answer is an error', async () => {
  ...only a step event, then close -> onError called, onAnswer not called...
});

test('falls back to the blocking route when the body is not readable', async () => { ... });

test('abort stops reading without calling onError', async () => { ... });
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run src/lib`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Rewrite `askStakeholder` in `useStore.ts` to call `streamAnswer`, pushing `steps` into a new `stakeholder.steps: StepEvent[]` and appending the final message exactly as today. Keep `loading`, keep the `try/catch` + `console.error(e)` shape, and **clear `steps` at the start of each ask** — a stale trail from the previous question next to a new answer is worse than no trail.

- [ ] **Step 4: Run**

Run: `cd frontend && npx vitest run && npx tsc --noEmit`
Expected: PASS, 0 errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib frontend/src/types frontend/src/store/useStore.ts && git commit -m "feat(frontend): typed analysis payloads + streaming answer client"
```

---

### Task 7: assistant-ui and the `ExternalStoreRuntime` adapter

**Files:**
- Create: `frontend/src/runtime/useStakeholderRuntime.ts`
- Modify: `frontend/package.json`
- Test: Create `frontend/src/runtime/__tests__/useStakeholderRuntime.test.tsx`

**Read the installed package before writing against it.** `frontend/AGENTS.md` already mandates this discipline for Next.js, and it applies here with more force: assistant-ui moves quickly and the exports below are the *expected* shape, not a verified one. **Install first, then read `node_modules/@assistant-ui/react`'s own types and docs, and let the installed version win over anything written here.** If the runtime hook's name or option keys differ, adapt this task and note the difference in the commit message — do not force the code to match this document.

```bash
cd frontend && npm i @assistant-ui/react @assistant-ui/react-markdown && \
  npm ls @assistant-ui/react && ls node_modules/@assistant-ui/react
```

**Expected shape (verify, do not assume):**
```ts
const runtime = useExternalStoreRuntime({
  isRunning,                    // stakeholder.loading
  messages,                     // our own message array
  convertMessage,               // ours -> assistant-ui's ThreadMessage
  onNew: async (m) => { ... },  // composer submit -> askStakeholder
});
// <AssistantRuntimeProvider runtime={runtime}> … </AssistantRuntimeProvider>
```

**The one non-obvious mapping, and the reason this is its own task.** A `StakeholderMessage` holds *both* the question and the answer. assistant-ui's thread wants alternating `user` / `assistant` messages. The adapter must therefore expand each `StakeholderMessage` into **two** thread messages:

- `{ role: "user", id: \`${answer_id}:q\`, content: [{ type: "text", text: question }] }`
- `{ role: "assistant", id: answer_id, content: [{ type: "text", text: answer }] }`

and carry the whole `StakeholderMessage` on the assistant message so `AnalystMessage` can reach `analysis`, `extract_meta`, `caveats`, and `feedback` without a second lookup. Two consequences the implementer must get right:

- **Ids must be stable and unique across a re-render**, or assistant-ui remounts every message on each store update and the thread scroll jumps. Derive them from `answer_id`, never from the array index.
- **A turn in flight has no `answer_id` yet.** Represent it with a fixed sentinel id (`"pending"`) so it does not collide with a real turn and does not change identity when the answer arrives. On arrival the pending pair is replaced by the real pair — assert this in a test, because a duplicated final message is the classic symptom.

**Interfaces:**
```ts
export function useStakeholderRuntime(): AssistantRuntime;
export function toThreadMessages(messages: StakeholderMessage[],
                                 pending?: { question: string }): ThreadMessage[];
```
`toThreadMessages` is exported separately and kept pure precisely so it can be tested without rendering a runtime.

- [ ] **Step 1: Write the failing tests**

```tsx
test('each stakeholder message expands into a user and an assistant message', () => {
  const out = toThreadMessages([{ answer_id: 'a1', question: 'q1', answer: 'ans' } as any]);
  expect(out.map(m => m.role)).toEqual(['user', 'assistant']);
  expect(out[1].id).toBe('a1');
});

test('ids are stable across calls and unique', () => {
  const msgs = [{ answer_id: 'a1' }, { answer_id: 'a2' }] as any;
  const ids = toThreadMessages(msgs).map(m => m.id);
  expect(new Set(ids).size).toBe(ids.length);
  expect(toThreadMessages(msgs).map(m => m.id)).toEqual(ids);
});

test('a pending question appears once and is replaced, not duplicated', () => {
  const pending = toThreadMessages([], { question: 'q1' });
  expect(pending).toHaveLength(1);
  const settled = toThreadMessages([{ answer_id: 'a1', question: 'q1', answer: 'ans' } as any]);
  expect(settled.filter(m => m.role === 'user')).toHaveLength(1);
});

test('the assistant message carries the raw stakeholder message', () => {
  const out = toThreadMessages([{ answer_id: 'a1', analysis: { datasets_used: ['df_1'] } } as any]);
  expect((out[1] as any).__source.analysis.datasets_used).toEqual(['df_1']);
});

test('submitting through the runtime calls askStakeholder', async () => { ... });
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run src/runtime`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run**

Run: `cd frontend && npx vitest run && npx tsc --noEmit`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/src/runtime && git commit -m "feat(frontend): assistant-ui external-store runtime for the stakeholder thread"
```

---

### Task 8: `AnalystThread` — the new chat shell

**Files:**
- Create: `frontend/src/components/AnalystThread.tsx`, `frontend/src/components/analyst/AnalystMessage.tsx`, `frontend/src/components/analyst/Disclosure.tsx`
- Modify: wherever `StakeholderChat` is mounted (`grep -rn "StakeholderChat" frontend/src`)
- Test: Create `frontend/src/components/__tests__/AnalystThread.test.tsx`

**Do not delete `StakeholderChat.tsx` in this task.** Both exist until Task 13 verifies the replacement. Deleting early means a broken chat with no way back.

**What comes from the library and what does not.** Thread scroll, the composer, message grouping, markdown, and keyboard behaviour are assistant-ui's. `AnalystMessage` is ours and owns everything below the answer prose: caveats, chart, disclosures, extract download, feedback, storyline checkbox. Wire assistant-ui's assistant-message slot to `AnalystMessage`; the exact primitive for that is version-specific — read the installed package.

**Carry these forward from `StakeholderChat.tsx`; they are real behaviour, not incidental:**
- `ConversationHistorySidebar` (lines 29-117) — port as-is into `frontend/src/components/analyst/ConversationSidebar.tsx`. It works. The only change is moving its `fetch` calls behind `apiUrl`.
- The `answer_mode` pill (line 216-218).
- Thumbs up/down with `aria-pressed` and `aria-label` (lines 221-242) — the accessibility attributes are correct and must survive.
- `useEffect(() => { fetchConversations(); startNewConversation(); }, [tenantId, ...])` on mount.

**`Disclosure` is the shared primitive** replacing `CollapsibleCode`: a `▸`/`▾` toggle with `aria-expanded`, a label, an optional count badge, and arbitrary children — collapsed by default, always. Every disclosure in Tasks 10-12 uses it. Code content goes in a `<pre><code>` with `overflow-x: auto`; never `dangerouslySetInnerHTML`.

**Markdown, finally.** The answer text has always been markdown and has always been rendered as a plain `<p>` (line 220), so every list and bold in every answer has been shown as literal asterisks. Render it with `@assistant-ui/react-markdown`, **with raw HTML disabled**.

- [ ] **Step 1: Write the failing tests**

```tsx
test('renders the answer prose and the mode pill', () => { ... });
test('markdown in the answer is rendered, not shown as asterisks', () => {
  ...answer "**bold**" -> expect a <strong>, and no literal "**"...
});
test('raw html in an answer is escaped, not executed', () => {
  ...answer '<img src=x onerror=alert(1)>' -> no <img> in the DOM...
});
test('disclosures are collapsed by default', () => {
  expect(screen.queryByText(/SELECT/)).not.toBeInTheDocument();
});
test('clicking a disclosure reveals its content and flips aria-expanded', async () => { ... });
test('feedback buttons keep their aria-pressed state', async () => { ... });
test('the composer submits to askStakeholder', async () => { ... });
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run src/components/__tests__/AnalystThread.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run**

Run: `cd frontend && npx vitest run && npx tsc --noEmit`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/AnalystThread.tsx frontend/src/components/analyst frontend/src/app && git commit -m "feat(frontend): assistant-ui analyst thread replaces the hand-rolled chat"
```

---

### Task 9: `StepTrail` — show the pipeline working

**Files:**
- Create: `frontend/src/components/analyst/StepTrail.tsx`
- Test: Create `frontend/src/components/analyst/__tests__/StepTrail.test.tsx`

**Interfaces:**
```ts
export function StepTrail({ steps, running }: { steps: StepEvent[]; running: boolean }): JSX.Element | null;
```

**Behaviour — the details that make it useful rather than decorative:**
- Render the six `PIPELINE_STEPS` in fixed order, so the shape of the pipeline is legible even before any event arrives. A step with no event yet is pending; `state: "start"` is active; `"done"` is complete; `"skipped"` is greyed with its `detail`.
- **`detail` is the payload.** *"Checking the workspace — reused df_1 (412,003 rows), no warehouse query needed"* is the sentence that tells a user why this answer took two seconds instead of ninety. Show it inline, always, not on hover.
- **Skipped is a feature, not a gap.** "Retrieving — skipped, the workspace already covers this" is Plan A's entire value proposition rendered as one line. Style it as deliberately-not-run, never as failed.
- Show `elapsed_ms` on completed steps, formatted (`1.2s`, `340ms`).
- When `running` is false and the turn is complete, **collapse the trail into a one-line summary** inside a `Disclosure` — `"6 steps · 2.4s · no warehouse query"`. A finished trail sitting expanded above every historical answer is exactly the clutter §8 warns about.
- Return `null` when `steps` is empty and `running` is false.
- Respect `prefers-reduced-motion` on any transition.

- [ ] **Step 1: Write the failing tests**

```tsx
test('renders all six steps in pipeline order before any event arrives', () => { ... });
test('a skipped step shows its reason and is not styled as an error', () => {
  render(<StepTrail running steps={[{ step:'retrieving', state:'skipped',
    label:'Retrieving', detail:'the workspace already covers this' }]} />);
  expect(screen.getByText(/already covers this/)).toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});
test('the coverage reason is shown inline, not hidden behind a hover', () => { ... });
test('a completed trail collapses to a one-line summary', () => {
  ...running=false, all six done -> the details are not in the document until expanded...
});
test('elapsed time is formatted', () => { ...1240 -> "1.2s"; 340 -> "340ms"... });
test('renders nothing when idle with no steps', () => { ... });
```

- [ ] **Step 2: Run to verify it fails** — `cd frontend && npx vitest run src/components/analyst`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** — `cd frontend && npx vitest run && npx tsc --noEmit` → PASS
- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/analyst/StepTrail.tsx frontend/src/components/analyst/__tests__ && git commit -m "feat(frontend): live pipeline step trail"
```

---

### Task 10: `AnalysisDisclosures` — the artifact becomes inspectable

**Files:**
- Create: `frontend/src/components/analyst/AnalysisDisclosures.tsx`
- Test: Create `frontend/src/components/analyst/__tests__/AnalysisDisclosures.test.tsx`

**This is the task the whole of Plan A was building toward.** Everything recorded per turn becomes visible here, and nothing else in this plan matters as much.

**Interfaces:**
```ts
export function AnalysisDisclosures({ analysis, extractMeta, tenantId, conversationId }: {
  analysis?: AnalysisArtifact; extractMeta?: ExtractMeta;
  tenantId: string; conversationId: string;
}): JSX.Element | null;
```

**Four disclosures, in this order, all collapsed by default:**

1. **▸ Data used** — for each `datasets_used` label: grain (`one row per session_id`), row count, time range, columns, and the coverage line (`reused` / `extended` / `retrieved`, with `coverage.reason` verbatim). Truncation and grain violations are called out **here in red**, not buried — `"truncated at 1,000,000 rows — totals and rates may be understated"`. Hosts the extract download button from Task 11.
2. **▸ SQL** — warehouse SQL and workspace SQL as **separately labelled** blocks. Conflating them is a correctness problem, not a cosmetic one: *"this ran against Athena"* and *"this ran locally over cached Parquet"* are different claims about where a number came from. Label them `Warehouse (Athena)` and `Workspace (DuckDB, local)`. Show a count badge when there is more than one.
3. **▸ Analysis code** — each `python_code` cell, with its result summary if present.
4. **▸ Methodology** — `plan_rationale`, `semantics_used` (the metric definitions that governed the answer), `requirement` rendered as a small key/value table rather than raw JSON, and `assumptions` — including every attribution rule applied, in the sentence form Plan A specified: *"service_line attributed to each session by highest intent (mobile > fixed > ott); sessions touching multiple service lines are counted once, under their highest-ranked one."*

**Uncertainty is not a disclosure.** `unresolved_terms` and the truncation/grain caveats must be **visible in the message body**, above the fold, not hidden one click away. Plan A's entire uncertainty mechanism is defeated by a UI that files "this is not a defined metric" under Methodology. `AnalysisDisclosures` renders the detail; `AnalystMessage` renders the warning.

**Empty sections do not render.** A turn with no Python produces no "Analysis code" disclosure — not an empty one. An `analysis` that is `undefined` (a pre-Plan-A row replayed from the database) renders `null` and the message still works.

- [ ] **Step 1: Write the failing tests**

```tsx
test('all four disclosures render collapsed for a full artifact', () => {
  render(<AnalysisDisclosures analysis={full} .../>);
  ['Data used','SQL','Analysis code','Methodology'].forEach(l =>
    expect(screen.getByRole('button', { name: new RegExp(l, 'i') })).toHaveAttribute('aria-expanded','false'));
});

test('warehouse and workspace SQL are labelled separately', async () => {
  ...expand SQL -> expect /Warehouse/ and /Workspace/ and /DuckDB/...
});

test('a section with no content does not render', () => {
  render(<AnalysisDisclosures analysis={{ ...full, python_code: [] }} .../>);
  expect(screen.queryByRole('button', { name: /Analysis code/i })).not.toBeInTheDocument();
});

test('a truncated extract is called out in Data used', async () => {
  ...expand -> expect /truncated at 1,000,000/ and /understated/...
});

test('a grain violation is surfaced', async () => { ...expect /double-counted/i... });

test('the coverage reason is shown verbatim', async () => {
  ...expect(screen.getByText(/reused df_1/))...
});

test('attribution rules are stated as sentences in Methodology', async () => {
  ...expect /attributed to each session by highest intent/...
});

test('an undefined analysis renders nothing and does not throw', () => {
  const { container } = render(<AnalysisDisclosures tenantId="t" conversationId="c1" />);
  expect(container).toBeEmptyDOMElement();
});

test('sql is rendered as text, never as html', () => {
  ...warehouse_sql containing "<script>" -> no script element, literal text present...
});
```

- [ ] **Step 2: Run to verify it fails** — `cd frontend && npx vitest run src/components/analyst`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** — `cd frontend && npx vitest run && npx tsc --noEmit` → PASS
- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/analyst && git commit -m "feat(frontend): render the analysis artifact as data/sql/code/methodology disclosures"
```

---

### Task 11: The chart, and the raw data

**Files:**
- Create: `frontend/src/components/analyst/AnalysisChart.tsx`, `frontend/src/components/analyst/ExtractDownload.tsx`
- Test: Create `frontend/src/components/analyst/__tests__/AnalysisChart.test.tsx`

**Interfaces:**
```ts
// Pure, exported separately so it is testable without recharts in jsdom.
export function specToConfig(spec: ChartSpec): ChartConfig | null;
export function AnalysisChart({ spec, data }: { spec?: ChartSpec | null; data?: unknown[] }): JSX.Element | null;
export function ExtractDownload({ tenantId, conversationId, meta }: {...}): JSX.Element | null;
```

**The mapping, spelled out because a wrong axis is a silent lie.** Plan A's sandbox emits `{kind, x, y, series, title}` where `kind` is a plain word (`bar`, `line`, `area`, `scatter`). `ChartRenderer` wants `{type: "BarChart"|…, xKey, series: [{key, name?, color?}]}`.

- `kind` → `type`: `bar→BarChart`, `line→LineChart`, `area→AreaChart`, `scatter→ScatterChart`. Case-insensitive. **An unknown `kind` returns `null`** — render nothing rather than guessing a chart type. A wrong chart is worse than no chart.
- `x` → `xKey`.
- `y` is a string or an array of strings → one `series` entry each, `name` defaulting to the key.
- `series` (the grouping column) is **not** a recharts concept and must not be silently dropped: when present and `y` is a single key, note it in the chart caption (`"by service_line"`) so the reader knows the data is grouped. Do not attempt to pivot in the client.
- A missing `x`, an empty `y`, or empty `data` → `null`.

**Legacy path stays.** `AnalystMessage` prefers `analysis.chart_spec` and falls back to the existing `chart_config`/`chart_data` when it is absent, so historical turns keep their charts.

**`ExtractDownload`** renders only when `extract_meta.label` exists: a button linking to `extractDownloadUrl(...)` labelled `Download df_1 (412,003 rows, CSV)`, plus the truncation note when `truncated`. Plain `<a download>`; the endpoint already sets `Content-Disposition` and CORS already exposes it.

- [ ] **Step 1: Write the failing tests**

```tsx
test('maps kind to a recharts type', () => {
  expect(specToConfig({ kind:'bar', x:'country', y:'revenue' })!.type).toBe('BarChart');
  expect(specToConfig({ kind:'LINE', x:'d', y:'r' })!.type).toBe('LineChart');
});
test('an array y becomes one series per key', () => {
  expect(specToConfig({ kind:'line', x:'d', y:['a','b'] })!.series.map(s=>s.key)).toEqual(['a','b']);
});
test('an unknown kind renders nothing rather than guessing', () => {
  expect(specToConfig({ kind:'sankey', x:'a', y:'b' })).toBeNull();
});
test('a missing x or empty y yields null', () => { ... });
test('a grouping series is surfaced in the caption, not dropped', () => {
  render(<AnalysisChart spec={{kind:'bar',x:'d',y:'r',series:'service_line'}} data={[{d:1,r:2}]} />);
  expect(screen.getByText(/service_line/)).toBeInTheDocument();
});
test('the download link points at the extract endpoint and names the row count', () => {
  render(<ExtractDownload tenantId="t" conversationId="c1"
                          meta={{ label:'df_1', row_count:412003 }} />);
  const a = screen.getByRole('link');
  expect(a).toHaveAttribute('href', expect.stringContaining('/extracts/df_1/download'));
  expect(a).toHaveTextContent('412,003');
});
test('a truncated extract says so next to the download', () => { ... });
```

- [ ] **Step 2: Run to verify it fails** — `cd frontend && npx vitest run src/components/analyst`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** — `cd frontend && npx vitest run && npx tsc --noEmit` → PASS
- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/analyst && git commit -m "feat(frontend): render chart specs and offer the raw extract as CSV"
```

---

### Task 12: Storyline selection in the thread, and the new export formats

**Files:**
- Modify: `frontend/src/components/analyst/AnalystMessage.tsx`, `frontend/src/components/AnalystThread.tsx`, `frontend/src/store/useStore.ts`
- Create: `frontend/src/components/analyst/StorylinePanel.tsx`
- Test: Create `frontend/src/components/analyst/__tests__/StorylinePanel.test.tsx`

**The change in shape.** Today's `ReportBuilderPanel` lists question stubs in a side panel, disconnected from the answers they name. Selection moves to a checkbox on each turn — *"include this in the report"* — and the panel keeps only what is genuinely global: the count, the token estimate, the narrate toggle, the format picker, the export button, and the error region.

**Carry these forward exactly:**
- `WARN_TOKEN_THRESHOLD = 50_000` **with its comment** tying it to `storyline.py`.
- `estimateTokens` (lines 119-125) — same formula, so the warning stays consistent with the backend's.
- The over-budget message and its `var(--error)` styling.
- The `exportError` alert region with `role="alert"` (lines 175-179). This was added deliberately to surface backend 400/404/503 failures that were previously silent — it must not regress.
- The `exportStoryline` download flow (blob → object URL → anchor → revoke), including the `content-disposition` filename parse.

**Additions:**
- Format picker gains `pdf` and `pptx`. Handle 503 for a missing renderer through the existing `exportError` path — the message the backend sends (`"pdf export unavailable: reportlab not installed"`) is already user-legible.
- A **"Write a narrative"** checkbox setting `narrate: true` on the request, with a one-line hint that it costs an extra LLM call. Default off.
- Disable the export button while exporting and when nothing is selected — both already true today; keep them.

- [ ] **Step 1: Write the failing tests**

```tsx
test('a turn checkbox toggles it into the selection', async () => { ... });
test('the panel count reflects in-thread selection', async () => { ... });
test('the token estimate warns over the threshold', () => {
  ...selection estimating > 50_000 -> expect /consider selecting fewer turns/...
});
test('pdf and pptx are offered', () => {
  expect(screen.getByRole('option', { name: /pdf/i })).toBeInTheDocument();
  expect(screen.getByRole('option', { name: /powerpoint|pptx/i })).toBeInTheDocument();
});
test('a 503 from a missing renderer is shown in the alert region', async () => {
  ...mock fetch 503 {"detail":"pdf export unavailable: reportlab not installed"}...
  expect(await screen.findByRole('alert')).toHaveTextContent(/reportlab not installed/);
});
test('the narrate toggle is off by default and reaches the request body', async () => { ... });
test('export is disabled with an empty selection', () => { ... });
```

- [ ] **Step 2: Run to verify it fails** — `cd frontend && npx vitest run src/components/analyst`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** — `cd frontend && npx vitest run && npx tsc --noEmit` → PASS
- [ ] **Step 5: Commit**

```bash
git add frontend/src/components frontend/src/store/useStore.ts && git commit -m "feat(frontend): in-thread storyline selection, narrated exports, pdf/pptx"
```

---

### Task 13: Retire the old chat and verify the whole surface

**Files:**
- Delete: `frontend/src/components/StakeholderChat.tsx`
- Modify: any remaining import of it (`grep -rn "StakeholderChat" frontend/src`)
- Test: full suite, both sides

**Only after `AnalystThread` is verified working in the browser.** Do the manual verification below *first*, then delete.

Check before deleting that nothing in `StakeholderChat.tsx` is still uniquely load-bearing:
- `ConversationHistorySidebar` → ported in Task 8
- `estimateTokens` + `WARN_TOKEN_THRESHOLD` + its comment → ported in Task 12
- `CollapsibleCode` → superseded by `Disclosure`
- the `answer_mode` pill and the feedback buttons with their aria attributes → ported in Task 8

If any of these has no home in the new tree, port it before deleting rather than after.

- [ ] **Step 1:** Run the manual verification below, end to end
- [ ] **Step 2:** `grep -rn "StakeholderChat" frontend/src` → only the file itself
- [ ] **Step 3:** Delete it and fix any import
- [ ] **Step 4:** `cd frontend && npx vitest run && npx tsc --noEmit && npm run build` → all clean; `.venv/bin/python -m pytest tests/ -q` → PASS
- [ ] **Step 5: Commit**

```bash
git add -A frontend/src && git commit -m "chore(frontend): remove the hand-rolled stakeholder chat"
```

---

## Verification

**Automated**

```bash
.venv/bin/python -m pytest tests/ -q
```

Expected: every Plan A test still passing, plus `test_answer_stream.py`, `test_narrative.py`, `test_storyline_pdf.py`, `test_storyline_pptx.py`, and the `test_api.py` additions. **`test_stakeholder.py` and `test_extract_flow.py` must pass entirely unchanged** — if a Plan A test needed editing, `answer()`'s contract was broken and Task 1 is wrong.

```bash
cd frontend && npx vitest run && npx tsc --noEmit && npm run build
```

**End-to-end, in the browser.** Boot the backend and the frontend:

```bash
./start_session.command
```

1. **Ask a first analytical question and watch the trail.** The six steps must appear *progressively*, not all at once at the end. All-at-once means the response is being buffered — check `X-Accel-Buffering` and that `StreamingResponse` got a sync iterator, not a materialised list.

2. **Read the answer.** Markdown renders (bold is bold, lists are lists — this has never worked). The answer prose is visible; no SQL, no Python, no JSON is visible without a click.

3. **Open each disclosure.** ▸ Data used names the extract, its grain, its row count, and the coverage decision. ▸ SQL shows the warehouse query under a *Warehouse (Athena)* label. ▸ Analysis code shows the Python cell. ▸ Methodology shows the metric definitions that governed the answer. **If a disclosure is empty rather than absent, that is a bug.**

4. **Ask a pure re-cut follow-up** (*"now break that down by service line"*). The headline check, now visual: the trail shows **Retrieving — skipped**, ▸ Data used says *reused df_1*, and ▸ SQL shows only a *Workspace (DuckDB, local)* block. A *Warehouse (Athena)* block here means Plan A's Data Manager is not doing its job and this is a Plan A regression, not a UI bug.

5. **Download the extract** from ▸ Data used. The file arrives with a sensible filename — that is the CORS `expose_headers` path, which `call()` cannot test.

6. **Check the chart.** A turn whose plan chose `analysis: "python"` and produced a `chart_spec` renders a chart. Confirm the x-axis is the column the spec named. If the spec had a grouping `series`, the caption says so.

7. **Ask about an undefined measure** (*"what is our churn rate?"*). The caveat is visible **in the message body**, not inside Methodology. This is the check that Plan A's uncertainty mechanism survived contact with the UI.

8. **Select three turns in the thread**, tick *Write a narrative*, export as PDF and as PPTX. The PDF opens with an executive summary and reads as one argument. The deck has a title slide, one slide per section, a caveats slide, and no SQL. Every figure in both must appear in one of the three turns — **spot-check at least two numbers against the thread.**

9. **Export without the narrative toggle.** The turn-by-turn document must still be produced. Narration is an enhancement, never a dependency.

10. **Uninstall one renderer** (`.venv/bin/pip uninstall -y reportlab`) and export as PDF. A red alert reads *"pdf export unavailable: reportlab not installed"* — not a silent no-op. Reinstall afterwards.

11. **Kill the backend mid-answer.** The UI reports a failed turn rather than spinning forever. Restart, reopen the conversation, and confirm the replayed turns still render their disclosures from the persisted `analysis`.

12. **Resize to a narrow window.** SQL and code blocks scroll inside their own containers; the page body does not scroll sideways.

**Cost note to watch:** narrated exports add one LLM call per export, over a prompt containing every selected turn — a ten-turn selection is a large prompt. That is why the toggle defaults off. Streaming adds no model cost; it only changes when bytes are delivered.

---

## Open decisions for the user

Three things in this plan are judgement calls rather than consequences of Plan A. All three have a default; none needs an answer before Task 1.

1. **Frontend test harness (Task 5).** Vitest + Testing Library is a genuine dependency addition to a repo that has never had a frontend test runner. **Default: add it** — Tasks 9-12 render a provenance surface whose value is being trustworthy, and `tsc` catches none of the bugs that would break that. Strike it and those six tasks are verified by types and the browser only.

2. **`reportlab` and `python-pptx` (Task 4).** Both are optional imports behind the existing `DocxRendererUnavailable` pattern, so the suite passes without them. **Default: add both.** If only one format matters, say which and the other task is dropped.

3. **POST-streaming instead of `GET …/answer/stream` (Task 2).** This deviates from Plan A's sketch, for the reasons in the Decisions table — questions in URLs get logged. **Default: POST.** The wire format stays SSE-shaped either way, so reversing it later is a small change.
