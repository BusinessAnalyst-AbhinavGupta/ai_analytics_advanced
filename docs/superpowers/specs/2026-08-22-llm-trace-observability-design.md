# Full LLM + retrieval trace visibility for the analysts

**Status:** design approved, not implemented
**Date:** 2026-08-22
**Line references:** as of `e8bcd58`. They drift; the symbol names are the durable anchor.

## The problem

An answer can be fully reconstructed as *what was computed* and not at all as
*why the model chose it*.

`AnalysisArtifact` already records the population, the slice, every Athena page,
the DuckDB re-cut and the Python cell. None of the following is recorded anywhere:

- **No prompt or completion is persisted by any component.** `LLMResponse`
  (`llm/client.py:12`) carries `text, provider, model, tokens_in, tokens_out, ok`
  and never echoes the prompt back, so nothing downstream *can* record it. Only
  token counts survive a turn.
- **`search_intent` is discarded one line after it is produced.** It exists on
  exactly two lines (`stakeholder.py:423-424`): an LLM rewrites the user's
  question into a 2-4 word topic, that string becomes the sole retrieval key for
  both `brain.search` calls, and it is then dropped. When retrieval returns the
  wrong nodes the cause is unrecoverable.
- **The diagnostic probe's reasoning is unrecorded.** Its hypothesis and
  `friction_type` shape the prose, but the `interpreting` step emits `""`
  (`stakeholder.py:777`) and the artifact has no field for them. Only the probe's
  SQL survives, appended to `warehouse_sql`.
- **Retrieval itself is opaque.** Which nodes each recall leg surfaced, whether
  embeddings were even available, and whether the SQL pre-filter hit its cap are
  all invisible.

A turn makes **seven** LLM calls (`stakeholder.py` lines 203, 960, 1759, 2013,
2414, 2465, 2807). Today all seven are unobservable.

## Scope

This spec covers **observability only**. It does not change what the analyst
retrieves, plans, or answers.

Fixing the single-topic `search_intent` is deliberately a *separate, later* spec.
The trace is the instrument that measures whether that change is an improvement;
building the change first would mean altering behaviour blind and grading it blind.

## Decisions

| Decision | Choice |
|---|---|
| Sequencing | Tracing first; retrieval redesign second |
| Capture policy | Always on, all tenants, no toggle |
| Retention | Existing purge, `log_retention_days` (30 by default) |
| Surfacing | Persisted, fetched on demand per answer |
| Trace scope | LLM calls **and** brain retrieval |
| Capture mechanism | Wrap the boundaries (not call sites, not threaded state) |
| Redaction | None |

### Why wrap the boundaries

The alternatives were explicit recording at each of the seven call sites, or
threading a `TurnTrace` object through the pipeline.

Call-site recording means the eighth call someone adds is untraced by default,
and that is discovered exactly when the trace is needed. Threading state means
changing the signature of most private methods in a 3,100-line file.

A wrapper has one code path and no opt-out, which is the argument the codebase
already makes about `answer`/`answer_stream`: *"two code paths for one pipeline is
how a streamed answer and a blocking one start quietly disagreeing about what the
analyst actually did."*

Its cost is that stage attribution is implicit: a call made outside any step
boundary tags `unattributed` rather than failing loudly. That is treated as a
feature -- an unattributed call appearing in a trace is itself a finding.

### Why not `telemetry.meta`

`telemetry` rows are small analytics records read by the metrics path. Putting
50KB prompts in their JSON `meta` column would make every metrics query carry
data none of them read. A separate table gets the same `("table", "ts")`
retention treatment that `retention.py:31` already applies.

### Why no redaction

Prompts embed the tenant's own warehouse data -- the synthesis prompt carries
cube rows, the planner prompt carries the rendered schema context and the
conversation thread. Traces are written to that tenant's own database, the same
isolation boundary that already holds its extracts. A redaction pass would defeat
the stated purpose and give false confidence about what is at rest.

## Architecture

Four components and one new table.

### `TracingLLMClient` (`llm/tracing.py`)

Wraps any `LLMClient` and implements the same protocol, so nothing downstream
knows it is there. On each `generate()` it returns the real `LLMResponse` first,
then records.

Inserted at **`make_role_client`** -- the single construction point, used at
`stakeholder.py:422`, `junior.py:817,868`, `junior_worker.py:482`,
`senior.py:258` and `api.py:1371`. The
junior and senior analysts therefore get capture for free, satisfying AGENTS.md's
core-not-tenant rule better than a stakeholder-only hook would. Their calls tag
`unattributed`, because only the stakeholder pipeline sets stages.

### `turn_stage` contextvar

