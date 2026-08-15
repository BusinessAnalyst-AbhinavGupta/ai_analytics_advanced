# Plan A — Analytical Workspace

## 1. Goal

Turn the **Stakeholder Analyst** from an LLM that writes SQL into an AI analyst operating inside a persistent analytical workspace.

Core pipeline:

```text
resolve semantics
→ inspect workspace
→ reuse / widen / retrieve
→ analyse
→ interpret
→ record provenance
```

Optimise for **correctness > speed > cleverness**. The analyst must state uncertainty rather than fabricate.

Plan A is **backend-only**. Do not modify `frontend/`. Plan B will replace the hand-built chat with assistant-ui, SSE step events, and the storyline surface.

---

## 2. Current Problems / Root Causes

1. `_synthesize_sql` asks the LLM to "write a query to answer the question", so it produces pre-aggregated answers rather than reusable data.
2. `_choose_compute_path` is starved of useful workspace metadata, so follow-ups often return to SQL instead of using Python.
3. `ConversationDataCache` is memory-only; data disappears on restart.
4. Only samples reach the synthesis LLM; raw reusable data is not persisted or exposed appropriately.
5. SQL synthesis receives insufficient schema/value context and therefore guesses columns and filter literals.
6. The catalog is maintained but not reliably surfaced; refresh currently duplicates the catalog node.
7. No column value profiling exists.
8. ID-grain categorical fan-out is unmanaged, creating attribution/double-counting risk.
9. There is no typed semantic layer for metric definitions, grain, valid dimensions, sources, filters, caveats, or freshness.
10. The LLM is being asked to decide whether data is already available. This is a deterministic coverage problem and belongs in code.
11. There is no persistent analytical workspace capable of local joins, re-cuts, or cross-extract analysis.
12. Related questions have no shared governed population, so their results cannot be mechanically reconciled.
13. The browser/AppleScript transport cannot safely carry million-row ID-grain results. The current `max_rows` slice happens after the payload has already crossed the transport boundary.

---

## 3. Load-Bearing Architecture

### 3.1 Semantic layer

Use the existing **Company Brain**.

Typed semantic metrics/dimensions must provide, where applicable:

- definition/formula
- valid grain
- valid dimensions
- source tables
- mandatory filters/exclusions
- caveats
- freshness

Column profiles provide real values/cardinalities, ranges, null fractions, and fan-out information.

The semantic layer is consulted **before SQL exists**.

### 3.2 BaseView

The central correctness primitive is a governed **ID-grain BaseView**.

Because Athena is read-only, this is not a warehouse `CREATE VIEW`. It is a stored SQL definition in the Company Brain, governed by the existing DRAFT → submit → approve flow, and inlined as a CTE into derived queries.

The BaseView defines the population:

- FROM
- JOIN
- WHERE
- attribution rules

The projection above the base must not change the population.

Store:

- `population_hash` — row-defining SQL
- `projection_hash` — selected columns

Two answers are reconcilable **iff their `population_hash` matches**.

### 3.3 Attribution

The BaseView must resolve multi-valued categoricals at the declared ID grain using a tenant-specific **Hierarchy of Intent**.

Use ranked `ROW_NUMBER()` logic.

Do not independently re-derive attribution per question.

Attribution is part of the BaseView and therefore part of `population_hash`.

### 3.4 Cubes, not raw populations

Do not download the ID-grain population by default.

The warehouse should aggregate the BaseView into the smallest required **cube**:

```text
BaseView
  ↓
GROUP BY required dimensions
  ↓
cube
  ↓
Parquet
  ↓
DuckDB / Python
```

Additive measures can roll up. Non-additive measures such as distinct counts, medians, and percentiles must be recomputed at the appropriate grain.

When ID-grain rows are genuinely required, use **keyset pagination**, never OFFSET.

### 3.5 Persistent workspace

Materialised cubes/extracts live as tenant-isolated Parquet files.

DuckDB provides local:

- filtering
- aggregation
- joins
- re-cuts
- cross-extract analysis

Python reads the same Parquet artifacts for:

- statistics
- decomposition
- significance testing
- anomaly detection
- chart specifications

Both analytical paths therefore operate over identical persisted data.

### 3.6 Data Manager

The LLM states the data requirement.

A deterministic Python **Data Manager** decides:

```text
requirement
    ↓
existing cube sufficient?
    ├── yes → reuse
    ├── partial → widen
    └── absent → retrieve
```

No LLM decision-making belongs inside the Data Manager.

### 3.7 Provenance

Every analytical turn creates an **Analysis artifact** containing, at minimum:

- question
- analytical plan
- data requirement
- BaseView
- population/projection hashes
- datasets used
- SQL
- Python
- result
- assumptions
- provenance

---

## 4. Transport / Storage Constraints

