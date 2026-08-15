# Processing Visibility: live progress for users, structured traces for developers

## Context

Tracing one real stakeholder run (`ans_13ca1f57bc6e`, question about consent→order drop-off for DE)
exposed how little of the pipeline is observable. That request took **642,516 ms (10m 42s)** and
persisted exactly **one** telemetry row — `stage="stakeholder.answer"`, `status="OK"` — plus the two
SQL statements that finally worked.

Everything that actually mattered was invisible:

- **The search keywords.** `_extract_search_intent` ([stakeholder.py:94](analytics_platform/stakeholder.py:94))
  LLM-distills the question to a 2–4 word topic before retrieval. That string is never recorded, so
  what was actually searched is unrecoverable.
- **What retrieval found.** The skill branch is only reachable after both the SQL-synthesis branch and
  the approved-query-reuse branch fall through, so we know up to 3 synthesis attempts and up to 3
  verbatim reuses failed — but not which nodes, not which SQL, not which errors. Re-running retrieval
  by hand afterwards showed a near-exact match ("Consent Page Drop-off and Exit Attribution Analysis")
  was retrieved and then discarded silently.
- **Why the warehouse was hit ~8 times.** Each failed attempt is a live Metabase round-trip. That is
  where the 10 minutes went, and none of it is attributable.
- **The truncation.** `LIMIT 5` in the template → `.head(5)` preview → `rows[:3]` at
  [stakeholder.py:784](analytics_platform/stakeholder.py:784). The final answer hedges about "top 3
  rows" and nothing in the record explains why.

The error text from those failures is gone for good: `tmp/api.log` is written by a dedicated
`api_access` logger with `propagate = False` ([api.py:18](analytics_platform/api.py:18)), so module
warnings never reach any file.

**Outcome wanted:** a stakeholder watching a 10-minute run sees what the system is doing as it happens;
a developer debugging it afterwards can replay every stage, prompt, query, and failure from disk.

## What already exists (reuse, don't rebuild)

The transport is already built and working — only the emitters are missing.

- `Observability.event()` ([observability.py:52](analytics_platform/observability.py:52)) persists to the
  per-tenant `telemetry` table **and** calls `self.on_event`.
- `EventBroadcaster.dispatch` ([api.py:482](analytics_platform/api.py:482)) is wired to `on_event` at
  startup and pushes to `/ws/tenants/{tenant_id}/activity`.
- `src/app/junior/page.tsx:53` already consumes that socket with a reconnect loop — copy the pattern.
- `Observability.span()` is an existing timing context manager.
- `LLMClient` is a clean `Protocol` ([llm/client.py:26](analytics_platform/llm/client.py:26)) — one
  decorator instruments every persona's LLM calls at once.
- `CollapsibleCode` in `StakeholderChat.tsx` is the established disclosure pattern for SQL/Python.

**Net effect: every `obs.event()` call already streams to the browser live.** The stakeholder pipeline
just never emits any.

## Design: two core primitives, four surfaces

Both primitives go in the core platform (tenant-agnostic, per AGENTS.md "Core vs. Tenant Development").

### 1. `TraceRecorder` — `analytics_platform/observability.py`

A thin per-request object bound to `(tenant_id, trace_id)`, constructed from an `Observability`:

- `.step(stage, status="OK", **meta)` — appends to an ordered in-memory list, stamps elapsed-ms since
  the previous step, and delegates to the existing `obs.event()`. Persistence and WS broadcast come
  free.
- `.substep(...)` / a `.timed(stage)` context manager wrapping `obs.span()` for warehouse and LLM calls.
- `.steps()` — the ordered trail, for persisting onto the answer row.
- Never raises. Observability must not break the pipeline — mirror the existing swallow-and-log
  contract in `Observability.event`.

Stage names namespace as `stakeholder.<phase>` so the frontend can filter, and `Observability.metrics()`
`by_stage` aggregation starts producing a real per-phase latency breakdown with no extra work.

### 2. `TracingLLMClient` — `analytics_platform/llm/client.py`

A decorator implementing the `LLMClient` protocol, wrapping any client (`GatewayClient`, `NullClient`):

```
TracingLLMClient(inner, recorder, purpose="search_intent")
```

Records duration, `tokens_in/out`, `ok`, and a truncated response preview on every `generate()`.
When `settings.trace_verbose` is on, it *also* writes the full `system_prompt`, `prompt`, and raw
response text to the JSONL trace file (never to the telemetry table — see storage split below).

This is the single change that makes search-intent distillation, skill routing, parameter extraction,
and answer synthesis all visible at once, and it generalizes to Junior/Pipeline/Storyline for free.

Add to `Settings` ([config.py:34](analytics_platform/config.py:34), same pattern as `metabase_live`):

```
trace_verbose: bool = False   # ANALYTICS_TRACE_VERBOSE=1
```

### 3. Storage split (deliberate)

| Sink | Contents | Retention |
|---|---|---|
| `telemetry` table (per-tenant DB) | stage, status, duration, tokens, counts, node ids, error text | existing |
| `stakeholder_answers.trace_steps` | the ordered step trail for replay in the UI | with the answer |
| `tmp/traces/<trace_id>.jsonl` | everything above **plus** full prompts and raw responses | dev only, flag-gated |

Full prompt bodies stay out of the tenant database — that keeps the DB lean and avoids writing
customer data into a durable multi-tenant store, while dev keeps full fidelity on local disk.

### 4. Developer log sinks

- **New `analytics_platform/logging_setup.py`** with `configure_logging(settings)`: attaches a
  `RotatingFileHandler` (~10 MB × 5) to the **root** logger → `tmp/platform.log`. Called from
  `create_app`. This is the actual fix for the vanished SQL errors — leave the existing `api_access`
  logger and its `propagate = False` untouched so `tmp/api.log` keeps its current clean access-log format.