Set by `_step()`, which already fires at every named boundary. An LLM or search
call inherits whatever stage is current. Generators run in the caller's context,
so setting it inside `_answer_steps` propagates into `_plan_turn` and below.

### Instrumented `CompanyBrain.search`

Records the query string actually used, `kind`, `limit`, the pre-filter candidate
count and whether it hit its cap, the ids each recall leg returned, the fused
ordering, the final ids, and `embedding_available`.

### `llm_traces` table

Tenant-scoped, one row per call:

```
id, tenant_id, trace_id, seq, stage, kind, payload JSON,
ts, duration_ms, tokens_in, tokens_out, ok
```

`kind` is `llm` or `retrieval`. Keyed by the `trace_id` `answer()` already mints
per turn; `stakeholder_answers` already carries `trace_id`
(`database.py:115`), so answer -> trace joins with no schema change.

## New pipeline step: `recalling`

`_extract_search_intent` and `_retrieve` run at `stakeholder.py:423-424`, **before**
the pipeline emits `understanding/start`. Under a pure contextvar scheme the two
calls that matter most would tag `unattributed`.

Adding a seventh entry to `PIPELINE_STEPS` and `STEP_LABELS` -- `recalling`,
labelled "Recalling what we know", ordered **first** in the tuple (it runs before
`understanding`) -- emitted around those lines fixes this, and
fixes a real gap in the live trail independently: today the question rewrite and
both brain searches happen before any step event, so they are invisible while
they run. Its `detail` states the intent string and how many nodes came back.

## Record contents

### `kind: "llm"`

`stage`, `seq`, `provider`, `model`, `temperature`, `system_prompt`, `prompt`,
`response_text` (all verbatim), `tokens_in`, `tokens_out`, `ok`, `duration_ms`, `ts`.

Two properties fall out without extra work:

- **The SQL repair loop becomes readable.** `_synthesize_sql_loop` retries with
  each policy rejection or execution error fed back into the next prompt. Those
  land as consecutive `analysing` records, so attempt 1's SQL and the rejection
  reason embedded in attempt 2's prompt are both visible.
- **The planner retry becomes readable.** Two `planning` records means the first
  response did not parse, and the malformed text is there to read.

**Known limit:** the wrapper sees the response, not what the pipeline did with it.
It cannot stamp "parsed" / "failed to parse". That is inferred from a second call
appearing in the same stage. Recording parse outcomes directly would require
touching call sites, which is what this mechanism trades away.

### `kind: "retrieval"`

`query`, `kind`, `limit`, `candidate_count`, `candidate_cap_hit`, `lexical_ids`,
`dense_ids`, `fused_order`, `returned_ids`, `embedding_available`.

## Surfacing

`GET /tenants/{tenant_id}/answers/{answer_id}/trace` resolves `trace_id` from
`stakeholder_answers` and returns records ordered by `seq`, grouped by stage.
Optional `?stage=` filter. Tenant-scoped through the same `tenant_or_404` path as
every other route.

In the UI: one more disclosure panel beside the data/sql/code/methodology tabs
added in `4aa012f`. Collapsed by default -- a full trace is tens of KB.

## Failure handling

**Tracing must never break a turn.** The recorder catches every exception on the
write path and logs it; a dead write degrades to a missing record, never a failed
answer. The wrapper returns the real `LLMResponse` before attempting to record, so
a recorder crash cannot swallow a good response. This mirrors `_diagnostic_probe`,
which already treats its own failure as an improvement lost rather than an answer lost.

**Size.** Each field is capped at a configurable ceiling (default 64KB) with
truncation marked explicitly -- `"prompt_truncated": true` plus the original
length. Never silently, matching the paging loop's behaviour at
`raw_extract_row_limit`.

**Retention.** Add `("llm_traces", "ts")` to the list in `retention.py:31`. The
purge already walks it; nothing else to build.

## Testing

- The wrapper returns the response byte-identical, and `answer()`'s payload is
  unchanged with tracing on -- the equivalence `test_answer_stream.py` already
  establishes for the generator refactor.
- All seven LLM calls in a full turn produce records with correct stage attribution.
- A planner retry produces two `planning` records; a SQL repair produces N
  `analysing` records with the prior error visible in the later prompt.
- A retrieval record captures the intent string and both legs' ids; with
  embeddings off it records `embedding_available: false` and empty `dense_ids`.
- **A recorder that raises still answers the turn.**
- Retention purges `llm_traces` past the window.

## Out of scope

- Redaction of any kind.
- Any change to retrieval, planning, or answering behaviour.
- Parse-outcome stamping (see Known limit).