- `MAX_TRANSPORT_ROWS` is a hard property of the browser/AppleScript transport, not a tunable application limit.
- Current default is 50,000; empirically validate the real ceiling before sizing dependent behaviour.
- `RAW_EXTRACT_ROW_LIMIT = 1_000_000` is the total materialised cube/extract ceiling across chunks.
- `MAX_CUBE_CELLS = 200,000`.
- `EXTRACT_CHUNK_ROWS <= MAX_TRANSPORT_ROWS`.
- Never silently truncate. Truncation must become an explicit warning/metadata flag.
- Existing `QueryPolicy` remains the warehouse query guard.
- Do not change the warehouse access path: continue using `BrowserSessionExecutor` through the authenticated Metabase tab.
- DuckDB is local and read-only over Parquet; no remote extensions or network access.

---

## 5. Tenant / Security Constraints

- Tenant isolation is filesystem-level.
- Extracts live under:
  `<tenants_dir>/<tenant_id>/extracts/<conversation_id>/...`
- Validate tenant, conversation, and label IDs against:
  `^[A-Za-z0-9_-]{1,64}$`
- Never rely on a tenant column/filter as the isolation mechanism.
- Python execution remains sandboxed with memory/time limits.

---

## 6. Implementation Tasks

### Phase 1 — Durable workspace

#### 1. ExtractStore

Create `analytics_platform/execution/extract_store.py`.

Provide a tenant-isolated Parquet store with JSON sidecar metadata supporting:

- put
- load
- metadata lookup
- list
- delete conversation
- retention sweep

Metadata must include label, question/description, grain, columns, dtypes, row count, truncation, SQL, and creation time.

Add unit tests for round-trip persistence, tenant isolation, path traversal rejection, missing/corrupt extracts, and deletion.

#### 2. Durable ConversationDataCache

Modify `execution/dataframe_cache.py`.

Keep the existing in-memory LRU as the hot layer, but write through to ExtractStore and fall back to Parquet on memory misses.

Expose grain, truncation, and a small JSON-safe sample in `describe()`.

Existing callers must continue to work when no ExtractStore is configured.

#### 3. Python sandbox → Parquet

Modify `execution/python_sandbox.py`.

Allow the child process to receive Parquet paths and load them itself instead of pickling large DataFrames through the process boundary.

Keep the existing inline-DataFrame API compatible.

---

### Phase 2 — Analytical correctness

#### 4. Transport limits

Align per-request limits and transport behaviour.

Requirements:

- enforce the real single-round-trip ceiling
- retain existing default behaviour
- support keyset paging
- emit explicit truncation warnings
- distinguish transport limits from materialised-extract limits

Before finalising the constant, measure the real Metabase/AppleScript boundary at several row counts.

#### 5. Column profiler

Modify `junior.py`.

Fix the duplicate catalog-node update.

Add per-table profiling producing:

- dtype
- distinct count
- null fraction
- complete low-cardinality values when `distinct_count <= 50`
- top 20 values otherwise
- numeric/date min/max
- row-count estimate
- fan-out by candidate grain key

Profiling must use QueryPolicy and should be cached in the Company Brain.

A saturated sample must never claim that its value list is complete.

#### 6. Semantic layer

Create `analytics_platform/semantic.py`.

Add typed `SemanticMetric` and `SemanticDimension` payloads to existing Company Brain nodes.

Metrics should capture definition, grain, dimensions, source tables, mandatory filters, caveats, and freshness.

Provide semantic resolution and the required API surface.

#### 7. BaseView

Create `analytics_platform/base_view.py`.

Implement:

- governed ID-grain base definitions
- Company Brain storage/review flow
- attribution-aware SQL
- `population_hash`
- `projection_hash`
- CTE inlining
- cube SQL composition
- cube cell-count guard
- reconciliation

This file owns the triangulation guarantee.

---

### Phase 3 — Workspace intelligence

#### 8. SchemaContext

Create `analytics_platform/schema_context.py`.

Combine:

- semantic layer
- database catalog
- column profiles
- relevant BaseViews

into the compact, schema-aware context supplied to the LLM.

If required profiles are missing, trigger the junior profiler and continue the turn.

This is the single place controlling analytical context shown to the LLM.

#### 9. AnalyticalWorkspace

Create `execution/workspace.py`.

Use DuckDB over the Parquet cache.

Support:

- registering extracts as views
- local SQL
- joins
- aggregation
- re-cuts
- cross-extract analysis
- handing Parquet paths to the Python sandbox

Add DuckDB as a pinned dependency.

#### 10. DataManager

Create `data_manager.py`.

Represent analytical requirements explicitly and implement deterministic:

- reuse
- widen
- retrieve

coverage decisions based on workspace manifests and `population_hash`.

A wider cube with the same population supersedes a narrower cube where its dimensions cover the requirement and measures are additive.