- **`tmp/traces/<trace_id>.jsonl`** written by `TraceRecorder` when `trace_verbose` is set; one JSON
  object per step, appended as it happens so a hung run is still inspectable mid-flight.
  Add `tmp/traces/` to `.gitignore`.

## Instrumentation points in `stakeholder.py`

Build the recorder once in `answer()` right after `trace = new_trace()`, wrap `llm` in
`TracingLLMClient`, and emit:

| Stage | Key metadata |
|---|---|
| `stakeholder.classify` | category, **which marker substring matched** |
| `stakeholder.search_intent` | raw question, **distilled query string** |
| `stakeholder.retrieve` | per kind: hit count, node ids + titles, embeddings available |
| `stakeholder.compute_path` | chosen path, df_label, cached frames considered |
| `stakeholder.sql_synth` | attempt n/max, policy verdict + `decision.reasons` on denial |
| `stakeholder.sql_exec` | duration, row_count, **full error text** on failure |
| `stakeholder.query_refresh` | per node: node id, placeholders resolved, ok/error |
| `stakeholder.skill_match` | candidate skill names offered, chosen, LLM reasoning |
| `stakeholder.skill_params` | extracted params, required keys, needs_clarification |
| `stakeholder.skill_exec` | per template: filename, duration, row_count |
| `stakeholder.synthesize` | **`rows_available` vs `rows_sent_to_llm`**, chart_config emitted |
| `stakeholder.answer` | final mode + status (existing event, keep it) |

Two fall-through points currently return no signal at all and must emit a `status="FAILED"` step
before falling through — they are exactly what hid this run's behaviour:
`_synthesize_and_execute_sql` returning `(None)` after exhausting attempts
([stakeholder.py:688](analytics_platform/stakeholder.py:688)), and the `any_failed` branch at
[stakeholder.py:428](analytics_platform/stakeholder.py:428).

The `synthesize` row-count pair is deliberate: it turns the silent
`LIMIT 5` → `.head(5)` → `[:3]` funnel into a visible fact.

## Persistence

- `database.py` — add to the existing `stakeholder_answers` ALTER-TABLE block
  ([database.py:219](analytics_platform/database.py:219), same idempotent
  `if "x" not in sa_cols` pattern):
  `ALTER TABLE stakeholder_answers ADD COLUMN trace_steps TEXT`
- `_record()` ([stakeholder.py:817](analytics_platform/stakeholder.py:817)) — accept and `dump_json` the trail.
- `get_conversation()` ([stakeholder.py:163](analytics_platform/stakeholder.py:163)) — `load_json` it into
  each message, alongside the existing `queries_run` / `python_cells` handling.

## Frontend

**`frontend/src/components/StakeholderChat.tsx`**

- Subscribe to `/ws/tenants/{tenantId}/activity`, reusing the reconnect pattern from
  `src/app/junior/page.tsx:53`. Filter to `stage.startsWith('stakeholder.')`.
- While `stakeholder.loading`, render a live step list in place of the current static spinner —
  plain-language labels ("Searching the company brain…", "Writing SQL — attempt 2 of 3", "Running
  query 1 of 2") with elapsed time per step. A 10-minute run stops looking frozen.
- After the answer lands, render the persisted `trace_steps` in a **"How this was answered"**
  collapsible next to the existing SQL blocks, using the `CollapsibleCode` disclosure pattern.
  Failed steps shown in the error colour with their message — a discarded near-exact brain match
  becomes visible instead of silent.

**`frontend/src/store/useStore.ts`** — add `trace_steps` to the `StakeholderMessage` type (~line 9)
and a `liveSteps` array on the `stakeholder` slice, cleared at the start of each ask (~line 193).

Incidental: drop the stray `print(f"DEBUG: websocket_activity reached…")` at
[api.py:526](analytics_platform/api.py:526).

## Verification

1. **Unit** — a test asserting `TraceRecorder.step()` never raises when the tenant store is
   unwritable (matches the `Observability.event` contract), and that `TracingLLMClient` omits prompt
   bodies when `trace_verbose` is off. Follow existing conventions in `tests/`.
2. **Offline end-to-end** — with `ANALYTICS_MB_LIVE=0`, ask the tenant a question and assert the
   answer row has a non-empty `trace_steps` covering classify → search_intent → retrieve → synthesize.
3. **Replay the real failure** — with `ANALYTICS_MB_LIVE=1` and `ANALYTICS_TRACE_VERBOSE=1`, re-ask
   the original consent→order question. Confirm from `tmp/traces/<trace_id>.jsonl`:
   the distilled search string is present; the retrieved node ids include
   `kn_6a3627092afa`; each SQL attempt has a policy verdict and a full error; the per-stage durations
   sum to roughly the request duration and identify where the ~10 minutes goes.
4. **Live UI** — with the frontend running, submit that question and confirm steps stream into the
   chat during the run, then collapse into the "How this was answered" panel. Verify with
   `preview_start` + `read_page` / `read_console_messages`.
5. **Log sink** — confirm `tmp/platform.log` now contains the `stakeholder._refresh` and
   `synthesized SQL execution failed` warnings that previously went nowhere, and that `tmp/api.log`
   still contains only access lines.

## Out of scope (follow-ups)

- Wiring `TraceRecorder` into JuniorEngine / Pipeline / Storyline — the primitive is built for it,
  the call sites come later.
- The two substantive gaps this trace exposed in the drop-off skill itself: the "where do they go
  next" query is documented but not wired for execution, and "left the site" vs "navigated back"
  both collapse into `no error signal (passive exit)`. Product issues, not observability ones.