---

### Phase 4 — Stakeholder Analyst integration

#### 11. Turn planner

Modify `stakeholder.py` and replace `_choose_compute_path` with `_plan_turn`.

The planner must resolve:

- business intent
- semantic metric/dimension
- required grain
- BaseView
- filters
- required cube
- analytical operation

The planner should state requirements, not make deterministic cache decisions.

#### 12. Cube execution / SQL synthesis

Refactor SQL generation so that:

- semantic/schema context is available before synthesis
- the governed BaseView is reused verbatim
- cube queries are composed over the BaseView
- large results are keyset-paged
- LLM-authored SQL is limited to the paths where SQL genuinely needs to be authored

The LLM must not independently recreate the population logic.

#### 13. Attribution + grain verification

Integrate the Hierarchy of Intent into SchemaContext/BaseView.

Verify at execution time that the BaseView actually satisfies its declared grain.

Prevent multi-valued dimensions from creating duplicate population rows or inconsistent attribution.

#### 14. Analyst pipeline + Analysis artifact

Restructure `answer()` into the intended pipeline:

```text
plan
→ semantic/schema context
→ workspace coverage
→ retrieve/widen/reuse
→ analyse
→ validate
→ interpret
→ record
```

Python analysis should operate over persisted Parquet where possible.

Record the Analysis artifact and database metadata required for reproducibility and replay.

---

### Phase 5 — API surface

#### 15. Replay / CSV / reconciliation

Extend the API with:

- CSV download
- cross-answer `reconcile`
- extract metadata
- full provenance in conversation replay
- analysis artifacts needed by Plan B

Plan A should expose everything the future frontend needs through JSON.

#### 16. Retention

Extend the existing retention mechanism to remove expired Parquet extracts and related metadata according to `extract_retention_days`.

---

## 7. Files

### Create

- `analytics_platform/execution/extract_store.py`
- `analytics_platform/semantic.py`
- `analytics_platform/base_view.py`
- `analytics_platform/schema_context.py`
- `analytics_platform/execution/workspace.py`
- `analytics_platform/data_manager.py`
- corresponding focused tests
- `tests/test_extract_flow.py`

### Modify

- `analytics_platform/junior.py`
- `analytics_platform/execution/dataframe_cache.py`
- `analytics_platform/execution/python_sandbox.py`
- `analytics_platform/stakeholder.py`
- `analytics_platform/config.py`
- `analytics_platform/execution/browser_session.py`
- `analytics_platform/database.py`
- `analytics_platform/api.py`
- `analytics_platform/retention.py`
- `requirements.txt`

### Do not modify

- `frontend/**`

Plan B owns the frontend migration, assistant-ui integration, SSE, tool rendering, storyline selection, and chart rendering.

---

## 8. Execution Rules

Implement incrementally and inspect the current repository before changing code.

For each task:

1. Inspect the relevant implementation and existing tests.
2. Make the smallest compatible change satisfying the task.
3. Add focused tests for the new behaviour.
4. Run the focused tests.
5. Run the relevant broader suite before moving on.
6. Preserve existing behaviour unless this plan explicitly changes it.

Do not invent new architecture when the existing code already provides the required mechanism.

Do not introduce LangGraph.

Do not build a new database/store for semantics when the Company Brain already owns that information.

Do not move profiling logic into the Stakeholder Analyst.

Do not move attribution logic into individual questions.

Do not bypass QueryPolicy for profiling or analytical queries.

Do not silently truncate data.

Commit directly to `main`.

Use subagent-driven development or executing-plans only when it materially helps; do not create unnecessary subagents.

When an implementation detail is not specified here, inspect the repository and choose the smallest implementation consistent with the architecture.

---

## 9. Acceptance Criteria

Plan A is complete when:

1. An initial question can resolve semantics and select a governed ID-grain BaseView.
2. The warehouse produces a bounded analytical cube rather than an answer-only extract.
3. Cubes persist as tenant-isolated Parquet.
4. A follow-up can reuse or widen existing workspace data without unnecessary warehouse queries.
5. DuckDB can locally re-cut/join persisted data.
6. Python can analyse the same persisted data in the sandbox.
7. The LLM receives real semantic, schema, and value context.
8. Multi-valued attribution is resolved consistently inside the BaseView.
9. Related answers expose matching `population_hash` values when they share a population.
10. Non-reconcilable answers explicitly state why.
11. Large results cannot silently cross the transport ceiling.
12. Every analytical turn produces reproducible provenance.
13. CSV, replay, extract metadata, and reconciliation are exposed through the API.
14. Parquet artifacts are subject to retention.
15. Existing tests remain green.

Primary verification:

```bash
.venv/bin/python -m pytest tests/ -q
```

The frontend remains untouched until Plan B.
