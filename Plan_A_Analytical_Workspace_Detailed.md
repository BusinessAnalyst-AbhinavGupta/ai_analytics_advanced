# Plan A — Analytical Workspace: Semantic Layer, Data Manager, and the Raw-Extract Loop

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Stakeholder Analyst from "an LLM that writes SQL" into an AI analyst working inside a persistent analytical workspace: a semantic layer it consults before writing anything, a governed **base view** that fixes the row population every answer is computed from, a deterministic Data Manager that decides whether the data is already in hand, a DuckDB + Parquet workspace it re-cuts locally, and a Python sandbox for statistics and chart specs — with full provenance on every turn.

**Architecture:** Each turn runs a fixed pipeline: *resolve semantics → check the workspace → retrieve only what's missing → analyse → interpret*.

The load-bearing idea is the **base view**. Athena here is read-only, so `CREATE VIEW` is not available; a base view is therefore an **ID-grain SQL definition stored in the Company Brain**, governed by the existing DRAFT → submit → approve flow, and **inlined as a CTE** into every query derived from it. The base defines *which rows exist* — `FROM`, `JOIN`, `WHERE`, and the attribution CTEs that collapse multi-valued categoricals onto the grain. Columns selected above it are a *projection*, not a change of population. So the base is hashed twice: a **`population_hash`** over the row-defining part and a **`projection_hash`** over the column list. Two answers reconcile **iff their `population_hash` matches**; their projections may differ freely, because adding a column cannot change an additive measure over the same rows. That is the triangulation guarantee — question A and question B are provably answered from the same underlying rows even when their filters and aggregations differ.

Data does not come down at ID grain. A turn takes a **cube** — `GROUP BY <dimensions>` over the base, computed *in the warehouse* — and materialises the result as per-tenant Parquet. A base of a million sessions becomes tens of thousands of cube rows, so the base itself never crosses the browser transport, while every cube built on it carries the same `population_hash` and therefore reconciles with every other cube and every prior answer. Additive measures (`SUM`, `COUNT(*)`, `MIN`, `MAX`, and `AVG` stored as SUM+COUNT) roll up freely from a cube; non-additive ones (`COUNT(DISTINCT)`, medians, percentiles) are re-asked at the grain they are needed at, or — when ID-grain rows genuinely are required — pulled in **keyset-paginated chunks**.

The **semantic layer** (typed metric/dimension nodes in the existing Company Brain, plus real column value profiles) is consulted before any SQL exists, and is what tells the planner which base view and which dimensions a question actually needs. The **Data Manager** — plain Python, not an LLM — compares the turn's stated data requirement against what the workspace already holds and returns `reuse` / `widen` / `retrieve`. The **workspace** exposes the materialised cubes both as DuckDB views (for local SQL re-cuts, joins, aggregation) and as frames in the Python sandbox (for statistics, decomposition, and chart specs) — the same Parquet files, so both paths see identical data. Every turn writes an **Analysis artifact** recording question, plan, base view and both hashes, data used, SQL, Python, result, and assumptions.

**Tech Stack:** Python 3 / FastAPI / DuckDB / Parquet + pyarrow 24.0.0 (already a dependency) / pandas / SQLite per tenant. Warehouse access stays as-is: `BrowserSessionExecutor` reaching Athena through the human's authenticated Metabase tab.

**Scope:** This is **Plan A of two**. Plan A is backend-only and ships behind the existing API. **Plan B** — assistant-ui replacing the hand-rolled chat, SSE step events, and the storyline surface — is written after Plan A is approved. Nothing in `frontend/` is touched here; instead every task that produces something a user must see puts it in the **JSON answer payload** and the conversation-replay endpoint, so Plan B has a surface to render.

---

## Context

**Why this plan is shaped the way it is.** The product should feel like *an AI analyst with a persistent analytical workspace*, not a chatbot that emits SQL. Engineering effort therefore goes into semantic understanding, analytical planning, data retrieval and caching, the workspace itself, provenance, and correctness — **not** into rebuilding chat infrastructure. The chat surface is a solved, open-source problem (assistant-ui) and is deliberately deferred to Plan B; this plan builds the analyst that sits behind it.

The system should therefore optimise for: *a business stakeholder starts with an ambiguous business question and collaboratively investigates it until they have a defensible, presentation-ready answer* — **correctness > speed > cleverness**. When the analyst is uncertain about a metric definition, data availability, or a conclusion, it says so rather than fabricating.

The Stakeholder Analyst was specified to work in two stages: the LLM writes a query, the **raw data is downloaded**, and then — for the initial question and every follow-up — the LLM decides whether it needs *new SQL* (data it doesn't have) or *Python over the data it already has* (a different cut of the same rows). In a recent real test run only two SQL queries appeared, no raw data was found anywhere, and there was no trace of Python. The SQL itself was a pre-aggregated answer.

Root cause, confirmed by reading the code:

1. **`analytics_platform/stakeholder.py:617-627`** — the `_synthesize_sql` system prompt says "write a query to answer the user's question." It never mentions grain or raw rows, so the LLM correctly emits `SELECT country, SUM(revenue) … GROUP BY country`. The cached result is an answer, not data.
2. **`stakeholder.py:550-593`** — `_choose_compute_path` is not broken, it is *starved*. It returns `("sql", "")` immediately when nothing is cached, and when something *is* cached it only sees `label / description / columns`. Given a frame that is already the answer to turn 1, a follow-up like "break that down by service line" genuinely cannot be computed from it — so `"sql"` is the right answer, and that is exactly why two SQLs and zero Python appeared.
3. **`analytics_platform/execution/dataframe_cache.py:5`** — "Never persisted to disk." There is no raw artifact to find, and it evaporates on restart, so reopening a conversation can never use Python.
4. Only `exec_res.data.head(3)` (`stakeholder.py:328-334`) ever reaches the synthesis LLM, and `_synthesize_python`'s prompt (`stakeholder.py:704-714`) explicitly forbids returning the raw frame.
5. **The query-writing LLM is given no schema at all.** `_synthesize_sql`'s prompt (`stakeholder.py:608-620`) is assembled from `defn_nodes` (title + summary) and `query_nodes` (title + SQL text) and nothing else. It has no column list, no types, and no idea what values a column actually contains — so it infers column names from whatever example query happened to be retrieved, and invents filter literals. That is the direct cause of generic, non-specific SQL.
6. **The schema is already in the knowledge graph, and nobody reads it.** `junior.get_catalog` / `refresh_catalog` (`junior.py:233-315`) maintain a `"Database Catalog"` DEFINITION node holding `{table, columns, types}` per table. `_retrieve` (`stakeholder.py:112-118`) only does a semantic `brain.search()`, so that node surfaces by luck at best. Worse, `refresh_catalog`'s `if cat_node:` and `else:` branches are **identical** — both call `brain.create(...)` — so every refresh appends a *duplicate* catalog node, and `get_catalog` returns whichever `all()` yields first. The dead comments at `junior.py:305-308` claim no update API exists; `BrainStore.update_field` (`brain/store.py:138`) does.
7. **Nothing anywhere profiles column values.** `refresh_catalog` probes `SELECT * FROM t LIMIT 1` — columns and dtypes only. There is no distinct count, no value list, no date range, no null fraction. So even a schema-aware LLM would still guess that a status column says `COMPLETED` rather than `complete`.

8. **Choosing a grain silently creates an attribution problem, and nothing in the system knows that.** The moment the planner says "one row per `session_id`", every categorical column that is one-to-*many* at that grain — `service_line`, `category`, `product_category` — has to collapse to a single value per session. An LLM given no guidance will either emit a `GROUP BY session_id, service_line` (which quietly breaks the stated grain and double-counts every multi-category session) or grab an arbitrary `MIN()`/first-touch value (which misattributes conversions). In the user's own production experience 5-7% of sessions span multiple product lines, and those are disproportionately the sessions that convert — so the arbitrary choice corrupts exactly the metric that matters most.

9. **There is no semantic layer — only a bag of retrieved text.** `NodeKind` (`domain.py:46-55`) has `METRIC`, `DEFINITION`, `JOIN_RULE`, `BUSINESS_RULE`, but nothing gives a metric a *structure*: no formula, no grain, no valid dimensions, no source table, no exclusion rule, no freshness. `_retrieve` (`stakeholder.py:112-118`) pulls three QUERY nodes and three DEFINITION nodes by similarity and drops their prose into the prompt. That is enough to produce a *technically valid* query and nowhere near enough to produce an *analytically correct* one: nothing stops the LLM computing conversion at the wrong grain, or forgetting that test traffic is excluded.

10. **The decision "do I already have this data?" is delegated to the LLM, and it is not a judgement call.** `_choose_compute_path` asks the model to pick between Python and SQL from a list of labels and column names. Whether a cached extract covers *country = Germany, August 1-31, dimension = device* is a set-containment question with a right answer. It belongs in deterministic code that inspects the workspace manifests — and the LLM's job shrinks to *stating the requirement*, which is what it is good at.

11. **There is no workspace, only a per-conversation dict of DataFrames.** `ConversationDataCache` is an in-process LRU (`dataframe_cache.py`, "Never persisted to disk"). There is no engine that can join two extracts, no way to run a local SQL re-cut, and nothing survives a restart. Every follow-up therefore goes back through Metabase to Athena — slow, expensive, and rate-limited by a human's browser tab.

12. **There is no shared base relation, so two answers to related questions cannot be reconciled.** Every turn writes its own `FROM … JOIN … WHERE` from scratch. Ask "revenue by country" and then "revenue by device" and you get two independently-authored queries whose row populations may differ — a date boundary interpreted differently, a join that drops nulls in one and keeps them in the other, test traffic excluded once and not the other time. The two totals then disagree and *nobody can tell why*, because there is no artifact saying "these two answers were computed over the same rows." In a warehouse you would fix this with `CREATE VIEW`. **This Athena account is read-only, and no additional access is being requested**, so the view has to be a client-side construct: a stored, hashed, governed SQL definition that is inlined into every derived query. Without it, triangulation is manual archaeology; with it, it is a hash comparison.

    The second half of the problem is that the base must be at **ID grain** (e.g. one row per `session_id`), not at whatever dimensional grain the first question happened to need. If the base is `country × device × date`, then the next question — which asks for `service_line` — needs a *different* base, and the two share nothing. At ID grain, both questions are projections of the same population, and a new dimension is a column added above an unchanged base rather than a rewrite of it.

13. **The transport cannot carry a million rows, and the code that appears to cap it does not.** `BrowserSessionExecutor` runs SQL by driving Chrome via AppleScript into Metabase's `/api/dataset`, `JSON.stringify`s the whole result into `window.__mb.payload`, and returns it as **a single `osascript` string** which Python then `json.loads`. The truncation at `browser_session.py:~303` is `if len(rows) > self.config.max_rows: rows = rows[: self.config.max_rows]` — a **post-hoc Python slice applied after the entire payload has already crossed that boundary**. Raising `max_rows` does not make the transport carry more; it only stops discarding what already arrived (and arrived through a single string). The only thing that actually bounds the warehouse is the `LIMIT` `QueryPolicy` injects. So "extract at ID grain, then re-cut locally" is not implementable as stated for large populations — the ID-grain rows must either stay in the warehouse (aggregated there into a cube) or come down in bounded chunks.

The intended outcome: the analyst resolves the question against a real semantic layer first; it picks or proposes a **governed, ID-grain base view** that fixes the row population, with an explicit, business-ranked rule for collapsing multi-valued categoricals onto that grain baked *into the base* so every question inherits the same one; the warehouse aggregates that base into a **cube** at the dimensions the question needs, and only the cube is materialised per tenant as Parquet, queryable locally via DuckDB and analysable in the Python sandbox; a deterministic Data Manager decides whether an existing cube already covers the requirement, or whether it should be widened; and every turn leaves an inspectable artifact carrying both hashes, so any two answers can be checked for — and mechanically reconciled against — a common population.

## Decisions taken (confirmed with the user)

| Decision | Choice |
|---|---|
| How two answers are made reconcilable | A **base view**: an ID-grain SQL definition stored in the Company Brain, governed by the existing DRAFT → approve flow, **inlined as a CTE** into every derived query. Athena is read-only, so a real `CREATE VIEW` is not available and none is requested |
| Base grain | **ID grain** (`session_id`, `order_id`, …), never dimensional grain — so a question needing a fifth dimension is a column added above an unchanged base, not a new base |
| How the base is identified | **Two hashes.** `population_hash` over the row-defining part (`FROM`/`JOIN`/`WHERE`/attribution); `projection_hash` over the column list. Two answers reconcile **iff `population_hash` matches**; projections may differ freely |
| What actually crosses the transport | **The cube, not the base.** `GROUP BY <dimensions>` runs in the warehouse; only the aggregated result is materialised. The base stays a definition |
| When ID-grain rows are genuinely needed | **Keyset pagination** (`WHERE id > '<last_seen>' ORDER BY id LIMIT <chunk>`), never `OFFSET`. Reserved for non-additive measures; not the default path |
| Where the materialised cube lives | **Parquet on disk, per tenant**, with the in-memory cache as a hot layer |
| Row ceiling | **`RAW_EXTRACT_ROW_LIMIT = 1_000_000` on a materialised cube** (summed across chunks). A *single round trip* stays under `MAX_TRANSPORT_ROWS` (50,000) — see Global Constraints for why that is a hard property of the transport, not a tunable |
| UI surface | CSV download + a full JSON provenance payload in Plan A; the rendered panel is **Plan B** |
| Missing column profile at query time | **Profile it inline, then proceed.** The junior owns the profiling code; the stakeholder triggers it for just the tables it needs and continues the same turn |
| Value-profile depth | **Full value list when distinct count ≤ 50**; above that, top 20 by frequency + min/max. Distinct count, null fraction, and (for dates/numerics) true min/max on every column |
| Collapsing multi-valued categoricals onto the grain | **Hierarchy of Intent**, ranked by business value, resolved with `ROW_NUMBER() OVER (PARTITION BY <grain> ORDER BY …)` — never first-touch, last-touch, or an arbitrary aggregate. The ranking lives in the Company Brain as a tenant-specific, senior-approvable node |
| Where attribution is applied | **Inside the base view**, decided once and inherited by every cube built on it. Per-question re-derivation would let two questions silently apply different rankings to the same rows — the exact failure the base exists to prevent — and it is part of the `population_hash` |
| Cube reuse rule | A cube is reusable for any requirement whose dimensions are a **subset** of its own, provided every measure is additive; a wider cube **supersedes** a narrower one with the same `population_hash`. This is the same containment rule the Data Manager already applies to grain, moved from ID grain to cube dimensions |
| Chat UI | **Do not keep hand-building it.** assistant-ui replaces `StakeholderChat.tsx` in **Plan B**. Plan A touches no frontend file and instead widens the JSON contract |
| Agent orchestration | **Keep the current explicit Python control flow.** No LangGraph. §4's requirement is that retrieval be *deterministic*, which an agent graph would obscure, and porting `stakeholder.py` would mean an async rewrite of a sync, fully-tested service |
| Streaming | **SSE step events** (`planning → checking workspace → extracting → analysing → writing`). The generator refactor of `answer()` and the SSE endpoint are **Plan B**; Plan A structures `answer()` into named steps so that refactor is mechanical |
| Local analytical engine | **DuckDB over the Parquet cache**, for filtering, joins, aggregation, and cross-extract work. **Python is not replaced** — statistics, decomposition, significance tests, anomaly detection, and every chart spec stay in the sandbox. Both read the same Parquet files |
| Semantic layer home | **The existing Company Brain**, with a typed `SemanticMetric` / `SemanticDimension` payload on `METRIC` nodes — not a new store and not a YAML file outside review |

## Global Constraints

- **Tenant isolation is filesystem-level.** Every tenant is a different company with its own SQLite file. Extract Parquet files go under `<tenants_dir>/<tenant_id>/extracts/…` where `tenants_dir` is `Settings.tenants_dir` (`analytics_platform/config.py:77-78`). A `tenant_id` column or filter is **not** isolation. Never build a path from unsanitized `tenant_id` / `conversation_id` / `label` — validate each against `^[A-Za-z0-9_-]{1,64}$` before it touches a path.
- **Commit directly to `main`.** No feature branches, no worktrees.
- **No single query result may exceed `MAX_TRANSPORT_ROWS` (default 50,000), and that is a property of the transport, not a tunable.** Results reach Python as one `osascript` return string; `browser_session.py`'s `max_rows` truncation is a **post-hoc `rows[:max_rows]` slice applied after the entire payload has already crossed that boundary**, so raising it does not make the transport carry more. The only ceiling that binds the warehouse is the `LIMIT` `QueryPolicy` injects. **`MAX_CUBE_CELLS` (200,000) and `MAX_TRANSPORT_ROWS` (50,000) are different numbers on purpose:** the first is whether a cube is worth composing at all, the second is what one round trip carries. A cube between them is legal and is fetched in keyset pages over its dimension tuple (Task 12); ID-grain rows, when genuinely needed, come down the same way in chunks of `EXTRACT_CHUNK_ROWS` (default 50,000).
- `RAW_EXTRACT_ROW_LIMIT = 1_000_000` is the ceiling on a **materialised cube or extract** — the sum across chunks — never on a single round trip. The existing `default_row_limit = 50000` (`config.py:12`) stays as-is.
- **A number is only reconcilable against another number when both were computed over the same `population_hash`.** Any answer that omits a base view — the `aggregate` escape path — must say so in its caveats, because it cannot be reconciled with anything.
- This repo has **no httpx and no TestClient**. API tests use `call(app, method, path_template, tenant, *body)` from `tests/test_api.py`, which invokes route closures directly and therefore **bypasses middleware**.
- `analytics_platform/api.py` uses **relative imports** (`from .storyline import …`).
- **No frontend changes in Plan A.** `frontend/` is untouched; `StakeholderChat.tsx` is being replaced wholesale in Plan B, so any work on it now is thrown away. Everything a user must eventually see goes into the answer payload and the replay endpoint instead.
- **Warehouse access does not change.** Queries run through `BrowserSessionExecutor` (`execution/browser_session.py`) — AppleScript into the human's authenticated Metabase tab, which fronts Athena. There is no direct Athena client and this plan does not add one. That is exactly why local reuse matters: every avoided warehouse query is an avoided round trip through someone's browser.
- **`duckdb` is a new dependency** — add it to `requirements.txt` pinned, and install into `.venv` in the same task that introduces it. DuckDB is an **embedded, read-only-over-Parquet** engine here: no `INSTALL`/`LOAD` of extensions, no `httpfs`, no attaching remote databases. It never reaches the network.
- `frontend/src/store/useStore.ts` is `create<AppState>((set) => ({…}))` — no `get` destructured; actions read state via `useStore.getState()` and try/catch with `console.error(e)`. (Recorded for Plan B; do not edit it here.)
- **Profiling is the junior's job.** All schema/value-profiling code lives in `analytics_platform/junior.py` and writes to the Company Brain. The stakeholder may *trigger* it and *read* it, but must never grow its own copy of the profiling logic.
- **Profiling SQL must go through `QueryPolicy` like any other query.** No raw `executor.execute()` of an unvalidated string — `refresh_catalog`'s existing `SELECT * FROM {t} LIMIT 1` already bypasses policy; do not add more of that pattern.
- **`PROFILE_CARDINALITY_CAP = 50`, `PROFILE_TOP_VALUES = 20`.** Both configurable; both must appear in the rendered prompt so the LLM knows when a value list is complete versus truncated.
- Full suite must stay green: `.venv/bin/python -m pytest tests/ -q` (currently 478 passed, 1 skipped) and `cd frontend && npx tsc --noEmit` (0 errors).

---

## File Structure

**Create**
- `analytics_platform/execution/extract_store.py` — Parquet-backed durable store for conversation extracts + JSON sidecar manifests. Sole owner of extract paths and path validation.
- `analytics_platform/semantic.py` — the typed semantic layer: `SemanticMetric` / `SemanticDimension`, read/write against the Company Brain, and `resolve(question_terms)`.
- `analytics_platform/base_view.py` — the base-view registry. Owns `BaseView`, the split `population_hash` / `projection_hash`, CTE inlining, cube SQL composition, the cell-count guard, and `reconcile()`. Reads and writes the Company Brain under the existing review flow. **This is the file the triangulation guarantee lives in.**
- `analytics_platform/schema_context.py` — reads the semantic layer plus the Brain's catalog and column profiles, triggers the junior's profiler when they are missing, and renders the compact text block that goes into every prompt. The single place that decides what an LLM sees.
- `analytics_platform/execution/workspace.py` — `AnalyticalWorkspace`: one DuckDB connection per (tenant, conversation), every extract registered as a view over its Parquet file; runs local SQL, hands Parquet paths to the sandbox.
- `analytics_platform/data_manager.py` — deterministic `DataRequirement` → `reuse` / `widen` / `retrieve` coverage decision over the workspace manifests. **No LLM in this file.**
- `tests/test_extract_store.py`, `tests/test_column_profiler.py`, `tests/test_semantic.py`, `tests/test_base_view.py`, `tests/test_schema_context.py`, `tests/test_workspace.py`, `tests/test_data_manager.py`, `tests/test_attribution.py`
- `tests/test_extract_flow.py` — end-to-end: extract turn → follow-up served entirely from the workspace.

**Modify**
- `analytics_platform/junior.py:233-315` — fix the duplicate-catalog-node bug, and add `profile_tables()` producing per-column distinct counts, value lists, ranges, and null fractions into the brain.
- `analytics_platform/execution/dataframe_cache.py` — accept an optional `ExtractStore`; write through on `put`, read through on `get`/`list_available`; carry grain + truncation + sample in `describe()`.
- `analytics_platform/execution/python_sandbox.py` — accept `dataframe_paths` so the child loads Parquet itself; configurable memory/timeout.
- `analytics_platform/stakeholder.py` — the bulk: `_plan_turn` (replaces `_choose_compute_path`), grain-first SQL synthesis, `_synthesize_and_execute_workspace_sql`, the restructured `answer()` pipeline, and extract metadata + the analysis artifact on `_record`.
- `analytics_platform/config.py` — `raw_extract_row_limit`, `extract_retention_days`, sandbox memory/timeout settings.
- `analytics_platform/execution/browser_session.py:159,165,305-306` — `max_rows` must not silently cap an extract at 50,000.
- `analytics_platform/database.py` (~line 227) — additive `extract_meta` column migration.
- `analytics_platform/api.py` — CSV download route; semantic-layer and attribution-proposal routes; extract metadata and the analysis artifact in conversation replay.
- `requirements.txt` — add `duckdb` (pinned).

**Explicitly out of scope (Plan B)**
- `frontend/**` — assistant-ui swap, tool-state rendering, storyline selection UI.
- SSE endpoint and the `answer()` → generator refactor.
- Chart *rendering*. Plan A produces a chart **spec** in the payload; Plan B renders it.

---

## Task Map

| # | Task | Why it exists |
|---|---|---|
| 1 | `ExtractStore` — durable Parquet layer | there is no materialised artifact anywhere today |
| 2 | `ConversationDataCache` becomes disk-durable | reopened conversations must stay analysable |
| 3 | Sandbox loads frames from Parquet | large frames cannot be pickled through a pipe |
| 4 | Per-request row limits + honest transport ceiling | the 50k caps must agree, and one of them is not a cap |
| 5 | Column profiler in the junior (+ duplicate-catalog fix) | real values and real cardinalities, so filters and cube sizes stop being guesses |
| 6 | **Semantic layer** | analytically correct, not merely valid, queries |
| 7 | **`BaseView` — governed ID-grain population + cube composition** | the read-only-Athena substitute for `CREATE VIEW`; the thing that makes two answers reconcilable |
| 8 | `SchemaContext` — semantics + schema + base views in front of the LLM | none of the above matters until it reaches a prompt |
| 9 | **DuckDB workspace** | local SQL re-cuts and joins over materialised cubes |
| 10 | **Data Manager** | reuse/widen/retrieve decided in code, not by an LLM |
| 11 | `_plan_turn` — pick the base view, state the cube | replaces the starved `_choose_compute_path` |
| 12 | Schema-aware SQL synthesis over an inlined base | the base is inlined verbatim, never re-authored |
| 13 | Attribution hierarchies (inside the base) + grain-integrity check | multi-valued categoricals must not double-count, and must collapse the same way for every question |
| 14 | Restructure `answer()` + the Analysis artifact | the pipeline, and provenance for every turn |
| 15 | CSV download + `reconcile` endpoint + widened replay payload | the surface Plan B renders, plus the check that proves the guarantee |
| 16 | Retention sweep | materialised Parquet accumulates fast |

---

### Task 1: `ExtractStore` — durable Parquet layer

**Files:**
- Create: `analytics_platform/execution/extract_store.py`
- Test: `tests/test_extract_store.py`

**Interfaces:**
- Consumes: `Settings.tenants_dir` (`analytics_platform/config.py:77-78`).
- Produces:
  ```python
  SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

  @dataclass
  class ExtractMeta:
      label: str
      description: str      # the question that produced it, truncated to 200 chars
      grain: List[str]      # e.g. ["session_id"] or ["user_id", "order_date"]
      columns: List[str]
      dtypes: Dict[str, str]
      row_count: int
      truncated: bool       # row_count hit the ceiling
      sql: str
      created_at: str       # now_iso()

  class ExtractStore:
      def __init__(self, tenants_dir: str) -> None: ...
      def dir_for(self, tenant_id: str, conversation_id: str) -> str: ...
      def put(self, tenant_id, conversation_id, meta: ExtractMeta, df: pd.DataFrame) -> str: ...  # returns parquet path
      def path(self, tenant_id, conversation_id, label: str) -> Optional[str]: ...
      def load(self, tenant_id, conversation_id, label: str) -> Optional[pd.DataFrame]: ...
      def meta(self, tenant_id, conversation_id, label: str) -> Optional[ExtractMeta]: ...
      def list_metas(self, tenant_id, conversation_id) -> List[ExtractMeta]: ...
      def delete_conversation(self, tenant_id, conversation_id) -> None: ...
      def sweep(self, retention_days: int) -> int: ...  # returns dirs removed
  ```
  Layout: `<tenants_dir>/<tenant_id>/extracts/<conversation_id>/<label>.parquet` and `<label>.json`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_extract_store.py
import pandas as pd, pytest
from analytics_platform.execution.extract_store import ExtractStore, ExtractMeta

def _meta(label="df_1", rows=3):
    return ExtractMeta(label=label, description="q", grain=["session_id"],
                       columns=["session_id", "revenue"],
                       dtypes={"session_id": "object", "revenue": "int64"},
                       row_count=rows, truncated=False, sql="SELECT 1", created_at="2026-08-15T00:00:00Z")

def test_put_then_load_roundtrips(tmp_path):
    store = ExtractStore(str(tmp_path))
    df = pd.DataFrame({"session_id": ["a", "b", "c"], "revenue": [1, 2, 3]})
    store.put("acme", "conv_1", _meta(), df)
    back = store.load("acme", "conv_1", "df_1")
    pd.testing.assert_frame_equal(back, df)
    assert store.meta("acme", "conv_1", "df_1").grain == ["session_id"]

def test_tenants_get_separate_directories(tmp_path):
    store = ExtractStore(str(tmp_path))
    df = pd.DataFrame({"session_id": ["a"], "revenue": [1]})
    store.put("acme", "conv_1", _meta(), df)
    store.put("globex", "conv_1", _meta(), df)
    assert store.load("globex", "conv_1", "df_1") is not None
    assert "acme" not in store.path("globex", "conv_1", "df_1")

@pytest.mark.parametrize("bad", ["../escape", "a/b", "", "x" * 65, "a b"])
def test_path_traversal_is_rejected(tmp_path, bad):
    store = ExtractStore(str(tmp_path))
    with pytest.raises(ValueError):
        store.dir_for(bad, "conv_1")
    with pytest.raises(ValueError):
        store.dir_for("acme", bad)

def test_missing_extract_returns_none(tmp_path):
    store = ExtractStore(str(tmp_path))
    assert store.load("acme", "conv_1", "df_9") is None
    assert store.meta("acme", "conv_1", "df_9") is None

def test_delete_conversation_removes_everything(tmp_path):
    store = ExtractStore(str(tmp_path))
    store.put("acme", "conv_1", _meta(), pd.DataFrame({"session_id": ["a"], "revenue": [1]}))
    store.delete_conversation("acme", "conv_1")
    assert store.list_metas("acme", "conv_1") == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_extract_store.py -q`
Expected: FAIL — `ModuleNotFoundError: analytics_platform.execution.extract_store`

- [ ] **Step 3: Implement `ExtractStore`**

Key points for the implementer:
- `dir_for` raises `ValueError` if either id fails `SAFE_ID`, then `os.path.join(tenants_dir, tenant_id, "extracts", conversation_id)`. Validate `label` the same way in `path`.
- `put`: `os.makedirs(..., exist_ok=True)`, `df.to_parquet(path, index=False)` (pyarrow engine), then write the sidecar with `json.dump(asdict(meta), …)`. Write the parquet **before** the json so a half-written pair never reports as complete; `meta()` returns `None` when the parquet is absent.
- `load` uses `pd.read_parquet`. Wrap in try/except and log + return `None` on a corrupt file — a bad extract must degrade to "re-run SQL", never crash a chat turn.
- `sweep(retention_days)` removes conversation directories whose newest sidecar `created_at` is older than the cutoff. Do not wire it into a scheduler in this task.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_extract_store.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/execution/extract_store.py tests/test_extract_store.py && git commit -m "feat(execution): Parquet-backed per-tenant extract store"
```

---

### Task 2: Make `ConversationDataCache` disk-durable

**Files:**
- Modify: `analytics_platform/execution/dataframe_cache.py`
- Test: `tests/test_dataframe_cache.py` (extend)

**Interfaces:**
- Consumes: `ExtractStore` from Task 1.
- Produces: `ConversationDataCache(max_conversations=50, max_frames_per_conversation=5, store: Optional[ExtractStore] = None)`; `put(..., meta: Optional[ExtractMeta] = None)`; `describe()` gains `grain`, `truncated`, and `sample` (first 3 rows, JSON-safe).

**Why:** call sites in `stakeholder.py` keep the same shape, but a cache miss now falls back to Parquet instead of forcing new SQL — which is what makes a reopened conversation still Python-capable.

- [ ] **Step 1: Write the failing tests**

```python
def test_get_falls_back_to_disk_after_memory_eviction(tmp_path):
    store = ExtractStore(str(tmp_path))
    cache = ConversationDataCache(max_frames_per_conversation=1, store=store)
    df1 = pd.DataFrame({"session_id": ["a"], "revenue": [1]})
    df2 = pd.DataFrame({"session_id": ["b"], "revenue": [2]})
    cache.put("acme", "c1", "df_1", "q1", df1, meta=_meta("df_1"))
    cache.put("acme", "c1", "df_2", "q2", df2, meta=_meta("df_2"))   # evicts df_1 from memory
    pd.testing.assert_frame_equal(cache.get("acme", "c1", "df_1"), df1)   # served from Parquet

def test_list_available_unions_memory_and_disk(tmp_path):
    store = ExtractStore(str(tmp_path))
    cache = ConversationDataCache(max_frames_per_conversation=1, store=store)
    cache.put("acme", "c1", "df_1", "q1", pd.DataFrame({"session_id": ["a"]}), meta=_meta("df_1"))
    cache.put("acme", "c1", "df_2", "q2", pd.DataFrame({"session_id": ["b"]}), meta=_meta("df_2"))
    labels = {f["label"] for f in cache.list_available("acme", "c1")}
    assert labels == {"df_1", "df_2"}

def test_describe_exposes_grain_and_sample(tmp_path):
    cache = ConversationDataCache(store=ExtractStore(str(tmp_path)))
    cache.put("acme", "c1", "df_1", "q", pd.DataFrame({"session_id": ["a", "b", "c", "d"]}),
              meta=_meta("df_1"))
    d = cache.list_available("acme", "c1")[0]
    assert d["grain"] == ["session_id"]
    assert len(d["sample"]) == 3

def test_cache_without_store_behaves_exactly_as_before(tmp_path):
    cache = ConversationDataCache()          # no store
    cache.put("acme", "c1", "df_1", "q", pd.DataFrame({"a": [1]}))
    assert cache.get("acme", "c1", "df_1") is not None
    assert cache.get("acme", "c1", "df_9") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_dataframe_cache.py -q`
Expected: FAIL on the new tests (`TypeError: unexpected keyword argument 'store'`); the pre-existing tests still pass.

- [ ] **Step 3: Implement**

- `CachedFrame` gains `meta: Optional[ExtractMeta]`. `describe()` adds `grain` (`meta.grain` or `[]`), `truncated` (`meta.truncated` or `False`), and `sample` = `self.df.head(3).to_dict(orient="records")` coerced JSON-safe (`str()` any value that is not `str/int/float/bool/None`).
- `put`: keep the existing in-memory LRU exactly as it is (including the monotonic `_label_counters` logic — do not touch it), then, when `store` and `meta` are both present, `store.put(...)`. A store write failure must be logged and swallowed: an unwritable disk degrades to today's in-memory behaviour, it does not fail the chat turn.
- `get`: on memory miss with a store present, `store.load(...)`; if it returns a frame, re-insert it into memory (with its sidecar meta) and return it.
- `list_available`: build a dict keyed by label from `store.list_metas(...)` first, then overlay live in-memory `describe()` entries so a hot frame's real row count wins. Return in label order.
- **Update the module docstring.** Line 5 currently says "Never persisted to disk" and is now false. Replace it with the new contract: row data is persisted per tenant under `tenants_dir`; only `describe()` output (schema, grain, truncation flag, and a 3-row sample) is intended for an LLM prompt.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_dataframe_cache.py -q`
Expected: PASS (all, old and new)

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/execution/dataframe_cache.py tests/test_dataframe_cache.py && git commit -m "feat(execution): back ConversationDataCache with durable extract store"
```

---

### Task 3: Sandbox loads large frames from Parquet

**Files:**
- Modify: `analytics_platform/execution/python_sandbox.py`
- Test: `tests/test_python_sandbox.py` (extend)

**Why this is required, not optional:** `run_python_sandboxed` uses `mp.get_context("spawn")` and passes the DataFrame as a process argument, so **every row is pickled through a pipe**, and the child sets `RLIMIT_AS` to `DEFAULT_MEMORY_MB = 512` (`python_sandbox.py:29,55-56`). At the new 1,000,000-row ceiling that is both very slow and an immediate MemoryError. The child must open the Parquet file itself.

**Interfaces:**
- Produces:
  ```python
  DEFAULT_MEMORY_MB = 512          # unchanged default
  EXTRACT_MEMORY_MB = 4096         # used by the extract path

  def run_python_sandboxed(code: str,
                           dataframes: Optional[Dict[str, pd.DataFrame]] = None,
                           timeout_s: float = DEFAULT_TIMEOUT_S,
                           memory_mb: int = DEFAULT_MEMORY_MB,
                           dataframe_paths: Optional[Dict[str, str]] = None) -> PythonExecResult: ...
  ```
  `dataframes` keeps its current positional slot and semantics so every existing caller and test is untouched. Labels in `dataframe_paths` are read with `pd.read_parquet` **inside `_worker`**, before `exec`, and merged into `scope` — user code still just sees `df_1`.

- [ ] **Step 1: Write the failing tests**

```python
def test_dataframe_paths_are_loaded_in_the_child(tmp_path):
    p = tmp_path / "df_1.parquet"
    pd.DataFrame({"revenue": [1, 2, 3, 4]}).to_parquet(p, index=False)
    res = run_python_sandboxed("result = int(df_1['revenue'].sum())",
                               dataframe_paths={"df_1": str(p)})
    assert res.ok and res.result_summary == 10

def test_unreadable_parquet_reports_an_error_not_a_crash(tmp_path):
    bad = tmp_path / "df_1.parquet"
    bad.write_text("not parquet")
    res = run_python_sandboxed("result = 1", dataframe_paths={"df_1": str(bad)})
    assert not res.ok and "df_1" in res.error

def test_paths_and_inline_frames_can_be_mixed(tmp_path):
    p = tmp_path / "df_1.parquet"
    pd.DataFrame({"revenue": [5]}).to_parquet(p, index=False)
    res = run_python_sandboxed("result = int(df_1['revenue'].sum() + df_2['revenue'].sum())",
                               dataframes={"df_2": pd.DataFrame({"revenue": [7]})},
                               dataframe_paths={"df_1": str(p)})
    assert res.ok and res.result_summary == 12
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_python_sandbox.py -q`
Expected: FAIL — `TypeError: unexpected keyword argument 'dataframe_paths'`

- [ ] **Step 3: Implement**

- Thread `dataframe_paths` into `_worker`'s args. Load them **after** the `setrlimit` calls so a runaway read is still bounded, and wrap each in try/except: on failure send `("error", f"could not load DataFrame {label!r} from disk: {exc}", "")` and return.
- `scope = {"pd": pd, **loaded_from_paths, **(dataframes or {})}`.
- `dataframes` defaults to `None` → treat as `{}`.
- Add `EXTRACT_MEMORY_MB = 4096` and `EXTRACT_TIMEOUT_S = 30.0` as module constants. Do not change `DEFAULT_MEMORY_MB` or `DEFAULT_TIMEOUT_S` — other callers depend on them.
- The `MAX_RESULT_ROWS = 20` / `MAX_RESULT_CHARS = 4000` result cap stays exactly as-is: the whole point is that only a summary crosses back.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_python_sandbox.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/execution/python_sandbox.py tests/test_python_sandbox.py && git commit -m "feat(execution): let the Python sandbox load frames from Parquet"
```

---

### Task 4: Per-request row limits, and an honest transport ceiling

**Files:**
- Modify: `analytics_platform/config.py:12` area (add settings, do not change `default_row_limit`)
- Modify: `analytics_platform/execution/browser_session.py:159,165,305-306`
- Test: `tests/test_execution_policy.py` (or the existing policy test module — find it with `grep -rln "QueryPolicy" tests/`)

**Interfaces:**
- Produces, on the policy settings dataclass in `config.py`:
  ```python
  max_transport_rows: int = 50_000     # what ONE round trip may return -- see below
  extract_chunk_rows: int = 50_000     # keyset page size; must be <= max_transport_rows
  raw_extract_row_limit: int = 1_000_000   # ceiling on a MATERIALISED cube/extract
  extract_retention_days: int = 30
  ```
  `QueryPolicy.validate(sql, allowed_tables=…, dialect=…, row_limit=…)` already accepts `row_limit` (`execution/policy.py:112`) — no signature change, callers just pass it.

**The trap:** a request is capped in **three** independent places and all three must agree, or a large request silently returns 50,000 and the LLM computes a confidently wrong total:
1. `execution/policy.py:112-121` injects `LIMIT {default_row_limit}` into any plain `SELECT` that lacks one.
2. `ExecutionContext.row_limit` defaults to `50000` (`execution/base.py:29`) and `execution/sampler.py:69-70` does `df.head(ctx.row_limit)`.
3. `browser_session.py` has its own `max_rows: int = 50000` (lines 159, 165, 305-306).

**The bigger trap, and the reason this task changed shape.** Only the first of those three is a real cap. `execute()` drives Chrome via AppleScript, `JSON.stringify`s Metabase's entire response into `window.__mb.payload`, and returns it as **one `osascript` string** which Python `json.loads`. The truncation at lines 305-306 is:

```python
rows = res.get("rows", [])
if len(rows) > self.config.max_rows:
    rows = rows[: self.config.max_rows]
```

— a **post-hoc Python slice, applied after the entire payload has already crossed the AppleScript boundary**. Raising `max_rows` does not make that boundary carry more; it only stops discarding what already arrived. **`max_rows` is a memory guard, not a transport limit, and this task must stop it being mistaken for one.** The only thing that bounds the warehouse is the `LIMIT` `QueryPolicy` injects, which is why Task 7 sizes cubes to fit and Task 12 pages ID-grain rows rather than asking for a million at once.

- [ ] **Step 1: Write the failing test**

```python
def test_a_per_request_row_limit_is_injected():
    policy = QueryPolicy(PolicySettings())
    d = policy.validate("SELECT session_id, revenue FROM orders WHERE dt >= '2026-01-01'",
                        row_limit=50_000, dialect="athena")
    assert d.allowed and "LIMIT 50000" in d.approved_sql

def test_default_path_still_limits_to_50000():
    policy = QueryPolicy(PolicySettings())
    d = policy.validate("SELECT country, SUM(revenue) FROM orders WHERE dt >= '2026-01-01' GROUP BY country",
                        dialect="athena")
    assert "LIMIT 50000" in d.approved_sql

def test_a_request_above_the_transport_ceiling_is_refused():
    """No caller may ask a single round trip for more than the transport carries.
    Above this, use a cube (Task 7) or keyset chunks (Task 12)."""
    policy = QueryPolicy(PolicySettings())
    d = policy.validate("SELECT session_id FROM orders", row_limit=1_000_000, dialect="athena")
    assert not d.allowed and "transport" in d.reason.lower()

def test_truncation_sets_a_warning_not_just_a_shorter_frame(fake_chrome):
    """Silent truncation is how a 50,000-row slice becomes a confidently wrong total."""
    fake_chrome.returns_rows(60_000)
    res = BrowserSessionExecutor(BrowserExecutorConfig()).execute(
        "SELECT 1", ExecutionContext(tenant_id="acme", question="q", row_limit=50_000))
    assert res.row_count == 50_000
    assert any("truncated" in w for w in res.warnings)
```

- [ ] **Step 2: Run to verify it fails / passes as expected**

Run: `.venv/bin/python -m pytest tests/ -q -k "policy or truncat"`
Expected: the second test passes today; the first likely does too if `row_limit` is honoured — **keep both as regression guards.** The third and fourth fail. The real work of this task is those two plus item 3 below.

- [ ] **Step 3: Measure what the transport actually carries — do this before implementing**

The `max_transport_rows` default of 50,000 is inherited, not measured. Nothing downstream is safe to size until someone has watched this boundary fail. Against the real Metabase tab, run the same `SELECT` at 25k / 50k / 100k / 200k rows and record, for each: wall-clock, whether `osascript` returned at all, and whether `json.loads` succeeded. Note the shape of the failure — AppleScript's return-value limit truncates a string rather than raising, so a *successful parse of a truncated payload* is the dangerous outcome to look for, not an exception.

Set `max_transport_rows` to the largest size that succeeded cleanly, with a margin, and **write the measured numbers into a comment beside the constant** so the next person does not have to rediscover them. If the honest answer turns out to be well under 50,000, say so and lower it — every other task in this plan is sized off this number, and a wrong value here produces silent data loss everywhere else.

- [ ] **Step 4: Implement**

- Add `max_transport_rows`, `extract_chunk_rows`, `raw_extract_row_limit`, and `extract_retention_days` to the policy settings dataclass in `config.py`, reading each from env in the `from_env`-style constructor at `config.py:96` following the existing pattern for `default_row_limit`.
- `QueryPolicy.validate` rejects any `row_limit > max_transport_rows` with a reason naming the transport. This is the guard that makes the ceiling real rather than documentary.
- `browser_session.py`: make `max_rows` default to `None`, meaning "take it from `ctx.row_limit`". Where it currently truncates (lines 305-306), truncate to `ctx.row_limit` instead of a hardcoded 50,000, and when truncation happens **append a warning to `QueryResult.warnings`** (the field already exists, `execution/base.py:41`) reading `"result truncated at N rows"`. Callers depend on that warning to set `truncated`. **Add a comment at that line** stating plainly that this slice runs after the payload has already crossed the AppleScript boundary and is therefore a memory guard, not a transport limit — the next reader will otherwise make the same mistake this plan originally made.
- Do **not** change `ExecutionContext.row_limit`'s default of 50000 — callers pass `row_limit=` explicitly.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (478+, 1 skipped)

- [ ] **Step 6: Commit**

```bash
git add analytics_platform/config.py analytics_platform/execution/policy.py analytics_platform/execution/browser_session.py tests/ && git commit -m "feat(execution): per-request row limits with a measured, enforced transport ceiling"
```

---

### Task 5: Column profiler in the junior (+ fix the duplicate catalog node)

**Files:**
- Modify: `analytics_platform/junior.py:233-315`
- Test: Create `tests/test_column_profiler.py`; extend `tests/test_junior.py`

**Two things, in this order — the bug first, because the profiler writes to the same node.**

**5a. The duplicate-catalog bug.** `refresh_catalog`'s `if cat_node:` and `else:` branches are byte-identical: both call `self.brain(tenant_id).create(...)`. Every refresh appends another `"Database Catalog"` node, and `get_catalog` does `next((n for n in nodes if n.title == "Database Catalog"), None)` — first match from `all()`, which may be a stale duplicate. Replace the `if` branch with `brain.update_field(cat_node.id, "payload", dump_json(payload))` (`brain/store.py:138` — it exists, contrary to the dead comments at `junior.py:305-308`, which must be deleted). `update_field` re-syncs the search index, which is what the current code was really reaching for.

**Interfaces:**
- Produces, in `analytics_platform/domain.py`:
  ```python
  PROFILE_CARDINALITY_CAP = 50     # ≤ this many distinct values -> store them all
  PROFILE_TOP_VALUES = 20          # above the cap -> store this many, by frequency

  @dataclass
  class ColumnProfile:
      column: str
      dtype: str
      distinct_count: int
      null_fraction: float
      values: List[str]            # complete when values_complete, else top-N by frequency
      values_complete: bool        # distinct_count <= PROFILE_CARDINALITY_CAP
      min_value: str = ""          # populated for numeric / date / datetime columns
      max_value: str = ""
      profiled_at: str = ""
      # Fan-out: for each candidate grain key in this table, the share of keys that
      # carry more than one distinct value of THIS column. 0.0 means the column is
      # safe to carry onto that grain as-is; anything above 0 means it needs an
      # attribution rule (Task 13). Keyed by grain column name.
      fanout_by_key: Dict[str, float] = field(default_factory=dict)
  ```
- On `JuniorEngine`:
  ```python
  def profile_tables(self, tenant_id: str, tables: Optional[List[str]] = None,
                     force: bool = False) -> Dict[str, List[ColumnProfile]]: ...
  ```
  `tables=None` profiles every table in the catalog. `force=False` skips tables already profiled — this is what makes the stakeholder's inline call cheap on the second question.

**Storage:** one `DEFINITION` node per table, titled `"Column Profile: <table>"`, `status=APPROVED`, payload `{"table": t, "columns": [asdict(p), …], "profiled_at": …, "row_count_estimate": n}`. One node per table, not one giant node, so a single wide table can be re-profiled without rewriting everything and so `brain.search` can surface the relevant table.

**How to profile — one query per table, not one per column.** Fetch a bounded sample and compute in pandas, rather than issuing `SELECT DISTINCT` per column against the warehouse:

```python
sample_sql = f"SELECT * FROM {table} LIMIT {profile_sample_rows}"   # default 50_000
```

Then per column: `nunique()`, `isna().mean()`, `value_counts()`, and `min()/max()` for numeric/datetime dtypes. Set `values_complete = distinct_count <= PROFILE_CARDINALITY_CAP`.

**Fan-out measurement — the number that makes attribution visible.** After the per-column pass, take the identifier columns (name ends `_id`/`_key`, or `distinct_count` ≥ 90% of sampled rows) as candidate grain keys, cap at 5 by descending distinct count, and for each `(key, categorical_column)` pair compute in pandas:

```python
per_key = sample.groupby(key)[col].nunique()
fanout = float((per_key > 1).mean())     # share of keys with >1 distinct value
```

Store it on the column's `fanout_by_key[key]`. Only for low-cardinality columns (`distinct_count <= PROFILE_CARDINALITY_CAP`) — fan-out on a free-text column is noise. This is the measurement that turns "5-7% of sessions span multiple service lines" from tribal knowledge into a number the planner can read.

**`distinct_count` and `row_count_estimate` are load-bearing for Task 7, not just for prompts.** They are the inputs to the cube cell-count guard — `min(∏ distinct_count(d), row_count_estimate)` — which is what stops the analyst composing a `GROUP BY` that returns more rows than the transport can carry. Two consequences for the implementer: (a) `row_count_estimate` must be the table's real row count where it can be obtained cheaply (`SELECT COUNT(*)`, routed through `QueryPolicy` like everything else), and only the sample size as a documented fallback — an estimate of 50,000 on a 1.2M-row table would let the guard wave through a cube 24× larger than it believes; (b) a column with **no** profile must be distinguishable from one profiled as low-cardinality, so that Task 7 can fail closed on the former. Absent profiles are absent, never defaulted to zero.

**Two honesty rules the implementer must not skip:**
- The sample bounds what you can claim. If `len(sample) >= profile_sample_rows`, the sample may not have seen every value — so `values_complete` must be forced to `False` for **every** column of that table regardless of its distinct count, and `distinct_count` recorded as a floor. A `values_complete: true` that is actually a sample artifact will make the LLM emit a `WHERE status IN (...)` that silently drops rows.
- Coerce every value to `str` and truncate each to 100 chars before storing. Values land in an LLM prompt; an unbounded free-text column would blow the context.

**Route the sample through `QueryPolicy`** the way `_synthesize_and_execute_sql` does (`stakeholder.py:670-687`) rather than calling `executor.execute` on a raw f-string.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_column_profiler.py
def test_low_cardinality_column_stores_every_value(engine, tenant, fake_executor):
    fake_executor.returns(pd.DataFrame({
        "order_id": [f"o{i}" for i in range(100)],
        "status": ["COMPLETED", "CANCELLED"] * 50,
    }))
    profiles = engine.profile_tables(tenant, ["orders"])["orders"]
    status = next(p for p in profiles if p.column == "status")
    assert status.distinct_count == 2
    assert sorted(status.values) == ["CANCELLED", "COMPLETED"]
    assert status.values_complete is True

def test_high_cardinality_column_is_capped_to_top_n(engine, tenant, fake_executor):
    fake_executor.returns(pd.DataFrame({"city": [f"city_{i % 300}" for i in range(3000)]}))
    city = engine.profile_tables(tenant, ["orders"])["orders"][0]
    assert city.distinct_count == 300
    assert len(city.values) == PROFILE_TOP_VALUES
    assert city.values_complete is False

def test_a_saturated_sample_never_claims_completeness(engine, tenant, fake_executor):
    """The sample hit its ceiling, so even a 2-value column might have unseen values."""
    engine.settings.profile_sample_rows = 100
    fake_executor.returns(pd.DataFrame({"status": ["A", "B"] * 50}))   # exactly 100 rows
    status = engine.profile_tables(tenant, ["orders"])["orders"][0]
    assert status.values_complete is False

def test_numeric_and_date_columns_carry_a_range(engine, tenant, fake_executor):
    fake_executor.returns(pd.DataFrame({
        "revenue": [1.0, 500.0, 99.0],
        "order_date": pd.to_datetime(["2026-01-01", "2026-06-30", "2026-03-15"]),
    }))
    profiles = {p.column: p for p in engine.profile_tables(tenant, ["orders"])["orders"]}
    assert profiles["revenue"].min_value == "1.0" and profiles["revenue"].max_value == "500.0"
    assert profiles["order_date"].min_value.startswith("2026-01-01")

def test_null_fraction_is_recorded(engine, tenant, fake_executor):
    fake_executor.returns(pd.DataFrame({"coupon": [None, None, "X", "Y"]}))
    assert engine.profile_tables(tenant, ["orders"])["orders"][0].null_fraction == 0.5

def test_profiles_persist_as_one_node_per_table(engine, tenant, fake_executor):
    engine.profile_tables(tenant, ["orders", "sessions"])
    titles = {n.title for n in engine.brain(tenant).all(kind=NodeKind.DEFINITION)}
    assert "Column Profile: orders" in titles and "Column Profile: sessions" in titles

def test_second_call_skips_already_profiled_tables(engine, tenant, fake_executor):
    engine.profile_tables(tenant, ["orders"])
    n = fake_executor.call_count
    engine.profile_tables(tenant, ["orders"])
    assert fake_executor.call_count == n            # cached
    engine.profile_tables(tenant, ["orders"], force=True)
    assert fake_executor.call_count > n             # forced

def test_long_values_are_truncated(engine, tenant, fake_executor):
    fake_executor.returns(pd.DataFrame({"notes": ["x" * 5000, "y" * 5000]}))
    assert all(len(v) <= 100 for v in engine.profile_tables(tenant, ["orders"])["orders"][0].values)

def test_fanout_detects_a_multi_valued_categorical(engine, tenant, fake_executor):
    """s1 touches two service lines, s2 and s3 touch one -> fan-out is 1/3."""
    fake_executor.returns(pd.DataFrame({
        "session_id":   ["s1", "s1", "s2", "s3"],
        "service_line": ["mobile", "fixed", "mobile", "ott"],
    }))
    sl = next(p for p in engine.profile_tables(tenant, ["events"])["events"]
              if p.column == "service_line")
    assert sl.fanout_by_key["session_id"] == pytest.approx(1 / 3)

def test_a_clean_categorical_has_zero_fanout(engine, tenant, fake_executor):
    fake_executor.returns(pd.DataFrame({
        "session_id": ["s1", "s1", "s2"],
        "country":    ["DE", "DE", "IN"],
    }))
    c = next(p for p in engine.profile_tables(tenant, ["events"])["events"] if p.column == "country")
    assert c.fanout_by_key["session_id"] == 0.0
```

```python
# tests/test_junior.py — the bug fix
def test_refresh_catalog_updates_in_place_instead_of_duplicating(self):
    self.engine.refresh_catalog(self.tid)
    self.engine.refresh_catalog(self.tid)
    nodes = [n for n in self.engine.brain(self.tid).all(kind=NodeKind.DEFINITION)
             if n.title == "Database Catalog"]
    self.assertEqual(len(nodes), 1)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_column_profiler.py tests/test_junior.py -q`
Expected: FAIL — `AttributeError: 'JuniorEngine' object has no attribute 'profile_tables'`, and the catalog test finds 2 nodes.

- [ ] **Step 3: Implement**

Add `profile_sample_rows: int = 50_000` to `Settings` in `config.py`, following the existing env-reading pattern.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/junior.py analytics_platform/domain.py analytics_platform/config.py tests/test_column_profiler.py tests/test_junior.py && git commit -m "feat(junior): profile column values into the brain; stop duplicating the catalog node"
```

---

### Task 6: The semantic layer

**Files:**
- Create: `analytics_platform/semantic.py`
- Modify: `analytics_platform/domain.py` (the dataclasses), `analytics_platform/api.py` (two routes)
- Test: Create `tests/test_semantic.py`

**Why this exists.** A schema tells the LLM that `funnel_events.status` exists and holds `'completed'`. It does not tell it that *conversion rate* means `completed_applications / eligible_applications`, is measured **at session grain**, is validly sliced by country / device / channel / date, and **excludes test traffic**. Without that, the analyst produces queries that are technically valid and analytically wrong — and a wrong number delivered confidently is worse than no number. This is the layer that makes the difference, and it is the thing this product is actually selling.

**It lives in the Company Brain, not a new store.** `NodeKind.METRIC` already exists, the review workflow (`DRAFT → submit → approve`) already exists, and metric definitions are exactly the kind of fact that should pass a senior's review before it silently reshapes every answer. What is missing is *structure* on the payload.

**Interfaces:**
- Produces, in `analytics_platform/domain.py`:
  ```python
  @dataclass
  class SemanticMetric:
      name: str                       # "conversion_rate"
      definition: str                 # "completed_applications / eligible_applications"
      grain: List[str]                # ["session_id"] -- the grain the metric is valid at
      dimensions: List[str]           # ["country", "device", "channel", "date"]
      source_tables: List[str]        # ["funnel_events"]
      filters: List[str]              # ["is_test_traffic = false"] -- ALWAYS applied
      caveats: List[str]              # "excludes test traffic"
      freshness: str = ""             # "daily, T+1"
      owner: str = ""
      aliases: List[str] = field(default_factory=list)   # "CVR", "conversion"

  @dataclass
  class SemanticDimension:
      name: str
      column: str
      source_tables: List[str]
      description: str = ""
      values: List[str] = field(default_factory=list)    # from the Task 5 profile when known
      aliases: List[str] = field(default_factory=list)
  ```
- Produces, in `analytics_platform/semantic.py`:
  ```python
  class SemanticLayer:
      def __init__(self, brain_for) -> None: ...
      def metrics(self, tenant_id: str, approved_only: bool = True) -> List[SemanticMetric]: ...
      def dimensions(self, tenant_id: str, approved_only: bool = True) -> List[SemanticDimension]: ...
      def upsert_metric(self, tenant_id: str, m: SemanticMetric, by: str) -> KnowledgeNode: ...
      def resolve(self, tenant_id: str, question: str) -> "SemanticResolution": ...
      def render(self, res: "SemanticResolution") -> str: ...

  @dataclass
  class SemanticResolution:
      metrics: List[SemanticMetric]         # matched by name or alias, in the question
      dimensions: List[SemanticDimension]
      required_filters: List[str]           # union of every matched metric's filters
      caveats: List[str]
      unresolved_terms: List[str]           # measure-ish words with no metric -> uncertainty
  ```

**Matching is lexical and deliberately dumb**: lowercase, strip punctuation, match metric `name` and each `aliases` entry as a whole-token match against the question, plus the existing `brain.search(question, kind=NodeKind.METRIC)` for semantic recall. Union the two, dedupe by name. Do **not** build another embedding path — `BrainIndex` already fuses lexical and dense recall.

**Approved-only by default.** A DRAFT metric definition must never silently steer an answer, exactly like the attribution rules in Task 13.

**`unresolved_terms` is the uncertainty signal.** When the question contains a measure-like term (`rate`, `conversion`, `churn`, `retention`, `margin`, `AOV`, …) with no matching metric, record it. Task 14 turns it into a visible caveat: *"'churn' is not a defined metric for this company — the figure below is computed from raw events and has not been validated against an approved definition."* That is the difference between an analyst and a confabulator.

**Rendered block** — goes into every prompt ahead of the schema:

```
=== BUSINESS SEMANTICS (authoritative -- these definitions override your own assumptions) ===

METRIC conversion_rate  (aliases: CVR, conversion)
  Definition : completed_applications / eligible_applications
  Grain      : session_id
  Dimensions : country, device, channel, date
  Source     : funnel_events
  ALWAYS APPLY: is_test_traffic = false
  Caveats    : excludes test traffic; backfills land T+1

DIMENSION country -> funnel_events.country

RULES:
- Compute a metric only at its stated grain. If the question needs it at a
  different grain, say so rather than silently re-deriving it.
- Every filter under ALWAYS APPLY is mandatory in every query touching that
  metric, whether or not the user mentioned it.
- Slice only by the dimensions listed for that metric.
- If a measure in the question has no metric defined here, say so explicitly
  in your rationale. Do not invent a definition.
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_semantic.py
def test_upsert_then_read_roundtrips(layer, tenant):
    m = SemanticMetric(name="conversion_rate", definition="a / b", grain=["session_id"],
                       dimensions=["country"], source_tables=["funnel_events"],
                       filters=["is_test_traffic = false"], caveats=["excludes test traffic"],
                       aliases=["CVR"])
    layer.upsert_metric(tenant, m, by="senior")
    got = layer.metrics(tenant, approved_only=False)[0]
    assert got.grain == ["session_id"] and got.filters == ["is_test_traffic = false"]

def test_resolve_matches_on_alias(layer, tenant, approved_conversion_metric):
    res = layer.resolve(tenant, "how did CVR trend in Germany?")
    assert [m.name for m in res.metrics] == ["conversion_rate"]

def test_resolve_collects_required_filters(layer, tenant, approved_conversion_metric):
    assert layer.resolve(tenant, "conversion by device").required_filters == ["is_test_traffic = false"]

def test_draft_metrics_are_excluded_by_default(layer, tenant, draft_metric):
    assert layer.metrics(tenant) == []
    assert len(layer.metrics(tenant, approved_only=False)) == 1

def test_an_undefined_measure_becomes_an_unresolved_term(layer, tenant, approved_conversion_metric):
    res = layer.resolve(tenant, "what is our churn rate?")
    assert "churn" in res.unresolved_terms

def test_render_marks_filters_as_mandatory(layer, tenant, approved_conversion_metric):
    r = layer.render(layer.resolve(tenant, "conversion by country"))
    assert "ALWAYS APPLY: is_test_traffic = false" in r
    assert "Grain      : session_id" in r

def test_upsert_updates_in_place_rather_than_duplicating(layer, tenant):
    layer.upsert_metric(tenant, _metric("conversion_rate"), by="senior")
    layer.upsert_metric(tenant, _metric("conversion_rate", definition="c / d"), by="senior")
    all_metrics = layer.metrics(tenant, approved_only=False)
    assert len(all_metrics) == 1 and all_metrics[0].definition == "c / d"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_semantic.py -q`
Expected: FAIL — `ModuleNotFoundError: analytics_platform.semantic`

- [ ] **Step 3: Implement**

Store one `METRIC` node per metric, titled `"Metric: <name>"`, payload `asdict(SemanticMetric)`; one `DEFINITION` node per dimension titled `"Dimension: <name>"`. `upsert_metric` looks up by title and uses `BrainStore.update_field(node.id, "payload", dump_json(...))` (`brain/store.py:138`) when it exists — the same in-place update Task 5 applies to the catalog node. Do not repeat the duplicate-node bug.

Add two routes to `api.py` beside the existing knowledge block: `GET /knowledge/{tenant_id}/semantic` (metrics + dimensions, with a `?approved_only=` flag) and `POST /knowledge/{tenant_id}/semantic/metrics` (upsert, creating at `DRAFT`). Approval reuses the existing `POST /knowledge/{tenant_id}/{node_id}/review` endpoint (`api.py:766-779`) — no parallel approval path.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/semantic.py analytics_platform/domain.py analytics_platform/api.py tests/test_semantic.py && git commit -m "feat(semantic): typed metric/dimension layer over the company brain"
```

---

### Task 7: `BaseView` — a governed ID-grain population, and cubes composed over it

**Files:**
- Create: `analytics_platform/base_view.py`
- Modify: `analytics_platform/domain.py` (the dataclasses), `analytics_platform/config.py` (three constants), `analytics_platform/api.py` (two routes)
- Test: Create `tests/test_base_view.py`

**Why this exists — read this before writing a line of it.** Two questions asked of the same warehouse should be answerable from the same rows, so that when their numbers are compared they either agree or the disagreement is explainable. Today every turn authors its own `FROM … JOIN … WHERE`, so two answers can silently rest on two different row populations and nobody can prove which. The warehouse fix is `CREATE VIEW`. **This Athena account is read-only and no additional access is being requested**, so the view becomes a client-side construct: a stored, hashed, governed SQL definition that is **inlined verbatim as a CTE** into every query built on it.

**The base is at ID grain, and that is the whole point.** A base at `country × device × service_line × date` is useless the moment someone asks for a fifth dimension — that question needs a different base, and the two share nothing. A base at `session_id` makes every question a *projection* of one population: a new dimension is a column added above an unchanged base, not a rewrite of it. Task 10's containment rule then works one level up, on cube dimensions, exactly as it already works on grain.

**Population versus projection — why there are two hashes.** The base decides *which rows exist*. The columns selected above it are a projection. Adding a column cannot change a `SUM` over the same rows, so two answers must be allowed to differ in projection and still reconcile. Hence:

- **`population_hash`** — sha256 over the canonicalised base: `source_sql`, sorted `grain`, and the canonicalised `attributions`. This is the reconciliation key.
- **`projection_hash`** — sha256 over the sorted column list the cube exposes. Informational; it never gates reconciliation.

**Per-question filters are a slice, not a population.** A filter that is part of the *approved definition* (`is_test_traffic = false`, a partition floor, a join predicate) lives inside `source_sql` and is population. A filter the question adds (`country = 'Germany'`) sits in the cube's `WHERE` above the base and is a **slice** — recorded in the artifact, deliberately **not** hashed. That is what makes "question A filtered to Germany" and "question B unfiltered" reconcilable: same population, one is a slice of the other. Every metric's `ALWAYS APPLY` filters (Task 6) therefore belong in the base, not in the cube.

**Attribution belongs inside the base.** The `ROW_NUMBER() OVER (PARTITION BY <grain> …)` CTEs that collapse multi-valued categoricals are part of *which rows exist* and are decided once, at the base, then inherited by every cube. Letting each question re-derive them — which the plan previously allowed — means two questions can apply two different rankings to the same sessions and produce two defensible-looking, mutually contradictory numbers. That is precisely the failure the base exists to prevent, so attribution is inside `population_hash`.

**What crosses the transport is the cube, never the base.** Per Global Constraints, a single result must stay under `MAX_TRANSPORT_ROWS`. `GROUP BY` runs in the warehouse; a base of 1.2M sessions becomes a cube of tens of thousands of cells. ID-grain rows come down only through `compose_keyset_chunk`, and only when a measure is genuinely non-additive.

**Interfaces:**
- Consumes: `CompanyBrain` (create / `update_field` / `all` / `search`), `ColumnProfile.distinct_count` from Task 5, `SemanticMetric.filters` from Task 6.
- Produces, in `analytics_platform/domain.py`:
  ```python
  @dataclass
  class AttributionRule:
      """Moved here from the planner: attribution is a property of the base
      population, not of a question. Tasks 11 and 13 consume this definition."""
      column: str                  # the multi-valued categorical, e.g. "service_line"
      grain: List[str]             # the key it must collapse onto, e.g. ["session_id"]
      strategy: str                # "highest_intent" | "most_frequent" | "latest" | "first"
      priority_values: List[str] = field(default_factory=list)  # ranked, highest business value first
      tiebreakers: List[str] = field(default_factory=list)      # e.g. ["event_count DESC", "log_time DESC"]
      source: str = ""             # "brain" (approved rule) | "llm" (proposed) | "default"
      rationale: str = ""

  @dataclass
  class BaseView:
      name: str                       # "checkout_sessions"
      grain: List[str]                # ["session_id"] -- ID grain, never dimensional
      source_sql: str                 # the population: FROM/JOIN/WHERE, one row per grain key
      dimension_columns: List[str]    # what may appear in GROUP BY above this base
      measure_columns: List[str]      # what may be aggregated above this base
      attributions: List[AttributionRule] = field(default_factory=list)
      time_column: str = ""
      row_count_estimate: int = 0
      description: str = ""
      owner: str = ""
      aliases: List[str] = field(default_factory=list)

  @dataclass
  class CubeMeasure:
      name: str            # "revenue"
      expr: str            # what goes in the cube's SELECT: "SUM(revenue)"
      additive: bool       # can this roll up from the cube to a coarser grain?
      read_expr: str = ""  # how to read it back, when it differs from `name`

  @dataclass
  class CubeSpec:
      base_name: str
      dimensions: List[str]
      measures: List[CubeMeasure]
      filters: Dict[str, List[str]] = field(default_factory=dict)   # the SLICE -- not hashed
      time_column: str = ""
      time_start: str = ""
      time_end: str = ""

  @dataclass
  class CubeSQL:
      ok: bool
      sql: str = ""
      population_hash: str = ""
      projection_hash: str = ""
      estimated_cells: int = 0
      non_additive: List[str] = field(default_factory=list)
      warnings: List[str] = field(default_factory=list)
      error: str = ""                 # set when the guard refuses
      offending_dimensions: List[str] = field(default_factory=list)

  @dataclass
  class ReconcileResult:
      same_population: bool
      population_hash_a: str
      population_hash_b: str
      measure: str = ""
      value_a: Optional[float] = None
      value_b: Optional[float] = None
      agrees: bool = False
      explanation: str = ""           # written for a human; lands in the API response
  ```
- Produces, in `analytics_platform/base_view.py`:
  ```python
  class BaseViewRegistry:
      def __init__(self, brain_for) -> None: ...
      def all(self, tenant_id: str, approved_only: bool = True) -> List[BaseView]: ...
      def get(self, tenant_id: str, name: str, approved_only: bool = True) -> Optional[BaseView]: ...
      def upsert(self, tenant_id: str, view: BaseView, by: str) -> KnowledgeNode: ...   # creates at DRAFT
      def population_hash(self, view: BaseView) -> str: ...
      def projection_hash(self, columns: List[str]) -> str: ...
      def render(self, views: List[BaseView]) -> str: ...              # the prompt block
      def compose_cube(self, view: BaseView, spec: CubeSpec,
                       profiles: Dict[str, "ColumnProfile"]) -> CubeSQL: ...
      def compose_keyset_chunk(self, view: BaseView, spec: CubeSpec,
                               last_seen: str, chunk_rows: int) -> str: ...

  def reconcile(population_hash_a: str, value_a: float,
                population_hash_b: str, value_b: float,
                measure: str, tolerance: float = 1e-6) -> ReconcileResult: ...
  ```
- Produces, in `analytics_platform/config.py`:
  ```python
  MAX_CUBE_CELLS = 200_000            # a composed cube may not exceed this many rows
  MAX_DIMENSION_CARDINALITY = 5_000   # a column above this is not a cube dimension
  EXTRACT_CHUNK_ROWS = 50_000         # keyset page size, <= MAX_TRANSPORT_ROWS
  ```

**Canonicalisation for the hash — specify it exactly, because everything rests on it.** Strip SQL line and block comments; collapse every run of whitespace to a single space; strip leading and trailing whitespace. **Do not lowercase** — string literals are case-sensitive and `'mobile'` is not `'Mobile'`. Canonicalise `attributions` by sorting the rule list on `(column, ",".join(grain))` and, within each rule, serialising `strategy`, `priority_values` **in their given order** (the ranking is the semantics — never sort it), and `tiebreakers` in their given order. Sort `grain` before hashing; grain is a set.

A cosmetic reformat of `source_sql` therefore changes `population_hash`. That is correct and intended: the base is a governed artifact edited by a human through a review flow, not a string an LLM re-emits each turn, and a human edit *should* announce that answers before and after it are no longer trivially comparable.

**Cube composition — the exact SQL shape.** The stored `source_sql` is inlined **byte for byte**. The LLM never re-authors it; that is what makes the hash mean anything.

```sql
WITH base AS (
    <view.source_sql verbatim>
)
SELECT country, device, service_line, date
     , COUNT(*)      AS sessions
     , SUM(revenue)  AS revenue
FROM base
WHERE country IN ('Germany')
  AND date BETWEEN DATE '2026-08-01' AND DATE '2026-08-31'
GROUP BY 1, 2, 3, 4
```

Slice filters are emitted as `IN (…)` lists over `spec.filters`, with every literal single-quoted and internal quotes doubled. `GROUP BY` is emitted **ordinally** (`GROUP BY 1, 2, 3, 4`) to match the dimension list positionally — Athena accepts it and it cannot drift out of sync with the `SELECT`.

**The additivity table — implement exactly this.** It is what lets a cube be reused at a coarser grain, and getting it wrong produces confidently wrong numbers:

| Measure the question wants | In the cube | `additive` | Notes |
|---|---|---|---|
| `SUM(x)` | `SUM(x) AS x` | `True` | |
| `COUNT(*)` | `COUNT(*) AS n` | `True` | |
| `MIN(x)` / `MAX(x)` | as written | `True` | |
| `AVG(x)` | **`SUM(x) AS x_sum, COUNT(x) AS x_count`** | `True` | `read_expr = "x_sum / NULLIF(x_count, 0)"`. **Never store `AVG` itself** — averaging averages is wrong the moment the cube is rolled up |
| ratio of two additive measures | both numerator and denominator, separately | `True` | divided at read time, same reason |
| `COUNT(DISTINCT x)` | as written | `False` | distinct counts do not sum across cells |
| median, `APPROX_PERCENTILE` | as written | `False` | |

`compose_cube` performs the `AVG` rewrite itself rather than trusting the caller, and lists every `additive=False` measure in `CubeSQL.non_additive`. A cube containing a non-additive measure is still valid **at its own grain**; Task 10 is what forbids reusing it at a coarser one.

**The cell-count guard.** Before returning SQL:

```python
estimated_cells = min(
    product(distinct_count(d) for d in spec.dimensions),   # from the Task 5 profile
    view.row_count_estimate or MAX_CUBE_CELLS,             # a cube cannot exceed its base
)
```

Refuse with `ok=False` when `estimated_cells > MAX_CUBE_CELLS`, naming the largest-cardinality dimensions in `offending_dimensions` so the planner can drop or bucket them rather than guess. Refuse outright — never silently — any dimension whose `distinct_count > MAX_DIMENSION_CARDINALITY`, or that Task 8 tagged `[identifier]`: those are keys and free text, not dimensions. When a dimension has no profiled `distinct_count`, assume `MAX_DIMENSION_CARDINALITY` (pessimistic) and record it in `warnings` — an unprofiled column must not be able to sneak a 10M-cell cube past the guard.

**`MAX_CUBE_CELLS` is not the transport ceiling, and this guard is not trying to be one.** `MAX_CUBE_CELLS` (200,000) answers *is this cube worth composing at all*; `max_transport_rows` (50,000) answers *what fits in one round trip*. A cube of 120,000 cells passes this guard and is then fetched in three keyset pages by Task 12. Do not lower `MAX_CUBE_CELLS` to the transport size to "make them agree" — that would refuse cubes the system can perfectly well retrieve, and it would push the analyst back toward narrower, less reusable cuts.

**Keyset pagination.** `compose_keyset_chunk` emits `ORDER BY <keys> LIMIT <chunk_rows>` pages, each carrying `WHERE <keys> > <last row's values>`. **Never `OFFSET`** — Athena rescans from the top on every page, which is quadratic and, on a changing table, silently skips and duplicates rows. Composite keys use a lexicographic row-value comparison. Two callers use it: ID-grain rows when a measure is genuinely non-additive (the escape hatch — the planner must justify it), and any cube whose `estimated_cells` exceeds `max_transport_rows`, paged over `spec.dimensions` instead of `view.grain`. Same function, same ordering discipline; take the key list as a parameter rather than reading `view.grain` unconditionally.

**Governance, and the day-one problem.** A base view is a `DEFINITION` node titled `"Base View: <name>"`, created at **DRAFT** and promoted through the existing `brain.submit` / `brain.approve` flow — no new machinery, same as metrics and attribution rules. `all()` and `get()` are `approved_only=True` by default.

But refusing to answer anything until a human has approved a base view makes the product unusable on its first day. So: **when no approved base view fits, the planner may propose one; it is stored as DRAFT and used for that turn, and the answer is marked unvalidated** — `AnalysisArtifact.base_view_approved = False` plus the caveat *"this answer rests on an unreviewed base view definition; figures are provisional until it is approved."* Approved views are always preferred over drafts. This is the same shape as Task 13's attribution rules: the machine proposes the *existence* of the thing, a human supplies the judgement.

**Rendered block** — goes into the planning and synthesis prompts, after the semantics and before the schema:

```
=== BASE VIEWS (the row populations you may build on) ===

BASE checkout_sessions  [APPROVED]
  Grain      : session_id  (one row per session)
  Rows       : ~1,240,000
  Dimensions : country, device, service_line, channel, date
  Measures   : revenue, is_converted
  Attribution: service_line collapsed to one value per session by highest intent
               (mobile > fixed > ott)
  Population : orders JOIN sessions, test traffic excluded, dt >= 2024-01-01

BASE guest_checkouts  [DRAFT -- unreviewed, answers using it are provisional]
  ...

RULES:
- Choose exactly ONE base view and name it. Every number in your answer must come
  from that one population, so that this answer can be compared against others.
- You do NOT write the base. It is inlined verbatim for you. You choose the
  dimensions to GROUP BY, the measures, and the filters that slice it.
- Slice and group using only that base's listed dimension columns. If the question
  needs a column the base does not carry, say so -- do not reach around the base
  into a raw table, because a number produced that way cannot be reconciled with
  anything.
- Prefer additive measures (SUM, COUNT(*), MIN, MAX). Ask for AVG as a plain AVG
  and it will be stored as a sum and a count for you. COUNT(DISTINCT), medians and
  percentiles do not roll up -- name them only when the question truly needs them.
- If no base view fits, propose one at ID grain (one row per identifier) and say
  so in your rationale. It will be recorded as a DRAFT for human review and the
  answer will be marked provisional.
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_base_view.py
import pytest
from analytics_platform.base_view import BaseViewRegistry, reconcile
from analytics_platform.domain import (BaseView, CubeSpec, CubeMeasure, AttributionRule,
                                       ColumnProfile, MAX_CUBE_CELLS)

def _view(**kw):
    d = dict(name="checkout_sessions", grain=["session_id"],
             source_sql="SELECT session_id, country, device, revenue FROM orders "
                        "WHERE is_test_traffic = false",
             dimension_columns=["country", "device"], measure_columns=["revenue"],
             row_count_estimate=1_200_000)
    d.update(kw)
    return BaseView(**d)

def _profiles(**counts):
    return {c: ColumnProfile(column=c, dtype="object", distinct_count=n, null_fraction=0.0,
                             values=[], values_complete=False) for c, n in counts.items()}

# --- the two hashes -------------------------------------------------------

def test_projection_does_not_change_the_population_hash(registry):
    """Question B adds a column. Same rows, so the same population."""
    v = _view()
    a = registry.compose_cube(v, CubeSpec(base_name=v.name, dimensions=["country"],
            measures=[CubeMeasure("revenue", "SUM(revenue)", True)]),
            _profiles(country=30, device=4))
    b = registry.compose_cube(v, CubeSpec(base_name=v.name, dimensions=["country", "device"],
            measures=[CubeMeasure("revenue", "SUM(revenue)", True)]),
            _profiles(country=30, device=4))
    assert a.population_hash == b.population_hash
    assert a.projection_hash != b.projection_hash

def test_a_slice_filter_does_not_change_the_population_hash(registry):
    """The user's requirement: A filtered to Germany and B unfiltered must reconcile."""
    v = _view()
    spec = lambda f: CubeSpec(base_name=v.name, dimensions=["country"],
                              measures=[CubeMeasure("revenue", "SUM(revenue)", True)], filters=f)
    p = _profiles(country=30)
    assert (registry.compose_cube(v, spec({}), p).population_hash
            == registry.compose_cube(v, spec({"country": ["Germany"]}), p).population_hash)

def test_a_different_source_sql_changes_the_population_hash(registry):
    assert (registry.population_hash(_view())
            != registry.population_hash(_view(source_sql="SELECT session_id FROM orders")))

def test_whitespace_and_comments_do_not_change_the_population_hash(registry):
    a = _view(source_sql="SELECT session_id FROM orders WHERE is_test_traffic = false")
    b = _view(source_sql="-- the population\nSELECT   session_id\n  FROM orders\n"
                         "  WHERE is_test_traffic = false\n")
    assert registry.population_hash(a) == registry.population_hash(b)

def test_literal_casing_does_change_the_population_hash(registry):
    """'mobile' is not 'Mobile'. Canonicalisation must never lowercase."""
    a = _view(source_sql="SELECT session_id FROM orders WHERE service_line = 'mobile'")
    b = _view(source_sql="SELECT session_id FROM orders WHERE service_line = 'Mobile'")
    assert registry.population_hash(a) != registry.population_hash(b)

def test_a_different_attribution_ranking_changes_the_population_hash(registry):
    """The whole reason attribution lives in the base: two rankings are two populations."""
    r1 = AttributionRule(column="service_line", grain=["session_id"], strategy="highest_intent",
                         priority_values=["mobile", "fixed", "ott"])
    r2 = AttributionRule(column="service_line", grain=["session_id"], strategy="highest_intent",
                         priority_values=["ott", "fixed", "mobile"])
    assert (registry.population_hash(_view(attributions=[r1]))
            != registry.population_hash(_view(attributions=[r2])))

def test_grain_order_does_not_change_the_population_hash(registry):
    assert (registry.population_hash(_view(grain=["session_id", "dt"]))
            == registry.population_hash(_view(grain=["dt", "session_id"])))

# --- cube composition -----------------------------------------------------

def test_the_base_is_inlined_verbatim_as_a_cte(registry):
    v = _view()
    out = registry.compose_cube(v, CubeSpec(base_name=v.name, dimensions=["country"],
            measures=[CubeMeasure("revenue", "SUM(revenue)", True)]), _profiles(country=30))
    assert out.ok
    assert out.sql.startswith("WITH base AS (")
    assert v.source_sql in out.sql          # byte for byte, not paraphrased
    assert "GROUP BY 1" in out.sql

def test_slice_filters_are_emitted_above_the_base(registry):
    v = _view()
    out = registry.compose_cube(v, CubeSpec(base_name=v.name, dimensions=["country"],
            measures=[CubeMeasure("revenue", "SUM(revenue)", True)],
            filters={"country": ["Germany", "France"]}), _profiles(country=30))
    body = out.sql.split("FROM base", 1)[1]
    assert "country IN ('Germany', 'France')" in body

def test_a_quote_in_a_filter_literal_is_escaped(registry):
    v = _view()
    out = registry.compose_cube(v, CubeSpec(base_name=v.name, dimensions=["country"],
            measures=[CubeMeasure("revenue", "SUM(revenue)", True)],
            filters={"country": ["Côte d'Ivoire"]}), _profiles(country=30))
    assert "'Côte d''Ivoire'" in out.sql

# --- additivity -----------------------------------------------------------

def test_avg_is_stored_as_a_sum_and_a_count(registry):
    v = _view()
    out = registry.compose_cube(v, CubeSpec(base_name=v.name, dimensions=["country"],
            measures=[CubeMeasure("revenue", "AVG(revenue)", True)]), _profiles(country=30))
    assert "SUM(revenue) AS revenue_sum" in out.sql
    assert "COUNT(revenue) AS revenue_count" in out.sql
    assert "AVG(" not in out.sql
    m = out_measure(out, "revenue")
    assert m.additive is True and m.read_expr == "revenue_sum / NULLIF(revenue_count, 0)"

def test_count_distinct_is_marked_non_additive(registry):
    v = _view()
    out = registry.compose_cube(v, CubeSpec(base_name=v.name, dimensions=["country"],
            measures=[CubeMeasure("users", "COUNT(DISTINCT user_id)", True)]),
            _profiles(country=30))
    assert out.ok and out.non_additive == ["users"]

def test_sum_and_count_star_are_additive(registry):
    ...both measures...
    assert out.non_additive == []

# --- the cardinality guard ------------------------------------------------

def test_a_cube_that_would_explode_is_refused_with_the_culprit_named(registry):
    v = _view(dimension_columns=["country", "device", "city"])
    out = registry.compose_cube(v, CubeSpec(base_name=v.name,
            dimensions=["country", "device", "city"],
            measures=[CubeMeasure("revenue", "SUM(revenue)", True)]),
            _profiles(country=30, device=4, city=4_000))   # 480,000 cells
    assert not out.ok and out.offending_dimensions == ["city"]
    assert str(MAX_CUBE_CELLS) in out.error or "200" in out.error

def test_the_estimate_is_capped_by_the_bases_own_row_count(registry):
    """4 dims whose product exceeds the base cannot produce more cells than rows."""
    v = _view(row_count_estimate=90_000, dimension_columns=["a", "b", "c"])
    out = registry.compose_cube(v, CubeSpec(base_name=v.name, dimensions=["a", "b", "c"],
            measures=[CubeMeasure("n", "COUNT(*)", True)]), _profiles(a=100, b=100, c=100))
    assert out.ok and out.estimated_cells == 90_000

def test_a_high_cardinality_column_is_never_a_dimension(registry):
    out = registry.compose_cube(_view(dimension_columns=["session_id"]),
            CubeSpec(base_name="checkout_sessions", dimensions=["session_id"],
                     measures=[CubeMeasure("n", "COUNT(*)", True)]),
            _profiles(session_id=1_200_000))
    assert not out.ok and "session_id" in out.offending_dimensions

def test_an_unprofiled_dimension_is_assumed_worst_case_and_warned(registry):
    out = registry.compose_cube(_view(dimension_columns=["country", "mystery"]),
            CubeSpec(base_name="checkout_sessions", dimensions=["country", "mystery"],
                     measures=[CubeMeasure("n", "COUNT(*)", True)]), _profiles(country=30))
    assert not out.ok
    assert any("mystery" in w and "not profiled" in w for w in out.warnings)

# --- keyset pagination ----------------------------------------------------

def test_keyset_chunk_uses_a_cursor_not_an_offset(registry):
    sql = registry.compose_keyset_chunk(_view(), CubeSpec(base_name="checkout_sessions",
              dimensions=[], measures=[]), last_seen="s_004999", chunk_rows=50_000)
    assert "session_id > 's_004999'" in sql
    assert "ORDER BY session_id" in sql and "LIMIT 50000" in sql
    assert "OFFSET" not in sql.upper()

def test_the_first_keyset_chunk_has_no_cursor_predicate(registry):
    sql = registry.compose_keyset_chunk(_view(), CubeSpec(base_name="checkout_sessions",
              dimensions=[], measures=[]), last_seen="", chunk_rows=50_000)
    assert ">" not in sql.split("FROM base", 1)[1].split("ORDER BY", 1)[0]

# --- governance -----------------------------------------------------------

def test_draft_base_views_are_excluded_by_default(registry, tenant, draft_view):
    assert registry.all(tenant) == []
    assert len(registry.all(tenant, approved_only=False)) == 1

def test_upsert_updates_in_place_rather_than_duplicating(registry, tenant):
    registry.upsert(tenant, _view(), by="senior")
    registry.upsert(tenant, _view(description="clearer"), by="senior")
    assert len(registry.all(tenant, approved_only=False)) == 1

def test_render_marks_a_draft_as_provisional(registry, tenant, draft_view):
    r = registry.render(registry.all(tenant, approved_only=False))
    assert "[DRAFT" in r and "provisional" in r.lower()

def test_render_lists_the_attribution_so_a_reader_sees_it(registry, tenant, approved_view):
    assert "highest intent" in registry.render(registry.all(tenant)).lower()

# --- reconcile ------------------------------------------------------------

def test_same_population_and_equal_values_reconcile(registry):
    r = reconcile("h1", 1_234.0, "h1", 1_234.0, measure="revenue")
    assert r.same_population and r.agrees

def test_same_population_but_different_values_is_a_real_disagreement(registry):
    r = reconcile("h1", 1_234.0, "h1", 1_200.0, measure="revenue")
    assert r.same_population and not r.agrees
    assert "revenue" in r.explanation

def test_different_populations_cannot_be_compared_at_all(registry):
    r = reconcile("h1", 1_234.0, "h2", 1_234.0, measure="revenue")
    assert not r.same_population and not r.agrees
    assert "different" in r.explanation.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_base_view.py -q`
Expected: FAIL — `ModuleNotFoundError: analytics_platform.base_view`

- [ ] **Step 3: Implement**

Order: the dataclasses in `domain.py` first, then `population_hash` / `projection_hash` (they are the contract everything else asserts against), then `compose_cube` with the guard, then `compose_keyset_chunk`, then storage and `render`, then `reconcile`.

Storage mirrors Task 6 exactly: one `DEFINITION` node per view titled `"Base View: <name>"`, payload `asdict(BaseView)`, `upsert` looking up by title and calling `BrainStore.update_field(node.id, "payload", dump_json(...))` (`brain/store.py:138`) when it exists. **Do not repeat the duplicate-node bug** Task 5 fixes in `refresh_catalog`.

`compose_cube` returns SQL as a string; it does **not** execute anything and does **not** import the executor. Composition and execution stay separate so this file is testable without a warehouse — and so the same composed SQL can be hashed, logged, and shown to a human before anyone runs it.

Add two routes to `api.py` beside the semantic-layer routes from Task 6: `GET /knowledge/{tenant_id}/base-views` (with an `?approved_only=` flag) and `POST /knowledge/{tenant_id}/base-views` (upsert, creating at `DRAFT`). Approval reuses the existing `POST /knowledge/{tenant_id}/{node_id}/review` endpoint (`api.py:766-779`) — no parallel approval path.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/base_view.py analytics_platform/domain.py analytics_platform/config.py analytics_platform/api.py tests/test_base_view.py && git commit -m "feat(base-view): governed ID-grain populations with split population/projection hashes"
```

---

### Task 8: `SchemaContext` — put the semantics and the schema in front of the LLM

**Files:**
- Create: `analytics_platform/schema_context.py`
- Test: Create `tests/test_schema_context.py`

**This is the task that closes the "generic queries" gap.** Everything Tasks 5, 6, and 7 wrote to the brain is useless until something puts it in a prompt. `SchemaContext.rendered` is **three blocks in a fixed order — semantics, then base views, then schema** — business meaning first, then the populations that meaning may be measured over, then the physical layout underneath. When they disagree, semantics beat base views beat schema beat the retrieved examples.

**Interfaces:**
- Consumes: `JuniorEngine.get_catalog`, `JuniorEngine.profile_tables` (Task 5), `SemanticLayer.resolve` / `.render` (Task 6), `BaseViewRegistry.all` / `.render` (Task 7), `CompanyBrain.all` / `.search`.
- Produces:
  ```python
  class SchemaContextBuilder:
      def __init__(self, junior, brain_for, settings, semantic: "SemanticLayer",
                   base_views: "BaseViewRegistry") -> None: ...

      def relevant_tables(self, tenant_id: str, question: str,
                          query_nodes: List[Any], defn_nodes: List[Any]) -> List[str]: ...

      def build(self, tenant_id: str, question: str,
                query_nodes: List[Any], defn_nodes: List[Any],
                profile_if_missing: bool = True) -> SchemaContext: ...

  @dataclass
  class SchemaContext:
      tables: List[Dict[str, Any]]      # {table, columns:[ColumnProfile-as-dict]}
      semantics: "SemanticResolution"   # Task 6 -- metrics/dimensions matched to this question
      base_views: List["BaseView"]      # Task 7 -- approved first, then drafts
      profiles: Dict[str, "ColumnProfile"]   # column -> profile, flattened across tables;
                                             # this is what Task 7's cell guard is fed
      rendered: str                     # the prompt block: semantics, base views, schema
      profiled_now: List[str]           # tables profiled inline on this turn
      unprofiled: List[str]             # tables we could not profile -> becomes a caveat
  ```

**Base views rank above tables in selection.** A resolved base view's `source_sql` names the tables that certainly matter, so feed it through the same `brain.ingest.extract(sql)["tables"]` parser used for `query_nodes` and give those tables the same never-dropped standing as a matched metric's `source_tables`. A base view whose underlying tables were cut by the 8-table cap would be shown to the LLM as selectable while its schema was invisible — the worst of both.

**`profiles` is an output, not an internal.** Task 7's `compose_cube` needs `distinct_count` per candidate dimension, and Task 11's planner needs it to judge whether a cube will fit. Flatten it here, once, rather than making two later callers re-read brain nodes. On a column-name collision across tables, keep the entry from the table with the larger `row_count_estimate` and record the collision in `unprofiled`-style caveat text — a silently-wrong cardinality is exactly what the guard cannot survive.

**Table selection** — do not dump the whole warehouse into every prompt. Union of:
0. **`source_tables` of every metric and dimension the semantic layer resolved** — these rank first and are never dropped by the cap; a matched metric's source table is the one table the query certainly needs;
1. tables named in the retrieved `query_nodes`' SQL, via the existing `analytics_platform.brain.ingest.extract(sql)["tables"]` (already used this way at `junior.py:210-222` — reuse it, do not write a second SQL parser);
2. tables whose `defn_nodes` title matches `"Table: <name>"` (same convention `junior._tables` relies on);
3. tables whose name or column names appear as tokens in the question.

Cap at 8 tables. If the cap truncates, say so in the rendered block — an LLM that thinks it has seen every table will confidently join against one it was never shown.

**Inline profiling (the decision you made):** for any selected table with no `"Column Profile: <table>"` node, call `junior.profile_tables(tenant_id, [t])` right there, record it in `profiled_now`, and continue the same turn. Wrap in try/except: a table that cannot be profiled (permissions, executor failure) goes into `unprofiled` and the turn proceeds with columns-and-types only — it must never take the chat down. `profile_if_missing=False` exists so tests and batch callers can opt out.

**Rendered format** — the exact text the LLM sees. Compact, unambiguous, and explicit about completeness:

```
=== DATABASE SCHEMA (authoritative -- use these exact table and column names) ===

TABLE orders  (~1,240,000 rows)
  order_id        string    1,240,000 distinct, 0% null   [identifier]
  status          string    3 distinct, 0% null
                            ALL VALUES: 'COMPLETED', 'CANCELLED', 'REFUNDED'
  country         string    28 distinct, 0% null
                            ALL VALUES: 'IN', 'US', 'GB', ... (28 total, complete)
  service_line    string    3 distinct, 0% null
                            ALL VALUES: 'mobile', 'fixed', 'ott'
                            FAN-OUT: 6% of session_id have >1 value -- needs attribution
  city            string    ~300 distinct, 2% null
                            TOP 20 OF ~300 (not exhaustive): 'Mumbai', 'Delhi', ...
  revenue         float64   0% null   range 0.0 .. 48,500.0
  order_date      datetime  0% null   range 2024-01-01 .. 2026-08-14

RULES:
- Use only the table and column names listed above. If the question needs a column
  that is not listed, say so instead of inventing one.
- When a column shows ALL VALUES, that list is exhaustive -- filter using those exact
  literals, matching their exact casing. Never invent a value or guess its casing.
- When a column shows TOP N (not exhaustive), other values exist; prefer a range or a
  LIKE predicate over an IN list you cannot complete.
- Ranges are the true min/max in the data -- do not filter outside them.
- FAN-OUT means that column holds more than one value for a single key. If you
  extract at that key's grain, you MUST collapse the column with an explicit
  attribution rule -- never by adding it to GROUP BY, which silently changes the
  grain and double-counts those keys.
```

The `[identifier]` tag is a heuristic hint for the grain planner: flag a column when its name ends in `_id`/`_key` or its `distinct_count` is ≥ 90% of the sampled row count.

- [ ] **Step 1: Write the failing tests**

```python
def test_rendered_block_marks_a_complete_value_list_as_exhaustive(builder, tenant):
    ...status has 3 values, values_complete=True...
    assert "ALL VALUES: 'COMPLETED', 'CANCELLED', 'REFUNDED'" in ctx.rendered
    assert "TOP 20" not in ctx.rendered

def test_rendered_block_marks_a_capped_list_as_not_exhaustive(builder, tenant):
    ...city has 300 distinct, values_complete=False...
    assert "not exhaustive" in ctx.rendered

def test_unprofiled_table_is_reported_not_raised(builder, tenant, failing_profiler):
    ctx = builder.build(tenant, "q", [], [])
    assert ctx.unprofiled == ["orders"]
    assert ctx.rendered                      # still renders columns/types

def test_missing_profile_triggers_the_junior_inline(builder, tenant, spy_junior):
    ctx = builder.build(tenant, "revenue by country", [], [])
    assert spy_junior.profile_calls == [("orders",)]
    assert ctx.profiled_now == ["orders"]

def test_profile_if_missing_false_does_not_call_the_junior(builder, tenant, spy_junior):
    builder.build(tenant, "q", [], [], profile_if_missing=False)
    assert spy_junior.profile_calls == []

def test_tables_come_from_retrieved_query_sql(builder, tenant):
    node = FakeNode(payload={"sql": "SELECT * FROM sessions JOIN orders USING (order_id)"})
    assert set(builder.relevant_tables(tenant, "q", [node], [])) >= {"sessions", "orders"}

def test_table_selection_is_capped_and_says_so(builder, tenant):
    ...20 candidate tables...
    ctx = builder.build(tenant, "q", nodes, [])
    assert len(ctx.tables) == 8
    assert "truncated" in ctx.rendered.lower()

def test_identifier_columns_are_tagged(builder, tenant):
    assert "[identifier]" in ctx.rendered      # order_id: distinct == row count

def test_the_three_blocks_render_in_priority_order(builder, tenant, approved_conversion_metric,
                                                   approved_base_view):
    r = builder.build(tenant, "conversion by country", [], []).rendered
    assert r.index("BUSINESS SEMANTICS") < r.index("BASE VIEWS") < r.index("DATABASE SCHEMA")
    assert "ALWAYS APPLY: is_test_traffic = false" in r

def test_a_base_views_own_tables_survive_the_cap(builder, tenant, approved_base_view):
    """The base names funnel_events; 20 other candidates must not push it out."""
    ctx = builder.build(tenant, "conversion by country", twenty_query_nodes, [])
    assert "funnel_events" in [t["table"] for t in ctx.tables]

def test_profiles_are_flattened_for_the_cube_guard(builder, tenant):
    ctx = builder.build(tenant, "revenue by country", [], [])
    assert ctx.profiles["country"].distinct_count == 28

def test_a_tenant_with_no_base_views_still_builds(builder, tenant):
    """Day one: no base view exists yet. The turn must proceed and say so."""
    ctx = builder.build(tenant, "revenue by country", [], [])
    assert ctx.base_views == []
    assert "propose one at ID grain" in ctx.rendered

def test_a_matched_metrics_source_table_survives_the_cap(builder, tenant, approved_conversion_metric):
    """20 candidate tables, cap of 8 -- funnel_events is the metric's source and must be kept."""
    ctx = builder.build(tenant, "conversion by country", twenty_query_nodes, [])
    assert "funnel_events" in [t["table"] for t in ctx.tables]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_schema_context.py -q`
Expected: FAIL — `ModuleNotFoundError: analytics_platform.schema_context`

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/schema_context.py tests/test_schema_context.py && git commit -m "feat(schema): build a semantic + schema context block for LLM prompts"
```

---

### Task 9: `AnalyticalWorkspace` — DuckDB over the Parquet cache

**Files:**
- Create: `analytics_platform/execution/workspace.py`
- Modify: `requirements.txt`
- Test: Create `tests/test_workspace.py`

**Why DuckDB and not more pandas.** Once extracts are durable Parquet (Tasks 1-2), the follow-up questions that used to cost a Metabase round trip are set operations: filter, group, join two extracts on a shared key, window. DuckDB does those over Parquet files directly, out of core, in one line of SQL — and the analyst is *already* fluent in SQL, so a local re-cut needs no new prompt vocabulary. **Python is not displaced:** significance tests, trend decomposition, anomaly detection, clustering, and every chart spec stay in the sandbox, which reads the same Parquet files. The split is *set operations → DuckDB, statistics and visuals → Python*, and Task 11's planner is told exactly that.

**Interfaces:**
- Consumes: `ExtractStore` (Task 1).
- Produces:
  ```python
  WORKSPACE_QUERY_TIMEOUT_S = 30.0
  WORKSPACE_RESULT_ROW_CAP  = 100_000     # what a local query may return into memory

  @dataclass
  class WorkspaceResult:
      ok: bool
      data: Optional[pd.DataFrame] = None
      error: str = ""
      row_count: int = 0
      truncated: bool = False
      sql: str = ""

  class AnalyticalWorkspace:
      def __init__(self, store: ExtractStore) -> None: ...
      def connect(self, tenant_id: str, conversation_id: str) -> "duckdb.DuckDBPyConnection": ...
      def register(self, tenant_id, conversation_id, label: str) -> bool: ...   # CREATE VIEW over the parquet
      def views(self, tenant_id, conversation_id) -> List[str]: ...
      def query(self, tenant_id, conversation_id, sql: str) -> WorkspaceResult: ...
      def parquet_paths(self, tenant_id, conversation_id) -> Dict[str, str]: ...  # label -> path, for the sandbox
      def close(self, tenant_id, conversation_id) -> None: ...
  ```

**Implementation notes the implementer must not improvise around:**
- **One in-memory connection per (tenant, conversation)**, keyed in a dict, created lazily. In-memory — the Parquet files are the durable state, the DuckDB database is not. Nothing is persisted to `workspace.duckdb`; a lost connection is rebuilt by re-registering the extracts.
- `register` issues `CREATE OR REPLACE VIEW "<label>" AS SELECT * FROM read_parquet(?)` with the path **as a bound parameter**, never string-interpolated. Validate `label` against the same `SAFE_ID` regex `ExtractStore` uses before it reaches an identifier position.
- `connect` calls `duckdb.connect(":memory:")` then, immediately, `SET enable_external_access=false` and `SET autoinstall_known_extensions=false; SET autoload_known_extensions=false`. Then re-register every label `store.list_metas(...)` reports, so a cold process rebuilds the workspace from disk. **Do this before returning the connection** — a query against a fresh connection must not silently see zero views.
- `query` runs the SQL through the **existing `QueryPolicy`** with `dialect="duckdb"` before executing — same read-only/statement checks every warehouse query gets. Then `LIMIT`-guard the result: fetch `WORKSPACE_RESULT_ROW_CAP + 1` rows and set `truncated` when the extra row appears. Wrap execution in try/except and return `ok=False` with the DuckDB message; a bad local query is a repairable LLM error, not a 500.
- Enforce `WORKSPACE_QUERY_TIMEOUT_S` by running the query on a worker thread and abandoning the connection on timeout (`duckdb` releases the GIL during execution). Do not add a second process.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_workspace.py
def test_a_registered_extract_is_queryable_as_a_view(tmp_path):
    store = ExtractStore(str(tmp_path))
    store.put("acme", "c1", _meta("df_1"), pd.DataFrame({"session_id": ["a","b"], "revenue": [1,2]}))
    ws = AnalyticalWorkspace(store)
    ws.register("acme", "c1", "df_1")
    res = ws.query("acme", "c1", "SELECT SUM(revenue) AS total FROM df_1")
    assert res.ok and res.data["total"][0] == 3

def test_two_extracts_can_be_joined_locally(tmp_path):
    ...put df_1 (session_id, revenue) and df_2 (session_id, device)...
    res = ws.query("acme", "c1", "SELECT d.device, SUM(f.revenue) AS r FROM df_1 f "
                                 "JOIN df_2 d USING (session_id) GROUP BY d.device")
    assert res.ok and set(res.data["device"]) == {"android", "ios"}

def test_a_cold_workspace_rebuilds_its_views_from_disk(tmp_path):
    """The whole point of durability: a fresh process must find the extracts."""
    store = ExtractStore(str(tmp_path))
    store.put("acme", "c1", _meta("df_1"), pd.DataFrame({"session_id": ["a"]}))
    ws2 = AnalyticalWorkspace(ExtractStore(str(tmp_path)))          # never saw the put
    assert ws2.query("acme", "c1", "SELECT COUNT(*) AS n FROM df_1").data["n"][0] == 1

def test_tenants_cannot_see_each_others_views(tmp_path):
    ...acme puts df_1; globex has none...
    res = ws.query("globex", "c1", "SELECT * FROM df_1")
    assert not res.ok and "df_1" in res.error

def test_a_write_statement_is_rejected_by_policy(tmp_path):
    assert not ws.query("acme", "c1", "DROP VIEW df_1").ok

def test_external_access_is_disabled(tmp_path):
    res = ws.query("acme", "c1", "SELECT * FROM read_csv_auto('https://example.com/x.csv')")
    assert not res.ok

def test_a_broken_query_returns_an_error_not_a_raise(tmp_path):
    res = ws.query("acme", "c1", "SELECT nope FROM df_1")
    assert not res.ok and res.error

def test_a_huge_result_is_truncated_and_says_so(tmp_path):
    ...extract with WORKSPACE_RESULT_ROW_CAP + 10 rows, SELECT *...
    assert res.truncated and res.row_count == WORKSPACE_RESULT_ROW_CAP

def test_parquet_paths_are_exposed_for_the_sandbox(tmp_path):
    assert ws.parquet_paths("acme", "c1")["df_1"].endswith("df_1.parquet")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_workspace.py -q`
Expected: FAIL — `ModuleNotFoundError: analytics_platform.execution.workspace`

- [ ] **Step 3: Implement**

```bash
.venv/bin/pip install duckdb && .venv/bin/pip show duckdb | grep -i version
```

Pin the installed version into `requirements.txt` in the same commit. Check `execution/policy.py` for how dialects are keyed before adding `"duckdb"` — if the policy's dialect handling is a closed set, extend it there rather than special-casing in `workspace.py`.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/execution/workspace.py requirements.txt tests/test_workspace.py && git commit -m "feat(execution): DuckDB analytical workspace over the Parquet extract cache"
```

---

### Task 10: `DataManager` — decide reuse vs. retrieve in code, not in a prompt

**Files:**
- Create: `analytics_platform/data_manager.py`
- Test: Create `tests/test_data_manager.py`

**Why this is not the LLM's job.** "Does the workspace already contain August, Germany, over the checkout-sessions population, cut by device?" is set containment with a right answer. The LLM's job is to *state the requirement*; the deterministic part is deciding whether it is met. Today `_choose_compute_path` asks the model to eyeball a list of column names, which is why a follow-up that was fully answerable from cached data went back to the warehouse. Every avoided warehouse query is an avoided round trip through a human's browser tab.

**What changed now that data arrives as cubes.** The containment rule this task was always built around — *finer stored grain is reusable, coarser is not* — is unchanged in spirit and moves up one level: from ID grain to **cube dimensions**. A cube grouped by `country × device × service_line` answers any question over a **subset** of those dimensions by summing over the rest; it cannot answer a question about a dimension it does not carry. Two new gates sit around that rule, and both are load-bearing.

**Interfaces:**
- Consumes: `ExtractMeta` (Task 1), `ConversationDataCache.list_available` (Task 2), `AnalyticalWorkspace` (Task 9), `CubeSQL.population_hash` / `.non_additive` (Task 7).
- Produces:
  ```python
  @dataclass
  class DataRequirement:
      base_view: str                         # "checkout_sessions" -- which population
      population_hash: str                   # from Task 7; the reconciliation key
      grain: List[str]                       # the base's ID grain, e.g. ["session_id"]
      dimensions: List[str]                  # ["country", "device"] -- the cube's GROUP BY
      measures: List["CubeMeasure"]          # carries `additive` per measure
      filters: Dict[str, List[str]]          # the SLICE: {"country": ["Germany"]}
      time_column: str = ""                  # "order_date"
      time_start: str = ""                   # ISO date, inclusive
      time_end: str = ""                     # ISO date, inclusive

  @dataclass
  class CoverageVerdict:
      decision: str                # "reuse" | "widen" | "retrieve"
      label: str = ""              # the cube to reuse or widen, when there is one
      missing_dimensions: List[str] = field(default_factory=list)
      missing_measures: List[str] = field(default_factory=list)
      missing_time_ranges: List[Tuple[str, str]] = field(default_factory=list)
      supersedes: str = ""         # on "widen": the narrower cube this one replaces
      reason: str = ""             # human-readable, goes into the answer's provenance

  class DataManager:
      def __init__(self, cache, workspace, settings) -> None: ...
      def assess(self, tenant_id, conversation_id, req: DataRequirement) -> CoverageVerdict: ...
  ```

**The decision rules, in order — implement exactly these, no LLM anywhere in this file:**

0. **Same population, or nothing.** A candidate is only a candidate when `meta.population_hash == req.population_hash`. A cube over a different population is not "close enough to reuse with a caveat" — reusing it is exactly the silent cross-population comparison this whole design exists to prevent. Mismatch → the candidate is discarded, with no partial credit. If no candidate survives this gate, the verdict is `retrieve`.
1. **Dimension containment.** `set(req.dimensions) ⊆ set(meta.dimensions)` → reusable, by summing over the surplus dimensions locally. A dimension the cube does not carry cannot be recovered from it; those go into `missing_dimensions`. (For a keyset-paginated ID-grain extract, the same test applies to `grain` instead, exactly as before: `set(req.grain) ⊆ set(meta.grain)`.)
2. **Additivity gate — the rule that makes rule 1 safe.** Rolling a cube up to fewer dimensions is only valid when every measure the requirement asks for is **additive in that cube**. `COUNT(DISTINCT)`, medians, and percentiles do not sum across cells, so a cube carrying them answers *only at its own dimension set*. If `req.dimensions` is a strict subset of `meta.dimensions` and any needed measure appears in `meta.non_additive` → `retrieve`, with a `reason` that names the measure. **`AVG` must never appear here**: Task 7 stores it as a sum and a count, so it arrives additive; an `AVG` column in a cube manifest is a Task 7 bug, and this file should log it loudly rather than quietly rolling it up.
3. **Measures present.** Every `req.measures` name (or its `read_expr` inputs — `revenue_sum` and `revenue_count` for an averaged `revenue`) must appear in `meta.columns`. Whatever is absent goes into `missing_measures`.
4. **Slice satisfiable.** A *narrower* slice than the one the cube was built with is fine — the surplus rows get filtered locally. A *wider* one is not: those rows were never fetched. Record the cube's own slice filters in `ExtractMeta` (Task 1 gains `filters: Dict[str, List[str]]`) so this is checkable rather than guessed. A filter on a column the cube does not carry is also a miss — you cannot filter on what you did not group by.
5. **Time covered.** With `req.time_start/end` set and `meta` carrying the cube's own range, coverage requires `meta.time_start <= req.time_start and req.time_end <= meta.time_end`. Anything outside becomes a `missing_time_ranges` entry.
6. **Never reuse a truncated cube for a population question.** `meta.truncated` means cells were dropped at the ceiling; totals, counts, and rates over the whole population are then wrong. Truncated cubes are reusable only when the requirement's slice is *strictly narrower* — otherwise `retrieve`.
7. **Verdict.** All checks pass → `reuse`. Same population, and the **only** gaps are missing dimensions or missing measures → `widen`: re-run the cube over the same base with the union of old and new dimensions. Anything else → `retrieve`.
8. **A widened cube supersedes the narrower one.** Because both share a `population_hash` and the wider one sums down to the narrower, the wider cube reconciles with every answer already given from the narrower — no prior number is invalidated. Set `supersedes` to the narrower label so Task 14 can record it, and prefer the wider cube in later assessments. Keep both on disk; the retention sweep handles them.
9. **Prefer the smallest sufficient cube.** When several qualify, pick the one with the fewest rows; ties break on most recent. Fewest rows means fewest cells to scan locally, and — because they share a population — every one of them gives the same answer.

`reason` is written for a human and lands in the turn's provenance: `"reused df_1 (checkout_sessions, country x device x date, 2026-08-01..2026-08-31, 41,203 cells) -- device rolls up from it"`, or `"df_1 covers this population but not service_line; widening to country x device x service_line"`, or `"df_1 carries unique_users, which is a distinct count and cannot be rolled up to country alone -- re-querying"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_data_manager.py
SUM_REV = CubeMeasure("revenue", "SUM(revenue)", additive=True)
DISTINCT_USERS = CubeMeasure("unique_users", "COUNT(DISTINCT user_id)", additive=False)

def _req(dimensions=("country",), measures=(SUM_REV,), pop="pop_A", **kw):
    return DataRequirement(base_view="checkout_sessions", population_hash=pop,
                           grain=["session_id"], dimensions=list(dimensions),
                           measures=list(measures), filters=kw.pop("filters", {}), **kw)

# --- gate 0: the population --------------------------------------------------

def test_a_cube_over_a_different_population_is_never_reused(dm, cached):
    """The gate the whole design rests on. Close-but-different is not reusable."""
    cached("df_1", pop="pop_B", dimensions=["country", "device"], columns=["country","device","revenue"])
    v = dm.assess("acme", "c1", _req(pop="pop_A"))
    assert v.decision == "retrieve" and v.label == ""
    assert "population" in v.reason.lower()

def test_the_same_population_with_a_different_projection_still_reuses(dm, cached):
    """Question B added a column at some point; the rows never changed."""
    cached("df_1", pop="pop_A", dimensions=["country", "device", "service_line"],
           columns=["country","device","service_line","revenue"])
    assert dm.assess("acme", "c1", _req(dimensions=["country"])).decision == "reuse"

# --- containment, one level up from grain ------------------------------------

def test_a_subset_of_the_cubes_dimensions_rolls_up(dm, cached):
    cached("df_1", pop="pop_A", dimensions=["country", "device", "date"],
           columns=["country","device","date","revenue"], time=("2026-08-01","2026-08-31"))
    v = dm.assess("acme", "c1", _req(dimensions=["country"], time_column="date",
                                     time_start="2026-08-05", time_end="2026-08-10"))
    assert v.decision == "reuse" and v.label == "df_1"

def test_a_dimension_the_cube_does_not_carry_asks_to_widen(dm, cached):
    cached("df_1", pop="pop_A", dimensions=["country"], columns=["country","revenue"])
    v = dm.assess("acme", "c1", _req(dimensions=["country", "device"]))
    assert v.decision == "widen" and v.missing_dimensions == ["device"]
    assert v.supersedes == "df_1"

def test_a_finer_stored_grain_is_still_reusable_for_a_keyset_extract(dm, cached):
    """The original grain rule survives unchanged for ID-grain extracts."""
    cached("df_1", pop="pop_A", grain=["session_id","event_id"],
           columns=["session_id","event_id","revenue"], dimensions=[])
    assert dm.assess("acme", "c1", _req(dimensions=[])).decision == "reuse"

def test_a_coarser_stored_grain_is_never_reused(dm, cached):
    cached("df_1", pop="pop_A", grain=["country"], columns=["country","revenue"], dimensions=[])
    assert dm.assess("acme", "c1", _req(dimensions=[])).decision == "retrieve"

# --- additivity: the rule that makes containment safe ------------------------

def test_a_distinct_count_cannot_be_rolled_up_to_fewer_dimensions(dm, cached):
    cached("df_1", pop="pop_A", dimensions=["country", "device"],
           columns=["country","device","unique_users"], non_additive=["unique_users"])
    v = dm.assess("acme", "c1", _req(dimensions=["country"], measures=[DISTINCT_USERS]))
    assert v.decision == "retrieve"
    assert "unique_users" in v.reason and "distinct" in v.reason.lower()

def test_a_distinct_count_at_the_cubes_own_dimensions_is_fine(dm, cached):
    """No roll-up is happening, so non-additivity does not bite."""
    cached("df_1", pop="pop_A", dimensions=["country", "device"],
           columns=["country","device","unique_users"], non_additive=["unique_users"])
    v = dm.assess("acme", "c1", _req(dimensions=["country", "device"], measures=[DISTINCT_USERS]))
    assert v.decision == "reuse"

def test_an_averaged_measure_reuses_via_its_sum_and_count(dm, cached):
    cached("df_1", pop="pop_A", dimensions=["country", "device"],
           columns=["country","device","revenue_sum","revenue_count"])
    avg = CubeMeasure("revenue", "AVG(revenue)", additive=True,
                      read_expr="revenue_sum / NULLIF(revenue_count, 0)")
    assert dm.assess("acme", "c1", _req(dimensions=["country"], measures=[avg])).decision == "reuse"

# --- slice, time, truncation -------------------------------------------------

def test_a_wider_slice_than_the_cube_forces_a_retrieve(dm, cached):
    cached("df_1", pop="pop_A", dimensions=["country"], columns=["country","revenue"],
           filters={"country": ["Germany"]})
    assert dm.assess("acme", "c1", _req(filters={})).decision == "retrieve"   # all countries

def test_a_narrower_slice_reuses(dm, cached):
    cached("df_1", pop="pop_A", dimensions=["country"], columns=["country","revenue"])
    assert dm.assess("acme", "c1", _req(filters={"country": ["Germany"]})).decision == "reuse"

def test_a_filter_on_a_dimension_the_cube_lacks_is_a_miss(dm, cached):
    """You cannot filter on what you did not GROUP BY."""
    cached("df_1", pop="pop_A", dimensions=["country"], columns=["country","revenue"])
    v = dm.assess("acme", "c1", _req(filters={"device": ["ios"]}))
    assert v.decision == "widen" and "device" in v.missing_dimensions

def test_a_date_range_beyond_the_cube_asks_to_retrieve(dm, cached):
    cached("df_1", pop="pop_A", dimensions=["date"], columns=["date","revenue"],
           time=("2026-08-01","2026-08-31"))
    v = dm.assess("acme", "c1", _req(dimensions=["date"], time_column="date",
                                     time_start="2026-07-01", time_end="2026-08-31"))
    assert v.missing_time_ranges == [("2026-07-01","2026-07-31")]

def test_a_truncated_cube_is_not_reused_for_a_population_question(dm, cached):
    cached("df_1", pop="pop_A", dimensions=["country"], columns=["country","revenue"], truncated=True)
    assert dm.assess("acme", "c1", _req()).decision == "retrieve"

# --- selection ---------------------------------------------------------------

def test_the_smallest_sufficient_cube_wins(dm, cached):
    cached("df_1", pop="pop_A", dimensions=["country","device"], columns=["country","device","revenue"], rows=90_000)
    cached("df_2", pop="pop_A", dimensions=["country","device"], columns=["country","device","revenue"], rows=4_000)
    assert dm.assess("acme", "c1", _req()).label == "df_2"

def test_a_wider_cube_supersedes_and_is_preferred_afterwards(dm, cached):
    cached("df_1", pop="pop_A", dimensions=["country"], columns=["country","revenue"], rows=30)
    cached("df_2", pop="pop_A", dimensions=["country","device"], columns=["country","device","revenue"], rows=120)
    v = dm.assess("acme", "c1", _req(dimensions=["country"]))
    assert v.decision == "reuse" and v.label == "df_2"      # wider wins over smaller-but-narrower

def test_the_reason_names_the_cube_and_why(dm, cached):
    cached("df_1", pop="pop_A", dimensions=["country"], columns=["country","revenue"])
    assert "df_1" in dm.assess("acme", "c1", _req()).reason
```

> **Note on `test_a_wider_cube_supersedes_and_is_preferred_afterwards` vs. `test_the_smallest_sufficient_cube_wins`:** these two rules can pull in opposite directions, and rule 8 wins — a superseding cube is preferred even when a smaller sufficient one exists, because keeping answers on the widest available cube is what keeps later follow-ups local. Rule 9 breaks ties only among cubes that are not in a supersedes relationship. Implement the preference in that order; both tests must pass.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_data_manager.py -q`
Expected: FAIL — `ModuleNotFoundError: analytics_platform.data_manager`

- [ ] **Step 3: Implement**

`ExtractMeta` (Task 1) gains `base_view: str`, `population_hash: str`, `projection_hash: str`, `dimensions: List[str]`, `non_additive: List[str]`, `filters: Dict[str, List[str]]`, `time_column: str`, `time_start: str`, `time_end: str`. Populate them in Task 14 from the `TurnPlan` and the `CubeSQL`, and compute `time_start`/`time_end` from the returned frame itself (`df[time_column].min()/.max()`) rather than trusting the plan — what the SQL *asked* for and what came back are not always the same.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/data_manager.py analytics_platform/execution/extract_store.py tests/test_data_manager.py && git commit -m "feat(workspace): deterministic reuse/widen/retrieve coverage over a fixed population"
```

---

### Task 11: `_plan_turn` — choose the base view, state the cube

**Files:**
- Modify: `analytics_platform/stakeholder.py:550-593` (replace `_choose_compute_path`)
- Test: `tests/test_stakeholder.py` (extend)

**This is the change that fixes the observed behaviour.** Today's router only ever picks between `python` and `sql`, sees nothing but column names, and short-circuits to `"sql"` when the cache is empty. It is replaced by a planning call that does the three things only an LLM can do — **pick the population**, **state the cube it needs over that population**, and **choose how to compute** — and hands everything else to code.

**The division of labour, which the implementer must not blur:**

| Decision | Owner |
|---|---|
| Which population does this question live in? (`base_view`) | **LLM** — `_plan_turn`, choosing from approved views, or proposing one |
| What cut of it does the question need? (dimensions, measures, slice, dates) | **LLM** — `_plan_turn` |
| Is the base valid, and what is its `population_hash`? | **`BaseViewRegistry`** (Task 7), deterministic |
| Will that cube fit? | **`compose_cube`'s cell guard** (Task 7), deterministic |
| Is it already in the workspace? | **`DataManager`** (Task 10), deterministic |
| How should the analysis be computed once the data is in hand? | **LLM** — `analysis: "workspace_sql" \| "python"` |
| Is the base actually at the grain it claims? | **the grain probe against the base** (Task 13), deterministic, once per `population_hash` |

**Interfaces:**
- Consumes: `SchemaContext` (Task 8 — semantics, base views, schema, and the flattened `profiles`), `BaseViewRegistry.get` / `.compose_cube` (Task 7), `DataManager.assess` (Task 10), `ConversationDataCache.list_available` (Task 2, carrying `dimensions`, `population_hash`, `truncated`, `sample`).
- Produces:
  ```python
  @dataclass
  class TurnPlan:
      path: str            # "reuse" | "widen" | "retrieve" | "aggregate" -- set from the verdict
      analysis: str = "python"    # "workspace_sql" | "python" -- how to compute, LLM's choice
      df_label: str = ""   # the cube to compute over, from the verdict
      base_view: Optional["BaseView"] = None            # the population, resolved from the name
      base_view_approved: bool = False                  # False -> the answer is provisional
      cube: Optional["CubeSpec"] = None                 # what the LLM asked for
      cube_sql: Optional["CubeSQL"] = None              # composed + hashed + guarded (Task 7)
      requirement: Optional["DataRequirement"] = None   # what goes to the DataManager
      verdict: Optional["CoverageVerdict"] = None       # what the DataManager decided
      grain: List[str] = field(default_factory=list)    # the base's ID grain
      dimensions: List[str] = field(default_factory=list)
      measures: List["CubeMeasure"] = field(default_factory=list)
      time_window: str = ""
      rationale: str = ""
      # Proposed only when the planner is authoring a NEW base view this turn;
      # on an existing base these are already baked in and inherited (Task 7).
      attributions: List["AttributionRule"] = field(default_factory=list)

  def _plan_turn(self, llm, tenant_id: str, conversation_id: str, question: str,
                 query_nodes: List[Any], defn_nodes: List[Any],
                 schema_ctx: Optional[SchemaContext] = None) -> TurnPlan: ...
  ```
  Put `TurnPlan` in `analytics_platform/domain.py` beside the other shared dataclasses, not in `stakeholder.py`. `AttributionRule`, `CubeSpec`, `CubeMeasure`, and `CubeSQL` are already there from Task 7 — do not redefine them.

**The planner is schema-aware too, not just the SQL writer.** `schema_ctx.rendered` goes into the planning prompt, because choosing dimensions is a *schema* decision: the LLM cannot judge whether `country × device × city` fits without knowing each column's `distinct_count`. The `[identifier]` tags and cardinalities are precisely what makes this choice grounded rather than a guess.

**Attribution is no longer a per-question decision, and that is a deliberate reduction in the planner's job.** On an existing base view the collapse rules are already inside the population and inherited automatically — the planner neither restates them nor may override them, because two questions applying two rankings to the same sessions is exactly the failure Task 7 exists to close. The planner emits `attributions` **only** when it is proposing a new base view, and even then they are stored as DRAFT for review. Prompt text for that case:

```
You are proposing a NEW base view, so you must also say how each multi-valued
column collapses onto the grain. The schema marks these FAN-OUT. Do NOT add such
a column to GROUP BY -- that changes the grain and double-counts those keys.

Prefer strategy "highest_intent": rank the column's values by business value and
take the highest-ranked value the key touched. Approved attribution rules for
this company are listed above -- reuse one verbatim when it covers the column,
and set source="brain". Only propose your own (source="llm") when none applies,
and explain the ranking in rationale.

Fall back to "most_frequent" (with a latest-timestamp tiebreak) only when no
value ordering is defensible. Never use "first" or "latest" alone for a column
that drives conversion or revenue -- that misattributes exactly the multi-value
keys that matter most.
```

**Prompt requirements (write these verbatim into the system prompt):**
- Strict JSON only:
  ```json
  {"base_view": "checkout_sessions",
   "propose_base_view": null,
   "cube": {"dimensions": [], "measures": [{"name": "", "expr": ""}],
            "filters": {}, "time_column": "", "time_start": "", "time_end": ""},
   "analysis": "workspace_sql" | "python",
   "aggregate_only": false,
   "attributions": [],
   "rationale": ""}
  ```
- **Name exactly one `base_view` from the list above.** Every number in your answer comes from that one population, which is what lets this answer be compared against earlier ones. Prefer an `[APPROVED]` view over a `[DRAFT]` one.
- **`propose_base_view`** — fill this in **only** when no listed view can answer the question, and set `base_view` to the name you are proposing. It must be at **ID grain**: one row per identifier (`session_id`, `order_id`, `user_id`), never one row per dimension combination. A base at dimensional grain is useless for the next question. Say in `rationale` why no existing view fits.
- **`cube.dimensions`** — the columns to `GROUP BY`, drawn only from the chosen base's listed dimension columns. Fewer is better: a cube is reusable for every question over a *subset* of its dimensions, and cheap to widen later, but a cube that is too large is refused outright. Do not add a dimension "in case it is useful."
- **`cube.measures`** — prefer `SUM`, `COUNT(*)`, `MIN`, `MAX`. Ask for `AVG(x)` as a plain `AVG(x)` and it will be stored as a sum and a count for you. `COUNT(DISTINCT x)`, medians, and percentiles **do not roll up** — a cube carrying one can answer only at its own dimensions, so name them only when the question truly needs them, and say so in `rationale`.
- **`cube.filters`** are equality sets only (`{"country": ["Germany"]}`), taken from the **exact literals and casing** in the schema block. These slice the population; they do not change it. A metric's `ALWAYS APPLY` filters belong in the base view, not here — if a matched metric has one and the chosen base does not enforce it, say so in `rationale` rather than patching it in as a slice.
- `aggregate_only: true` means no base view applies and none is worth proposing — a one-off scalar, an operational lookup. Justify it in `rationale`. **The answer will be marked as unreconcilable**, so this is the exception, not the default.
- `analysis` — **how to compute the answer once the data is in hand**:
  - `"workspace_sql"` for set operations: filtering, grouping, joining two cubes, aggregating, ranking, windows. This runs as DuckDB SQL against the cubes as views and is the cheaper, more reliable path — **prefer it** for any re-cut.
  - `"python"` for statistics, trend decomposition, significance tests, anomaly detection, forecasting, clustering, correlation, and **any turn that should produce a chart** — the chart spec is built in Python.
- The cubes listed below come with `base_view`, `dimensions`, `row_count`, `truncated`, and a 3-row `sample`. They are context for *stating the requirement well*, not a menu to pick from — code decides what gets reused.

**Behaviour:**
- **Always call the LLM**, cached cubes or not (this is the key difference from today's early return).
- Resolve `base_view` through `BaseViewRegistry.get`. Unknown name → treat as a parse failure. `propose_base_view` present → `upsert` it at DRAFT, use it, and set `base_view_approved = False`. An existing DRAFT view resolves the same way. Approved → `base_view_approved = True`.
- Call `compose_cube(view, spec, schema_ctx.profiles)`. **When the guard refuses, re-prompt once** with `cube_sql.error` and `cube_sql.offending_dimensions` fed back verbatim: *"that cube would produce ~N cells, over the limit; the largest dimensions are X, Y — drop or bucket one and try again."* A cube the LLM cannot shrink on the retry falls back to `aggregate_only`. Do not silently drop a dimension on the model's behalf: the answer would then be to a question nobody asked.
- Build the `DataRequirement` from the composed cube — `population_hash` comes from `cube_sql`, never from the LLM — then call `DataManager.assess(...)` and set `plan.path` from `verdict.decision`, `plan.df_label` from `verdict.label`, `plan.verdict` from the whole verdict. `aggregate_only: true` overrides to `path="aggregate"`.
- Default on any parse failure or LLM error: `TurnPlan(path="aggregate", analysis="python")` with no base view. **Note this is a change from the earlier draft of this plan, which defaulted to `retrieve`:** without a resolved base view there is no population to retrieve *over*, and inventing one from a malformed plan is how an unreconcilable number gets produced silently. Falling back to the plain aggregate path — which is today's behaviour, explicitly marked unreconcilable — is the honest degradation. Log the parse failure with the raw text at WARNING.
- `verdict.reason` is carried into the turn's provenance verbatim. A reader must be able to see *why* no query ran.

- [ ] **Step 1: Write the failing tests**

Use the existing mock-LLM sequencing pattern in `tests/test_stakeholder.py`. **Critical:** `classify()` is a pure keyword heuristic with no LLM call, and the *first* `llm.generate()` in `answer()` is `_extract_search_intent`. Count mock responses from there.

```python
CUBE = ('{"base_view":"checkout_sessions","cube":{"dimensions":["device"],'
        '"measures":[{"name":"revenue","expr":"SUM(revenue)"}],"filters":{}},'
        '"analysis":"workspace_sql"}')

def test_the_verdict_not_the_llm_sets_the_path(stakeholder, tmp_cache, approved_base_view):
    """The LLM names the population and the cut; DataManager decides it is covered."""
    tmp_cache.put("acme", "c1", "df_1", "sessions by device and country",
                  pd.DataFrame({"device": ["ios"], "country": ["DE"], "revenue": [1]}),
                  meta=_meta("df_1", dimensions=["device", "country"],
                             population_hash=approved_base_view.population_hash))
    plan = stakeholder._plan_turn(MockLLM([CUBE]), "acme", "c1",
                                  "break that down by device", [], [])
    assert plan.path == "reuse" and plan.df_label == "df_1"
    assert plan.analysis == "workspace_sql"

def test_the_population_hash_comes_from_the_base_not_the_llm(stakeholder, approved_base_view):
    """An LLM-supplied hash would let a bad plan claim reconcilability it does not have."""
    plan = stakeholder._plan_turn(MockLLM([CUBE]), "acme", "c1", "q", [], [])
    assert plan.requirement.population_hash == approved_base_view.population_hash

def test_a_cube_the_workspace_cannot_cover_becomes_retrieve(stakeholder, approved_base_view):
    llm = MockLLM([CUBE])
    plan = stakeholder._plan_turn(llm, "acme", "c1", "how did revenue trend?", [], [])
    assert plan.path == "retrieve" and plan.grain == ["session_id"]
    assert llm.calls == 1          # regression guard: today this returns without calling

def test_an_unknown_base_view_name_is_a_parse_failure(stakeholder, approved_base_view):
    plan = stakeholder._plan_turn(MockLLM(['{"base_view":"nope","cube":{"dimensions":[]}}']),
                                  "acme", "c1", "q", [], [])
    assert plan.path == "aggregate" and plan.base_view is None

def test_a_proposed_base_view_is_stored_as_draft_and_marked_provisional(stakeholder, registry):
    llm = MockLLM(['{"base_view":"guest_checkouts","propose_base_view":'
                   '{"name":"guest_checkouts","grain":["guest_id"],'
                   '"source_sql":"SELECT guest_id FROM guests","dimension_columns":["country"],'
                   '"measure_columns":["revenue"]},'
                   '"cube":{"dimensions":["country"],"measures":[]}}'])
    plan = stakeholder._plan_turn(llm, "acme", "c1", "guest revenue by country", [], [])
    assert plan.base_view.name == "guest_checkouts"
    assert plan.base_view_approved is False
    assert registry.get("acme", "guest_checkouts", approved_only=False) is not None
    assert registry.get("acme", "guest_checkouts") is None        # not approved

def test_a_cube_over_the_cell_limit_is_re_prompted_with_the_culprit(stakeholder, approved_base_view):
    """The guard refuses; the LLM is told which dimension is the problem, not just 'no'."""
    llm = RecordingLLM([
        '{"base_view":"checkout_sessions","cube":{"dimensions":["country","device","city"],'
        '"measures":[{"name":"n","expr":"COUNT(*)"}]}}',
        '{"base_view":"checkout_sessions","cube":{"dimensions":["country","device"],'
        '"measures":[{"name":"n","expr":"COUNT(*)"}]}}',
    ])
    plan = stakeholder._plan_turn(llm, "acme", "c1", "q", [], [], schema_ctx=schema_ctx_wide)
    assert llm.calls == 2
    assert "city" in llm.prompts[1]
    assert plan.cube.dimensions == ["country", "device"] and plan.cube_sql.ok

def test_a_cube_that_cannot_be_shrunk_falls_back_to_aggregate(stakeholder, approved_base_view):
    llm = MockLLM([TOO_WIDE, TOO_WIDE])
    assert stakeholder._plan_turn(llm, "acme", "c1", "q", [], [],
                                  schema_ctx=schema_ctx_wide).path == "aggregate"

def test_aggregate_only_overrides_the_verdict(stakeholder, approved_base_view):
    llm = MockLLM(['{"base_view":"checkout_sessions","cube":{"dimensions":[],'
                   '"measures":[{"name":"revenue","expr":"SUM(revenue)"}]},'
                   '"aggregate_only":true,"analysis":"python"}'])
    assert stakeholder._plan_turn(llm, "acme", "c1", "total revenue?", [], []).path == "aggregate"

def test_plan_turn_falls_back_to_aggregate_on_garbage(stakeholder):
    """Deliberately NOT retrieve: with no resolved base there is no population to
    retrieve over, and inventing one produces an unreconcilable number silently."""
    p = stakeholder._plan_turn(MockLLM(["not json at all"]), "acme", "c1", "q", [], [])
    assert p.path == "aggregate" and p.base_view is None

def test_the_verdict_reason_survives_onto_the_plan(stakeholder, tmp_cache):
    ...cached df_1 that covers the requirement...
    assert "df_1" in stakeholder._plan_turn(llm, "acme", "c1", "q", [], []).verdict.reason

def test_plan_turn_prompt_carries_all_three_context_blocks(stakeholder, schema_ctx):
    llm = RecordingLLM([CUBE])
    stakeholder._plan_turn(llm, "acme", "c1", "q", [], [], schema_ctx=schema_ctx)
    prompt = llm.last_system_prompt + llm.last_prompt
    assert "BUSINESS SEMANTICS" in prompt and "BASE VIEWS" in prompt and "DATABASE SCHEMA" in prompt

def test_avg_is_accepted_and_arrives_additive(stakeholder, approved_base_view):
    llm = MockLLM(['{"base_view":"checkout_sessions","cube":{"dimensions":["country"],'
                   '"measures":[{"name":"revenue","expr":"AVG(revenue)"}]}}'])
    plan = stakeholder._plan_turn(llm, "acme", "c1", "average revenue by country", [], [])
    assert plan.cube_sql.non_additive == []
    assert "revenue_sum" in plan.cube_sql.sql and "AVG(" not in plan.cube_sql.sql

def test_a_distinct_count_is_flagged_non_additive_on_the_plan(stakeholder, approved_base_view):
    llm = MockLLM(['{"base_view":"checkout_sessions","cube":{"dimensions":["country"],'
                   '"measures":[{"name":"users","expr":"COUNT(DISTINCT user_id)"}]}}'])
    assert stakeholder._plan_turn(llm, "acme", "c1", "q", [], []).cube_sql.non_additive == ["users"]

def test_attribution_on_an_existing_base_is_inherited_not_restated(stakeholder, approved_base_view):
    """The base already collapses service_line. The planner must not re-decide it,
    and an attributions array it emits anyway must be ignored."""
    llm = MockLLM(['{"base_view":"checkout_sessions","cube":{"dimensions":["service_line"],'
                   '"measures":[]},"attributions":[{"column":"service_line",'
                   '"grain":["session_id"],"strategy":"latest"}]}'])
    plan = stakeholder._plan_turn(llm, "acme", "c1", "q", [], [])
    assert plan.attributions == []                                    # ignored
    assert plan.base_view.attributions[0].strategy == "highest_intent"  # the base's, inherited

def test_a_proposed_base_view_may_carry_attribution_rules(stakeholder, schema_ctx):
    llm = MockLLM(['''{"base_view":"events_by_session","propose_base_view":{
        "name":"events_by_session","grain":["session_id"],
        "source_sql":"SELECT session_id FROM events","dimension_columns":["service_line"],
        "measure_columns":[]},
        "cube":{"dimensions":["service_line"],"measures":[]},
        "attributions":[{"column":"service_line","grain":["session_id"],
                         "strategy":"highest_intent",
                         "priority_values":["mobile","fixed","ott"],
                         "tiebreakers":["event_count DESC","log_time DESC"],
                         "source":"brain"}]}'''])
    plan = stakeholder._plan_turn(llm, "acme", "c1", "q", [], [], schema_ctx=schema_ctx)
    assert plan.base_view.attributions[0].priority_values == ["mobile", "fixed", "ott"]

def test_a_proposed_base_with_a_fanned_out_column_and_no_rule_gets_a_default(stakeholder, schema_ctx):
    """service_line has 6% fan-out and the proposal ignored it. Silently carrying it
    onto the grain would double-count -- synthesize a most_frequent rule and mark it."""
    plan = stakeholder._plan_turn(MockLLM([PROPOSAL_WITHOUT_ATTRIBUTION]), "acme", "c1", "q",
                                  [], [], schema_ctx=schema_ctx)
    rule = next(a for a in plan.base_view.attributions if a.column == "service_line")
    assert rule.strategy == "most_frequent" and rule.source == "default"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -q -k plan_turn`
Expected: FAIL — `AttributeError: 'StakeholderService' object has no attribute '_plan_turn'`

- [ ] **Step 3: Implement `_plan_turn`, delete `_choose_compute_path`**

Grep for other references to `_choose_compute_path` before deleting (`grep -rn "_choose_compute_path" analytics_platform tests`) and update or delete its tests in the same commit.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/stakeholder.py analytics_platform/domain.py tests/test_stakeholder.py && git commit -m "feat(stakeholder): requirement-stating turn planner + deterministic coverage verdict"
```

---

### Task 12: Execute the composed cube; keep LLM synthesis only where SQL is still authored

**Files:**
- Modify: `analytics_platform/stakeholder.py:595-643` (`_synthesize_sql`) and `:645-688` (`_synthesize_and_execute_sql`)
- Modify: `analytics_platform/stakeholder.py` — `_plan_turn`'s base-view proposal prompt (from Task 11)
- Test: `tests/test_stakeholder.py` (extend)

**Read this before touching the file: on the cube paths there is no SQL to write.** Task 7's `compose_cube` already returned a complete, hashed, guarded, executable statement in `plan.cube_sql.sql`, with the approved `source_sql` inlined byte for byte. Asking an LLM to re-author it would break the one thing the base exists to guarantee — that two answers over the same population produce the same rows — because a re-emitted base is a *different string* and therefore, correctly, a different `population_hash`. **So `retrieve` and `widen` do not call the synthesis LLM at all.** This task is mostly about executing, paging, and bounding what Task 7 composed; the LLM's remaining SQL-authoring job shrinks to the one path where no base view exists.

**Three paths, three owners:**

| `plan.path` | Who writes the SQL | What this task does |
|---|---|---|
| `retrieve` / `widen` | **`compose_cube`** (Task 7), deterministic | validate through policy, execute, page if needed, retry on the *planner*, never on the SQL writer |
| ID-grain escape (non-additive measure) | **`compose_keyset_chunk`** (Task 7), deterministic | drive the cursor loop, accumulate, stop at the ceiling |
| `aggregate` | **the LLM**, as today | give it `schema_ctx` so it stops inventing columns and literals, and mark the answer unreconcilable |

**Interfaces:**
- Consumes: `TurnPlan` (Task 11), `SchemaContext` (Task 8), `BaseViewRegistry.compose_keyset_chunk` (Task 7), `settings.policy.max_transport_rows` / `.extract_chunk_rows` / `.raw_extract_row_limit` (Task 4).
- Produces, on `StakeholderService`:
  ```python
  def _execute_cube(self, tenant_id: str, plan: TurnPlan,
                    question: str) -> Tuple[str, QueryResult]: ...
  def _fetch_keyset_chunks(self, tenant_id: str, plan: TurnPlan,
                           question: str) -> Tuple[List[str], QueryResult]: ...
  def _render_attribution_pattern(self, rules: List[AttributionRule]) -> str: ...
  ```
  and, unchanged in signature but now schema-aware:
  ```python
  def _synthesize_sql(self, llm, question, query_nodes, defn_nodes,
                      plan: Optional[TurnPlan] = None,
                      schema_ctx: Optional[SchemaContext] = None, ...) -> str: ...
  def _synthesize_and_execute_sql(self, llm, tenant_id, question, query_nodes, defn_nodes,
                                  plan: Optional[TurnPlan] = None,
                                  schema_ctx: Optional[SchemaContext] = None, ...): ...
  ```
  With `plan` and `schema_ctx` both `None`, `_synthesize_sql` behaves byte-for-byte as it does today — that is what keeps every existing test green.

**`_synthesize_and_execute_sql` becomes a dispatcher.** It routes on `plan.path`: `retrieve`/`widen` → `_execute_cube` (or `_fetch_keyset_chunks`), `aggregate` or `plan is None` → today's synthesise-validate-execute-retry loop verbatim. Keep the existing retry loop intact for the aggregate path; it is well-tested and out of scope to change.

**Row limits, and the fact that `MAX_CUBE_CELLS` is bigger than the transport.** `MAX_CUBE_CELLS` (200,000) is the sanity ceiling on whether a cube is worth composing at all; `max_transport_rows` (50,000, and possibly lower after Task 4 Step 3 measures it) is what one round trip can carry. **These are different numbers on purpose, and a cube between them is legal.** So:

- Every round trip is issued with `row_limit=self.settings.policy.max_transport_rows`. Never with `raw_extract_row_limit` — Task 4 makes the policy reject that outright, and it should.
- When `plan.cube_sql.estimated_cells <= max_transport_rows`, one query, done.
- When it exceeds it, the cube is fetched in **keyset pages over its dimension tuple**, `ORDER BY 1, 2, …` with `WHERE (d1, d2, …) > (<last row's values>)`, concatenated locally. Cube cells are disjoint by construction, so concatenation is exact — no dedupe, no re-aggregation.
- The accumulated row count is bounded by `raw_extract_row_limit`. On hitting it, stop paging, set `truncated=True`, and append the warning — never keep going silently.
- **`estimated_cells` is an estimate.** The loop's real stop condition is a short page (`len(rows) < chunk_rows`), not the estimate. A cube that estimated 40,000 and returns 50,000 exactly must page again rather than assume it is complete.

**`widen` is not `extend`, and the difference matters.** The old `extend` path assumed a gap could always be fetched in isolation and joined on. That is true for a **time** gap and false for a **dimension** gap:

- **Missing dimensions** → the whole cube is re-run over the same base with the union of old and new dimensions. Adding `device` re-splits every existing `country` cell; there is no "just the device part" to fetch. The verdict's `supersedes` label (Task 10, rule 8) is what makes this cheap in the long run — the wider cube answers both questions from then on.
- **Missing time ranges only** → compose the same cube with `time_start`/`time_end` set to the missing window and `UNION ALL` it onto the existing cube locally in DuckDB. Cells over disjoint date ranges are disjoint, and every measure that survived Task 10's additivity gate sums across them. Both extracts stay on disk as sibling views.
- **Both** → treat it as a missing-dimension widen over the full requested range. Do not try to be clever.

**When the composed SQL fails in the warehouse, do not hand it to the LLM to fix.** The failure is in one of two places, and they have different owners:

- **An APPROVED base view failed.** Its `source_sql` references a column or table that no longer exists, or Athena rejects it. This is a **governance failure, not a prompt failure**: surface it. Log at ERROR with the view name and the warehouse error, add the caveat *"base view `<name>` no longer executes against the warehouse — it needs review"*, and fall through to `aggregate` for this turn. **Never let an LLM rewrite an approved base's `source_sql`** — the whole review flow exists so a human owns that string, and an answer computed from a silently-patched base carries a `population_hash` that no longer describes the SQL that ran.
- **A DRAFT base proposed on this same turn failed.** The LLM authored that `source_sql` minutes ago and it was never reviewed, so it may repair it: re-prompt `_plan_turn` once with the warehouse error verbatim, `upsert` the corrected draft, recompose, retry. One repair attempt, then `aggregate`.
- **The base is fine but the planner picked a bad dimension or measure name.** Same single re-prompt of the planner, with the error fed back — this is the same re-prompt-once loop Task 11 already implements for the cell guard, reused rather than duplicated.

In all three cases the retry re-prompts the **planner**, never the SQL writer. There is no SQL writer on this path.

**The aggregate path keeps the LLM, and now gets the context block.** Today `prompt` starts at `f"Question: {question}\n\nContext:\n"` and then lists definitions and example queries. Insert `schema_ctx.rendered` immediately after the question and **before** the definitions, and add this to `sys_prompt`:

```
The BUSINESS SEMANTICS, BASE VIEWS, and DATABASE SCHEMA sections below are
authoritative. Semantics describe what a metric MEANS -- its formula, the grain
it is valid at, and the filters that must always be applied. Schema describes
the real tables, the real columns, and the real values in this warehouse. The
Example Queries are historical and may reference columns that no longer exist --
where they disagree, semantics win over schema, and schema wins over the
examples.

Never invent a column name, and never invent a filter literal: use the exact
values listed for that column, with their exact casing. Every filter listed
under ALWAYS APPLY for a metric you are computing must appear in the WHERE
clause, whether or not the user mentioned it.

You are on the fallback path: no base view governs this query, so its result
cannot be reconciled against any other answer. Keep it narrow and answer only
what was asked.
```

That last paragraph is not decoration. Per Global Constraints, a number computed without a `population_hash` is not comparable to anything, and Task 14 turns it into a visible caveat.

**The attribution worked pattern moves to the proposal prompt.** Attribution now lives inside the base view (Task 7), so the only moment an LLM writes attribution SQL is when it is authoring `propose_base_view.source_sql` in `_plan_turn`. `_render_attribution_pattern` is a pure function that renders the shape; this task adds it and wires it into that prompt. Do not describe the technique in prose and hope — render the actual CTE, parameterized on the rule, because the LLM copies structure far more reliably than it follows instructions:

```
The column `{column}` holds more than one value per {grain}. Your base view must
collapse it with a ranked attribution CTE, not GROUP BY. Business ranking,
highest value first: {priority_values}. Resolve ties with {tiebreakers}. Use
exactly this shape:

WITH ranked_{column} AS (
    SELECT {grain}
         , {column}
         , COUNT(*) AS event_count
         , MAX({ts_column}) AS latest_event
    FROM {table}
    WHERE {filters}
    GROUP BY {grain}, {column}
)
, attributed_{column} AS (
    SELECT *
         , ROW_NUMBER() OVER (
               PARTITION BY {grain}
               ORDER BY CASE {column} {priority_case} ELSE 99 END ASC
                      , event_count DESC
                      , latest_event DESC
           ) AS rn
    FROM ranked_{column}
)
-- then join back on {grain} AND rn = 1, exposing {column} as the attributed value

Emit one such CTE per attributed column and chain them: attribute the coarser
level first, then the finer level within it. Every join back is ON the grain key
AND rn = 1 -- the row count of your base view must equal the distinct count of
{grain}. That is what makes it an ID-grain base rather than an event feed.
```

`priority_case` is built from `rule.priority_values` as `WHEN 'mobile' THEN 1 WHEN 'fixed' THEN 2 …`. For `strategy="most_frequent"` drop the `CASE` term and order by `event_count DESC, latest_event DESC` alone.

**Where this pattern comes from and how far to trust it.** It is modeled on a production Athena query the user supplied, which resolves `service_line` and then `category` onto `session_id` for a checkout-journey table. Take from it the *shape* — rank within key, `ROW_NUMBER` partitioned by the grain, join back on `rn = 1`, chain coarse-to-fine — and not its specifics. Two things in that reference query are deliberately **not** reproduced here:

- It re-joins each attributed level back to the event-level base and filters `WHERE b.category = psl.category`, which keeps the output at *event* grain, not session grain. That is right for its purpose (a filtered event feed) and wrong for ours (one row per key). A base view must end at its grain, and Task 13's grain probe — `COUNT(*)` versus `COUNT(DISTINCT <grain>)` over the inlined base — checks exactly that.
- Its ordering is `event_count DESC, latest_time DESC` — pure most-frequent, no business ranking. The prose problem statement it came with argues for ranking by business value (a completed transaction outranks time-on-page); the SQL does not yet do that. `strategy="highest_intent"` with an explicit `priority_case` is the version to generate, with most-frequent as the documented fallback.

Emit a short version of that caveat as a SQL comment inside the proposed `source_sql`, so the human reviewing the DRAFT base view sees which rule was applied without reading the payload.

- [ ] **Step 1: Write the failing tests**

```python
# --- the cube paths do not synthesize -------------------------------------

def test_a_retrieve_path_never_calls_the_sql_llm(stakeholder, spy_executor, plan_with_cube):
    """The base is governed. Re-authoring it would change the population_hash."""
    llm = RecordingLLM([])                       # any call raises
    stakeholder._synthesize_and_execute_sql(llm, "acme", "q", [], [], plan=plan_with_cube)
    assert llm.calls == 0

def test_the_executed_sql_is_the_composed_cube_byte_for_byte(stakeholder, spy_executor,
                                                             plan_with_cube):
    stakeholder._synthesize_and_execute_sql(MockLLM([]), "acme", "q", [], [], plan=plan_with_cube)
    assert spy_executor.last_sql == plan_with_cube.cube_sql.sql

def test_each_round_trip_is_bounded_by_the_transport_ceiling(stakeholder, spy_executor,
                                                             plan_with_cube):
    """Not raw_extract_row_limit -- Task 4's policy rejects that, and rightly."""
    stakeholder._synthesize_and_execute_sql(MockLLM([]), "acme", "q", [], [], plan=plan_with_cube)
    assert spy_executor.last_ctx.row_limit == stakeholder.settings.policy.max_transport_rows

# --- paging ----------------------------------------------------------------

def test_a_cube_larger_than_the_transport_is_paged(stakeholder, spy_executor, wide_cube_plan):
    """estimated_cells = 120,000, transport = 50,000 -> three round trips, concatenated."""
    spy_executor.returns_pages(50_000, 50_000, 20_000)
    _, res = stakeholder._synthesize_and_execute_sql(MockLLM([]), "acme", "q", [], [],
                                                     plan=wide_cube_plan)
    assert spy_executor.call_count == 3
    assert len(res.data) == 120_000
    assert not res.truncated

def test_paging_stops_on_a_short_page_not_on_the_estimate(stakeholder, spy_executor,
                                                          wide_cube_plan):
    """The estimate said 120,000; the warehouse had 60,000. Two trips, then stop."""
    spy_executor.returns_pages(50_000, 10_000)
    _, res = stakeholder._synthesize_and_execute_sql(MockLLM([]), "acme", "q", [], [],
                                                     plan=wide_cube_plan)
    assert spy_executor.call_count == 2 and len(res.data) == 60_000

def test_a_full_final_page_forces_one_more_trip(stakeholder, spy_executor, wide_cube_plan):
    """A page that is exactly chunk_rows long is not evidence of completeness."""
    spy_executor.returns_pages(50_000, 50_000, 0)
    stakeholder._synthesize_and_execute_sql(MockLLM([]), "acme", "q", [], [], plan=wide_cube_plan)
    assert spy_executor.call_count == 3

def test_paging_stops_at_the_materialised_ceiling_and_says_so(stakeholder, spy_executor,
                                                              wide_cube_plan):
    stakeholder.settings.policy.raw_extract_row_limit = 100_000
    spy_executor.returns_pages(50_000, 50_000, 50_000)
    _, res = stakeholder._synthesize_and_execute_sql(MockLLM([]), "acme", "q", [], [],
                                                     plan=wide_cube_plan)
    assert len(res.data) == 100_000 and res.truncated
    assert any("truncated" in w for w in res.warnings)

def test_no_page_ever_uses_offset(stakeholder, spy_executor, wide_cube_plan):
    spy_executor.returns_pages(50_000, 10_000)
    stakeholder._synthesize_and_execute_sql(MockLLM([]), "acme", "q", [], [], plan=wide_cube_plan)
    assert all("OFFSET" not in sql.upper() for sql in spy_executor.all_sql)
    assert ">" in spy_executor.all_sql[1]              # the cursor predicate

# --- widen -----------------------------------------------------------------

def test_a_widen_on_a_missing_dimension_re_runs_the_whole_cube(stakeholder, spy_executor):
    """Adding `device` re-splits every existing country cell -- there is no gap to fetch."""
    plan = _plan(path="widen", dimensions=["country", "device"],
                 verdict=CoverageVerdict(decision="widen", label="df_1",
                                         missing_dimensions=["device"], supersedes="df_1"))
    stakeholder._synthesize_and_execute_sql(MockLLM([]), "acme", "q", [], [], plan=plan)
    assert "GROUP BY 1, 2" in spy_executor.last_sql
    assert "df_1" not in spy_executor.last_sql        # not a delta query

def test_a_widen_on_a_time_gap_only_fetches_the_missing_window(stakeholder, spy_executor):
    """Cells over disjoint date ranges are disjoint and additive, so this one IS a gap fetch."""
    plan = _plan(path="widen", dimensions=["country"], time_column="date",
                 verdict=CoverageVerdict(decision="widen", label="df_1",
                                         missing_time_ranges=[("2026-07-01", "2026-07-31")]))
    stakeholder._synthesize_and_execute_sql(MockLLM([]), "acme", "q", [], [], plan=plan)
    sql = spy_executor.last_sql
    assert "2026-07-01" in sql and "2026-07-31" in sql
    assert "2026-08" not in sql                       # August is already on disk

# --- failure ownership ------------------------------------------------------

def test_a_failing_approved_base_is_surfaced_not_rewritten(stakeholder, spy_executor,
                                                           plan_with_cube):
    """An approved base is a human-owned artifact. Patching it silently would make its
    population_hash describe SQL that never ran."""
    plan_with_cube.base_view_approved = True
    spy_executor.always_fails("COLUMN_NOT_FOUND: revenue")
    sql, res, caveats = stakeholder._synthesize_and_execute_sql(
        MockLLM([]), "acme", "q", [], [], plan=plan_with_cube)
    assert not res.ok
    assert any("needs review" in c and "checkout_sessions" in c for c in caveats)
    assert spy_executor.call_count == 1               # no blind retry

def test_a_failing_draft_base_may_be_repaired_once(stakeholder, spy_executor, draft_plan):
    """The LLM authored this source_sql this turn and it was never reviewed."""
    spy_executor.fails_then_succeeds("COLUMN_NOT_FOUND: revenu")
    llm = RecordingLLM([REPAIRED_PROPOSAL])
    _, res, _ = stakeholder._synthesize_and_execute_sql(llm, "acme", "q", [], [], plan=draft_plan)
    assert res.ok and llm.calls == 1
    assert "COLUMN_NOT_FOUND" in llm.prompts[0]       # the error is fed back verbatim

def test_a_repair_that_fails_again_falls_back_to_aggregate(stakeholder, spy_executor, draft_plan):
    spy_executor.always_fails("boom")
    _, res, _ = stakeholder._synthesize_and_execute_sql(
        RecordingLLM([REPAIRED_PROPOSAL]), "acme", "q", [], [], plan=draft_plan)
    assert not res.ok
    assert spy_executor.call_count == 2               # original + one repair

# --- the aggregate path ------------------------------------------------------

def test_the_aggregate_path_still_calls_the_llm(stakeholder):
    llm = RecordingLLM(["```sql\nSELECT 1\n```"])
    stakeholder._synthesize_sql(llm, "q", [], [], plan=TurnPlan(path="aggregate"))
    assert llm.calls == 1

def test_mandatory_metric_filters_are_demanded_in_the_prompt(stakeholder, schema_ctx_with_metric):
    llm = RecordingLLM(["```sql\nSELECT 1\n```"])
    stakeholder._synthesize_sql(llm, "conversion by country", [], [],
                                plan=TurnPlan(path="aggregate"),
                                schema_ctx=schema_ctx_with_metric)
    assert "ALWAYS APPLY: is_test_traffic = false" in llm.last_system_prompt + llm.last_prompt

def test_the_aggregate_prompt_says_the_result_is_unreconcilable(stakeholder, schema_ctx):
    llm = RecordingLLM(["```sql\nSELECT 1\n```"])
    stakeholder._synthesize_sql(llm, "q", [], [], plan=TurnPlan(path="aggregate"),
                                schema_ctx=schema_ctx)
    assert "cannot be reconciled" in llm.last_system_prompt

def test_no_plan_and_no_schema_ctx_is_todays_prompt_exactly(stakeholder):
    """The regression guard for every existing test in this module."""
    llm = RecordingLLM(["```sql\nSELECT 1\n```"])
    stakeholder._synthesize_sql(llm, "q", [], [])
    assert "BUSINESS SEMANTICS" not in llm.last_system_prompt
    assert "cannot be reconciled" not in llm.last_system_prompt

# --- the attribution pattern in the proposal prompt --------------------------

def test_attribution_pattern_renders_a_ranked_case_and_row_number(stakeholder):
    p = stakeholder._render_attribution_pattern([AttributionRule(
        column="service_line", grain=["session_id"], strategy="highest_intent",
        priority_values=["mobile", "fixed", "ott"],
        tiebreakers=["event_count DESC", "log_time DESC"])])
    assert "ROW_NUMBER() OVER" in p and "PARTITION BY session_id" in p
    assert "WHEN 'mobile' THEN 1" in p and "WHEN 'ott' THEN 3" in p

def test_most_frequent_strategy_omits_the_priority_case(stakeholder):
    p = stakeholder._render_attribution_pattern([AttributionRule(
        column="category", grain=["session_id"], strategy="most_frequent")])
    assert "CASE category" not in p and "event_count DESC" in p

def test_no_rules_means_no_pattern_block(stakeholder):
    assert stakeholder._render_attribution_pattern([]) == ""

def test_the_proposal_prompt_carries_the_worked_pattern(stakeholder, schema_ctx_with_fanout):
    """A base view proposed over a fanned-out column must be told the shape, not the theory."""
    llm = RecordingLLM([PROPOSAL_JSON])
    stakeholder._plan_turn(llm, "acme", "c1", "revenue by service line", [], [],
                           schema_ctx=schema_ctx_with_fanout)
    assert "ROW_NUMBER() OVER" in llm.last_system_prompt
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -q -k "cube or paging or widen or aggregate_path or attribution_pattern"`
Expected: FAIL — `TypeError: unexpected keyword argument 'plan'`, then `AttributeError: … has no attribute '_execute_cube'`.

- [ ] **Step 3: Implement**

Order: `_render_attribution_pattern` first (pure, no dependencies), then `_execute_cube`, then `_fetch_keyset_chunks`, then the `_synthesize_and_execute_sql` dispatcher, then the `_synthesize_sql` prompt additions, then the `_plan_turn` proposal-prompt wiring.

- `_execute_cube` runs `plan.cube_sql.sql` through `policy.validate(sql, allowed_tables=…, dialect=self.settings.source_dialect, row_limit=max_transport_rows)` and then `executor.execute(approved_sql, ExecutionContext(...))`. **Do not re-inject a `LIMIT` of your own** — `compose_cube` did not emit one, and the policy's injection is the single place that decides it.
- Note that line 686 currently hardcodes `dialect="athena"` in the `ExecutionContext` while `_synthesize_sql` uses `self.settings.source_dialect`. That inconsistency is pre-existing and out of scope — leave it alone, and use `self.settings.source_dialect` in the *new* code paths rather than copying the hardcoded value forward.
- The paging loop's cursor is the last row's dimension tuple, read from the returned DataFrame, not from a counter. Reuse `compose_keyset_chunk` for the ID-grain case; for the cube case the same ordinal `ORDER BY`/row-value comparison applies over `spec.dimensions` — put that composition in `base_view.py` beside its sibling rather than growing a second SQL-writer inside `stakeholder.py`.
- Concatenate pages with `pd.concat(pages, ignore_index=True)` once at the end, not incrementally inside the loop.
- Every path returns the same triple shape the caller already expects; add caveats as a third element only if `_synthesize_and_execute_sql` already returns one — check the current signature at `stakeholder.py:645` before assuming, and thread the base-view caveats through whatever channel Task 14 collects them on rather than inventing a parallel one.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/stakeholder.py analytics_platform/base_view.py tests/test_stakeholder.py && git commit -m "feat(stakeholder): execute composed cubes with keyset paging; LLM SQL only on the aggregate path"
```

---

### Task 13: Attribution hierarchies in the brain + base-view grain verification

**Files:**
- Modify: `analytics_platform/schema_context.py` (from Task 8)
- Modify: `analytics_platform/junior.py`
- Modify: `analytics_platform/base_view.py` (from Task 7)
- Modify: `analytics_platform/stakeholder.py` (the verification call site)
- Modify: `analytics_platform/api.py` (one route)
- Test: Create `tests/test_attribution.py`

**Why a brain node and not a prompt constant:** which value outranks which is a *business* judgement and differs per tenant — for one company a completed transaction outranks a lead form, for another the reverse. It is exactly the kind of fact the Company Brain exists to hold, and exactly the kind that should pass a senior's review before it silently reshapes every answer. It also stops the LLM re-deriving the ranking from scratch (and differently) on every turn.

**Where these rules are consumed changed in Task 7, and it is worth being explicit.** Attribution no longer decorates a per-question extract; it is *inside* the base view's `source_sql` and is part of `population_hash`. So an approved rule is an input to **base-view proposal and review**, not to every turn's SQL: the planner reads it when authoring a new base (Task 12's worked pattern), and a human reads it when deciding whether to approve that base. Once approved, every cube inherits it for free and no prompt can override it. That is the point — two questions can no longer apply two rankings to the same sessions.

**Interfaces:**
- Storage: `DEFINITION` node titled `"Attribution Rule: <table>.<column> by <grain>"`, payload = `asdict(AttributionRule)` plus `{"table": …, "fanout": 0.06}`. Created by the junior with `status=DRAFT`, promoted through the existing `brain.submit` / `brain.approve` flow — no new governance machinery.
- On `SchemaContextBuilder`: `attribution_rules(tenant_id, tables) -> List[AttributionRule]`, rendered into `SchemaContext.rendered` under an `=== APPROVED ATTRIBUTION RULES ===` heading, between the base views and the schema. **Approved rules only** — a draft must never silently steer an answer.
- On `JuniorEngine`: `propose_attribution_rules(tenant_id, tables=None) -> List[KnowledgeNode]`. For every `(grain_key, column)` pair whose profiled `fanout_by_key` exceeds `attribution_fanout_threshold` (default `0.01`) and that has no rule yet, create a DRAFT node with `strategy="most_frequent"` and a summary naming the fan-out percentage. The junior proposes the *existence* of the problem; a human supplies the business ranking.
- On `BaseViewRegistry`:
  ```python
  def compose_grain_probe(self, view: BaseView) -> str: ...
  def record_grain_check(self, tenant_id: str, view: BaseView,
                         rows: int, keys: int) -> BaseView: ...
  ```
- `BaseView` gains three fields: `grain_verified: bool = False`, `grain_violation_ratio: float = 0.0`, `grain_checked_at: str = ""`.
- Route: `POST /knowledge/{tenant_id}/attribution/propose` → `propose_attribution_rules`. Review and approval reuse the existing `POST /knowledge/{tenant_id}/{node_id}/review` endpoint (`api.py:766-779`) — do not add a parallel approval path.

**The grain-integrity check moved, and if you implement it in the old place it will never fire.** The earlier draft of this plan checked `df.duplicated(subset=plan.grain)` on the returned extract. That worked when ID-grain rows came down; it is **useless against a cube**, because `GROUP BY` deduplicates the dimension tuple unconditionally. A base whose `source_sql` emits three rows per `session_id` produces a cube that looks immaculate — every cell unique, every `SUM` silently tripled. The violation is now invisible at exactly the layer that used to catch it.

So the check moves onto the base itself, where the fan-out actually lives, and runs **once per `population_hash`** rather than once per turn:

```sql
WITH base AS (
    <view.source_sql verbatim>
)
SELECT COUNT(*) AS row_count, COUNT(DISTINCT session_id) AS key_count
FROM base
```

Composite grain uses `COUNT(DISTINCT (k1, k2))` — or `COUNT(DISTINCT k1 || '\x1f' || k2)` if the dialect refuses a row constructor there; pick one and note which in a comment, do not leave it to the implementer to guess. Two rows back, one round trip, cheap enough to be unconditional.

- `row_count == key_count` → `grain_verified = True`. Store `row_count` as the view's `row_count_estimate` while you are here: Task 7's cell guard needs a real number and Task 5 could only offer a sampled floor. This is the honest one, and it costs nothing extra.
- `row_count != key_count` → `grain_verified = False`, `grain_violation_ratio = 1 - key_count / row_count`, and **the base is not usable**: refuse to compose cubes over it, add the caveat `"base view <name> returns <n> rows for <m> distinct <grain> keys -- it is not at the grain it claims, and every measure over it would be multiplied"`, and fall through to `aggregate` for that turn. A DRAFT base that fails this may be repaired once by the planner (same single-repair rule as Task 12); an APPROVED base that fails is a governance failure — log at ERROR, surface it, do not patch it.
- The result is written back onto the base-view node, keyed by `population_hash`. A view whose `source_sql` is edited gets a new hash and is therefore re-probed automatically — which is the second reason the hash is over the canonicalised SQL rather than a version number nobody would bump.
- Run the probe from `_plan_turn`, immediately after resolving the base and before `compose_cube`, and **only when the stored check does not match the current `population_hash`**. A tenant asking twenty questions against one approved base pays for this once, ever.

This is a stronger guarantee than the old row-level assertion, not a weaker one: it verifies the *definition* rather than one query's output, so it holds for every cube built on that base including ones nobody has asked for yet.

**The ID-grain path keeps a row-level check too.** When `compose_keyset_chunk` is used (non-additive measures, Task 12), the rows really do arrive at grain, so assert it on the concatenated frame: `df.duplicated(subset=view.grain).any()`. On a verified base this should never fire; if it does, the base changed underneath the stored check — record `extract_meta.grain_violated = True` and caveat it rather than trusting either artifact.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_attribution.py

# --- the junior proposes; a human ranks --------------------------------------

def test_junior_proposes_a_draft_rule_for_a_fanned_out_column(engine, tenant, profiled):
    nodes = engine.propose_attribution_rules(tenant, ["events"])
    n = next(n for n in nodes if "service_line" in n.title)
    assert n.status == ReviewStatus.DRAFT
    assert n.payload["strategy"] == "most_frequent"
    assert "6%" in n.summary or n.payload["fanout"] == pytest.approx(0.06, abs=0.01)

def test_a_clean_column_gets_no_rule(engine, tenant, profiled):
    assert not any("country" in n.title for n in engine.propose_attribution_rules(tenant, ["events"]))

def test_only_approved_rules_reach_the_prompt(builder, tenant, draft_rule, approved_rule):
    r = builder.build(tenant, "q", [], []).rendered
    assert approved_rule.payload["column"] in r
    assert draft_rule.payload["column"] not in r

def test_approved_rules_render_between_base_views_and_schema(builder, tenant, approved_rule,
                                                             approved_base_view):
    r = builder.build(tenant, "q", [], []).rendered
    assert r.index("BASE VIEWS") < r.index("ATTRIBUTION RULES") < r.index("DATABASE SCHEMA")

def test_proposing_twice_does_not_duplicate(engine, tenant, profiled):
    engine.propose_attribution_rules(tenant, ["events"])
    before = len(engine.brain(tenant).all(kind=NodeKind.DEFINITION))
    engine.propose_attribution_rules(tenant, ["events"])
    assert len(engine.brain(tenant).all(kind=NodeKind.DEFINITION)) == before

# --- the grain probe ---------------------------------------------------------

def test_the_probe_inlines_the_base_and_counts_both_ways(registry, approved_base_view):
    sql = registry.compose_grain_probe(approved_base_view)
    assert approved_base_view.source_sql in sql
    assert "COUNT(*)" in sql and "COUNT(DISTINCT session_id)" in sql

def test_a_clean_base_is_marked_verified_and_gets_a_real_row_count(registry, tenant,
                                                                   approved_base_view):
    v = registry.record_grain_check(tenant, approved_base_view, rows=1_200_000, keys=1_200_000)
    assert v.grain_verified is True
    assert v.row_count_estimate == 1_200_000      # replaces Task 5's sampled floor

def test_a_fanned_out_base_is_marked_unusable_with_the_ratio(registry, tenant,
                                                             approved_base_view):
    v = registry.record_grain_check(tenant, approved_base_view, rows=1_300_000, keys=1_200_000)
    assert v.grain_verified is False
    assert v.grain_violation_ratio == pytest.approx(1 - 1_200_000 / 1_300_000)

def test_a_cube_over_an_unverified_base_is_refused(registry, unverified_base_view):
    """The whole reason the check moved: GROUP BY would have hidden this."""
    out = registry.compose_cube(unverified_base_view,
            CubeSpec(base_name=unverified_base_view.name, dimensions=["country"],
                     measures=[CubeMeasure("revenue", "SUM(revenue)", True)]),
            {"country": _profile(30)})
    assert not out.ok and "grain" in out.error.lower()

def test_the_probe_runs_once_per_population_hash(stakeholder, spy_executor, approved_base_view):
    for _ in range(3):
        stakeholder._plan_turn(MockLLM([CUBE]), "acme", "c1", "q", [], [])
    assert spy_executor.probe_call_count == 1

def test_editing_the_source_sql_forces_a_re_probe(stakeholder, spy_executor, registry, tenant):
    stakeholder._plan_turn(MockLLM([CUBE]), "acme", "c1", "q", [], [])
    registry.upsert(tenant, _view(source_sql="SELECT session_id FROM orders_v2"), by="senior")
    stakeholder._plan_turn(MockLLM([CUBE]), "acme", "c1", "q", [], [])
    assert spy_executor.probe_call_count == 2

def test_a_failed_probe_on_an_approved_base_falls_through_to_aggregate(stakeholder, spy_executor,
                                                                       approved_base_view):
    spy_executor.probe_returns(rows=1_300_000, keys=1_200_000)
    out = stakeholder.answer("acme", "q", conversation_id="c1")
    assert any("not at the grain it claims" in c for c in out["caveats"])
    assert out["analysis"]["coverage"]["decision"] == "retrieve" or out["queries_run"] == []

# --- the ID-grain path still checks rows --------------------------------------

def test_duplicate_keys_in_a_keyset_extract_are_recorded(stakeholder, spy_executor):
    """On a verified base this cannot happen -- so if it does, trust neither artifact."""
    spy_executor.always_returns(pd.DataFrame({"session_id": ["s1", "s1"]}))
    out = stakeholder.answer("acme", "q", conversation_id="c1")
    assert out["extract_meta"]["grain_violated"] is True
    assert any("double-counted" in c for c in out["caveats"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_attribution.py -q`
Expected: FAIL — `AttributeError: … has no attribute 'propose_attribution_rules'`, then `… has no attribute 'compose_grain_probe'`.

- [ ] **Step 3: Implement**

Add `attribution_fanout_threshold: float = 0.01` to `Settings`. The probe goes through `QueryPolicy` like every other query — it is a `SELECT` with two aggregates and needs no special casing, and it must **not** be exempted from the transport ceiling just because it returns one row.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/schema_context.py analytics_platform/junior.py analytics_platform/base_view.py analytics_platform/stakeholder.py analytics_platform/api.py analytics_platform/config.py tests/test_attribution.py && git commit -m "feat(brain): tenant attribution hierarchies + one-shot base-view grain verification"
```

---

### Task 14: Restructure `answer()` into the analyst pipeline, and record the Analysis artifact

**Files:**
- Modify: `analytics_platform/stakeholder.py:248-360` (`answer`), `:690-728` (`_synthesize_python`), `:730-778` (`_synthesize_and_execute_python`), `:817-852` (`_record`)
- Modify: `analytics_platform/database.py` (~line 227, additive migration)
- Test: Create `tests/test_extract_flow.py`

**Read this first, because it inverts an assertion the original bug report implies.** The reported failure was *"the SQL was a pre-aggregated answer."* Under this design the warehouse SQL **is** aggregated — a `GROUP BY` cube — and that is correct, not a regression. What was actually wrong was never the `GROUP BY`; it was that the aggregate was a *one-off answer to one question*, authored from scratch, reusable for nothing and reconcilable with nothing. A cube is aggregated **at a dimension set chosen to be reused**, over a **governed population**, carrying a `population_hash`. So do not write a test asserting `"GROUP BY" not in queries_run[0]` — the earlier draft of this plan did, and it now asserts the opposite of what the design wants. The assertions that matter are that a *second, related* question issues **zero** warehouse queries, and that both answers carry the same `population_hash`.

**Interfaces:**
- Produces: `_record(..., extract_meta=None, analysis=None)`; the answer dict and the `stakeholder_answers` row gain `extract_meta` and `analysis`.
- New columns: `ALTER TABLE stakeholder_answers ADD COLUMN extract_meta TEXT` and `… ADD COLUMN analysis TEXT` — follow the exact `if "produced_df_label" not in sa_cols:` pattern at `database.py:227-228`, one guard per column.
- Produces, in `analytics_platform/domain.py` — **the provenance record for one analytical turn**:
  ```python
  @dataclass
  class AnalysisArtifact:
      question: str
      plan_rationale: str
      # --- the population: what makes this answer comparable to another -----
      base_view: str                   # "" on the aggregate path
      population_hash: str             # "" on the aggregate path
      projection_hash: str
      base_view_approved: bool         # False -> the figures are provisional
      base_view_grain_verified: bool   # from Task 13's probe
      reconcilable: bool               # population_hash != "" and grain verified
      slice_filters: Dict[str, List[str]]   # the question's own filters -- NOT hashed
      dimensions: List[str]
      non_additive: List[str]          # measures in this cube that cannot roll up
      supersedes: str                  # a narrower cube this turn replaced
      # --- the rest of the turn ---------------------------------------------
      semantics_used: List[str]        # metric/dimension names the semantic layer matched
      unresolved_terms: List[str]      # measures with no approved definition
      requirement: Dict[str, Any]      # asdict(DataRequirement)
      coverage: Dict[str, Any]         # asdict(CoverageVerdict) -- includes the human reason
      datasets_used: List[str]         # df_labels
      warehouse_sql: List[str]
      workspace_sql: List[str]         # DuckDB queries run locally
      python_code: List[str]
      result_summary: Any
      chart_spec: Optional[Dict[str, Any]]
      key_findings: List[str]
      assumptions: List[str]           # attributions applied, truncation, provisional base
      created_at: str
  ```
  Serialised onto the answer row and returned in the payload. This is what makes an answer *reproducible* rather than merely plausible, and it is the entire input Plan B's "▸ Data used / ▸ SQL / ▸ Analysis code / ▸ Methodology" disclosure renders from — plus the input to Task 15's `reconcile` endpoint. **Build it incrementally through the turn** — a local `artifact` dict appended to at each step — rather than reconstructing it at the end from whatever variables happen to still be in scope.

  `population_hash` is the field that has to be right. Everything else is documentation; that one is a claim, and Task 15 lets a user act on it.

**The new control flow in `answer()`,** replacing the `_choose_compute_path` gate at line 285 and the SQL block at 314-360. Keep each stage a **named private method** returning its piece of the artifact — Plan B turns exactly these boundaries into SSE step events, and a method per step makes that refactor mechanical instead of surgical:

```
schema_ctx = self._resolve_semantics(...)        # step: "understanding"   (Task 8)
plan       = self._plan_turn(...)                # step: "planning"        (Tasks 11+13)
                                                 #   -> resolves the base view
                                                 #   -> probes its grain once per population_hash
                                                 #   -> composes + guards the cube (Task 7)
                                                 #   -> DataManager verdict (Task 10)

if plan.path in ("reuse", "widen"):              # step: "checking workspace"
    workspace.register(...) for every label the verdict names

if plan.path in ("retrieve", "widen"):           # step: "retrieving"
    sql, exec_res = self._synthesize_and_execute_sql(..., plan=plan)   # Task 12: executes
                                                 # the COMPOSED cube; no SQL LLM call here
    on success:
        label = data_cache.next_label(...)
        meta  = ExtractMeta(
            base_view=plan.base_view.name,
            population_hash=plan.cube_sql.population_hash,
            projection_hash=plan.cube_sql.projection_hash,
            grain=plan.base_view.grain,
            dimensions=plan.cube.dimensions,
            non_additive=plan.cube_sql.non_additive,
            filters=plan.cube.filters,                     # the slice
            columns=list(df.columns), row_count=len(df),
            time_column=tc, time_start=df[tc].min(), time_end=df[tc].max(),
            truncated=<accumulated rows >= raw_extract_row_limit
                       or any("truncated" in w for w in exec_res.warnings)>,
            sql=sql, ...)
        data_cache.put(..., label, question[:200], exec_res.data, meta=meta)
        workspace.register(tenant_id, conversation_id, label)
        # on "widen": the wider cube SUPERSEDES the narrower one (Task 10 rule 8).
        # Both stay on disk and both stay registered -- they share a population_hash,
        # so nothing computed from the old one is invalidated. Record
        # artifact.supersedes = verdict.supersedes.
    on failure: fall through to plan.path = "aggregate", and carry the reason
                into caveats -- a silent downgrade is the worst outcome here,
                because the answer stops being reconcilable and nothing says so.

                                                 # step: "analysing"
if plan.analysis == "workspace_sql":
    local_sql, ws_res = self._synthesize_and_execute_workspace_sql(
        llm, tenant_id, conversation_id, question, labels)
    on failure after the repair loop: fall back to plan.analysis = "python"
else:
    code, py_res, toks = self._synthesize_and_execute_python(
        llm, tenant_id, conversation_id, question, label)   # ← turn 1 now runs Python too

                                                 # step: "interpreting"
synthesize the narrative from whichever result came back, build the artifact,
_record(queries_run=[sql], python_cells=[…], produced_df_label=label,
        extract_meta=meta_dict, analysis=asdict(artifact))

# aggregate: today's existing SQL block verbatim, plan.path == "aggregate",
#            population_hash = "", reconcilable = False
```

**Time-derived fields come from the returned frame, not from the plan.** `time_start`/`time_end` are `df[tc].min()/.max()`, because what the SQL *asked* for and what the warehouse *had* are not the same thing, and Task 10's coverage check will happily reuse a cube for a window it never actually contained.

**`_synthesize_and_execute_workspace_sql` is new and mirrors `_synthesize_and_execute_python` exactly** — same repair loop, same attempt cap, same `prior_error` feedback. Its prompt lists the registered views with their columns, **dimensions**, **which measures are non-additive**, and a 3-row sample; states the dialect is **DuckDB**; and requires a single `SELECT`. Two rules go in that prompt and they are not optional:

- *"An averaged measure is stored as `<name>_sum` and `<name>_count`. To read it, divide: `SUM(x_sum) / NULLIF(SUM(x_count), 0)`. Never average `x_sum`, and never average an average."*
- *"These measures are listed as NON-ADDITIVE: {…}. Do not `SUM` them and do not group them to fewer columns than the view already carries — the numbers would be wrong in a way that looks right."*

On repeated failure it falls back to the Python path rather than to a new warehouse query: the data is already local, and a failed local query is not a reason to re-bill the warehouse.

**Also required in this task:**
- **A `reuse` turn must issue zero warehouse queries.** That is the single most important assertion in this plan — it is the observable that was broken. `out["queries_run"] == []` on a reuse turn.
- **Uncertainty is surfaced, not smoothed over.** Every `schema_ctx.semantics.unresolved_terms` entry becomes a caveat: `"'churn' is not a defined metric for this company -- this figure was computed from raw events and has not been validated against an approved definition."` Same for `schema_ctx.unprofiled`: `"tables <x>, <y> could not be profiled -- filter values were not verified against the data."`
- **Four population caveats, each attached to the condition that produces it.** These are the ones that carry the design's honesty, so none may be dropped for brevity:
  - `base_view_approved is False` → `"this answer rests on an unreviewed base view definition (<name>); figures are provisional until it is approved."`
  - `path == "aggregate"` → `"no base view governs this query, so this number cannot be reconciled against other answers in this conversation."`
  - `base_view_grain_verified is False` → the Task 13 wording, and the turn should not have got this far — if it did, that is a bug worth logging.
  - `plan.base_view.attributions` non-empty → one caveat per rule: `"service_line attributed to each session by highest intent (mobile > fixed > ott); sessions touching multiple service lines are counted once, under their highest-ranked one."` This one is inherited from the base, so it rides along on **every** turn over that base, including pure `reuse` turns that ran no SQL at all — the number still depends on the ranking.
- **Truncation caveats ride along too.** When `meta.truncated` is true, `"cube truncated at N rows -- totals and rates may be understated"` goes on the extract turn *and* on every later turn computed from that frame.
- `_synthesize_python`'s system prompt (line 704-714) must gain: *"The DataFrame is a cube: one row per combination of {dimensions}, with pre-aggregated measures. To answer at fewer dimensions, sum over the ones you are dropping — do not treat a row as a single event."* Keep the existing "not the full raw DataFrame unmodified" sentence; the `MAX_RESULT_ROWS = 20` cap makes returning the raw frame useless anyway. Add the same averaged-measure and non-additive rules the workspace-SQL prompt carries — the Python path can violate them just as easily. And add: *"If a chart would make the finding clearer, also assign `chart = {...}` — a small spec of `{kind, x, y, series, title}` over the rows in `result`. Do not attempt to draw or save an image."* The spec is data; Plan B renders it.
- `_synthesize_and_execute_python` must call `run_python_sandboxed` with `dataframe_paths={df_label: path}` when the cache's store has a Parquet path for the label, and `memory_mb=EXTRACT_MEMORY_MB, timeout_s=EXTRACT_TIMEOUT_S`. Fall back to the in-memory `dataframes=` form when there is no path.

**`answer()` builds the `SchemaContext` once per turn** (before `_plan_turn`) and threads the same object into `_plan_turn` and into `_synthesize_and_execute_sql`'s aggregate path. Building it twice would double the inline-profiling cost.

- [ ] **Step 1: Write the failing end-to-end test**

```python
# tests/test_extract_flow.py
CUBE_1 = ('{"base_view":"checkout_sessions",'
          '"cube":{"dimensions":["country","device"],'
          '"measures":[{"name":"revenue","expr":"SUM(revenue)"}],"filters":{}},'
          '"analysis":"python"}')

def test_first_turn_builds_a_cube_and_answers_in_python(tenant, tmp_path, approved_base_view):
    """The trail the user reported missing: a warehouse query AND a Python cell.
    The SQL is aggregated on purpose -- what makes it not the old bug is that it is
    aggregated over a governed population at a reusable dimension set."""
    llm = SequencedLLM([
        "revenue by country",                                   # _extract_search_intent
        CUBE_1,                                                 # _plan_turn
        "```python\nresult = df_1.groupby('country')['revenue'].sum().to_dict()\n```",
        "Revenue is concentrated in IN and US.",                # _synthesize
    ])
    out = service.answer(tenant, "what is revenue by country?", conversation_id="c1")

    assert len(out["queries_run"]) == 1
    assert "WITH base AS (" in out["queries_run"][0]            # the base, inlined verbatim
    assert approved_base_view.source_sql in out["queries_run"][0]
    assert len(out["python_cells"]) == 1                        # the missing trail, now present
    assert out["produced_df_label"] == "df_1"
    assert out["extract_meta"]["dimensions"] == ["country", "device"]
    assert out["extract_meta"]["population_hash"]
    assert out["analysis"]["coverage"]["decision"] == "retrieve"
    assert out["analysis"]["reconcilable"] is True

def test_no_sql_llm_call_is_made_on_the_cube_path(tenant, tmp_path, approved_base_view):
    """Task 12's contract, observed end to end: the LLM plans, it does not write SQL."""
    llm = SequencedLLM(["revenue by country", CUBE_1, "```python\nresult = 1\n```", "ok"])
    service.answer(tenant, "what is revenue by country?", conversation_id="c1")
    assert llm.exhausted                                        # exactly 4 calls, none for SQL

def test_a_reuse_turn_issues_no_warehouse_query(tenant, tmp_path, approved_base_view):
    """The observable that was broken. device is already a dimension of df_1, so the
    follow-up rolls up locally -- zero SQL to Metabase."""
    ... first turn as above ...
    llm2 = SequencedLLM([
        "device breakdown",
        '{"base_view":"checkout_sessions","cube":{"dimensions":["device"],'
        '"measures":[{"name":"revenue","expr":"SUM(revenue)"}],"filters":{}},'
        '"analysis":"workspace_sql"}',
        "```sql\nSELECT device, SUM(revenue) AS r FROM df_1 GROUP BY device\n```",
        "iOS leads.",
    ])
    out2 = service.answer(tenant, "break that down by device", conversation_id="c1")
    assert out2["queries_run"] == []                            # no warehouse round trip
    assert len(out2["analysis"]["workspace_sql"]) == 1          # answered in DuckDB
    assert "df_1" in out2["analysis"]["coverage"]["reason"]

def test_two_answers_over_one_base_carry_the_same_population_hash(tenant, tmp_path,
                                                                  approved_base_view):
    """The triangulation guarantee, asserted end to end. This is the test that says
    the whole design works: A and B differ in dimensions and filters, and are still
    provably computed from the same rows."""
    a = service.answer(tenant, "revenue by country", conversation_id="c1")
    b = service.answer(tenant, "revenue in Germany by device", conversation_id="c1")
    assert a["analysis"]["population_hash"] == b["analysis"]["population_hash"]
    assert b["analysis"]["slice_filters"] == {"country": ["Germany"]}   # a slice, not a population

def test_a_widen_supersedes_the_narrower_cube_and_keeps_both(tenant, tmp_path,
                                                             approved_base_view):
    ... df_1 is country-only; the turn asks for country x service_line ...
    assert out["analysis"]["supersedes"] == "df_1"
    assert {m.label for m in service.data_cache.store.list_metas(tenant, "c1")} == {"df_1", "df_2"}
    assert out["extract_meta"]["population_hash"] == _meta_of("df_1").population_hash

def test_a_widen_on_a_time_gap_only_fetches_the_missing_window(tenant, tmp_path,
                                                               approved_base_view):
    ... df_1 covers August; the turn asks for July-August ...
    assert "2026-07" in out["queries_run"][0] and "2026-08-01" not in out["queries_run"][0]
    assert out["extract_meta"]["label"] == "df_2"

def test_a_provisional_base_view_is_caveated(tenant, tmp_path):
    """Day one: the planner proposed the base, nobody has approved it."""
    out = service.answer(tenant, "guest revenue by country", conversation_id="c1")
    assert out["analysis"]["base_view_approved"] is False
    assert any("provisional" in c for c in out["caveats"])

def test_the_aggregate_path_says_it_cannot_be_reconciled(tenant, tmp_path):
    ... plan returns aggregate_only ...
    assert out["analysis"]["reconcilable"] is False
    assert any("cannot be reconciled" in c for c in out["caveats"])

def test_an_attribution_caveat_rides_along_on_a_reuse_turn(tenant, tmp_path,
                                                           attributed_base_view):
    """The reuse turn ran no SQL, but the number still depends on the ranking."""
    ... first turn, then a pure reuse follow-up ...
    assert any("highest intent" in c for c in out2["caveats"])

def test_a_python_analysis_turn_can_return_a_chart_spec(tenant, tmp_path):
    ... plan says analysis="python"; the cell assigns result and chart ...
    assert out["analysis"]["chart_spec"]["kind"] == "bar"

def test_extract_survives_a_cold_workspace(tenant, tmp_path):
    """Reopening a conversation in a fresh process must still be answerable locally."""
    ... first turn ...
    service2 = build_service(tenant, tmp_path)                  # fresh caches, same disk
    out = service2.answer(tenant, "now by device", conversation_id="c1")
    assert out["queries_run"] == []

def test_truncated_cube_produces_a_visible_caveat(tenant, tmp_path):
    ... executor returns exactly raw_extract_row_limit rows across pages ...
    assert any("truncated" in c for c in out["caveats"])
    assert out["extract_meta"]["truncated"] is True

def test_an_undefined_metric_is_flagged_as_uncertain(tenant, tmp_path):
    """No approved 'churn' metric exists -- the answer must say so rather than
    quietly inventing a definition."""
    out = service.answer(tenant, "what is our churn rate?", conversation_id="c1")
    assert any("churn" in c and "not a defined metric" in c for c in out["caveats"])
    assert out["analysis"]["unresolved_terms"] == ["churn"]

def test_a_failed_cube_downgrade_is_announced_not_silent(tenant, tmp_path,
                                                          approved_base_view):
    """Falling back to aggregate loses reconcilability. Saying nothing is the worst case."""
    ... executor fails the cube query ...
    assert out["analysis"]["reconcilable"] is False
    assert any("cannot be reconciled" in c for c in out["caveats"])

def test_the_artifact_records_every_stage(tenant, tmp_path, approved_base_view):
    a = service.answer(tenant, "revenue by country", conversation_id="c1")["analysis"]
    assert a["base_view"] == "checkout_sessions" and a["population_hash"]
    assert a["datasets_used"] == ["df_1"]
    assert a["warehouse_sql"] and a["python_code"]
    assert a["created_at"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_extract_flow.py -q`
Expected: FAIL — `KeyError: 'extract_meta'`, and `python_cells` empty on turn 1.

- [ ] **Step 3: Implement**

Order: migration in `database.py` first, then `_record`, then `_synthesize_python` prompt + sandbox path wiring, then `_synthesize_and_execute_workspace_sql`, then the `answer()` restructure last. Construct `ExtractStore`, `AnalyticalWorkspace`, `BaseViewRegistry`, `SemanticLayer`, and `DataManager` where `ConversationDataCache` is constructed today (`grep -rn "ConversationDataCache(" analytics_platform`) using `settings.tenants_dir`, and pass them into `StakeholderService` — do not construct them per turn.

This is the largest task in the plan. If the implementer reports it is too big, the clean split is: (14a) migration + `_record` + artifact assembly; (14b) `_synthesize_and_execute_workspace_sql` + its repair loop; (14c) the `answer()` restructure. Do not split it any other way — the `answer()` rewrite needs both halves in place.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/stakeholder.py analytics_platform/database.py analytics_platform/domain.py tests/test_extract_flow.py && git commit -m "feat(stakeholder): semantic-first analyst pipeline with cube reuse and population provenance"
```

---

### Task 15: CSV download, the `reconcile` endpoint, and widened conversation replay

**Files:**
- Modify: `analytics_platform/api.py` (near the stakeholder block at 1041-1090)
- Modify: `analytics_platform/stakeholder.py:163-183` (`get_conversation`) and `:205-215` (`delete_conversation`)
- Test: `tests/test_api.py` (extend)

**Interfaces:**
- Produces:
  - `GET /stakeholder/{tenant_id}/conversations/{conversation_id}/extracts/{label}/download` → `Response` with `text/csv` and a `Content-Disposition` header.
  - `POST /stakeholder/{tenant_id}/conversations/{conversation_id}/reconcile` — body `{"answer_a": <message_id>, "answer_b": <message_id>, "measure": "revenue"}` → `asdict(ReconcileResult)`.
  - `get_conversation` messages gain `"extract_meta": load_json(r["extract_meta"], {})` and `"analysis": load_json(r["analysis"], {})`.
  - `delete_conversation` also calls `extract_store.delete_conversation(...)` **and** `workspace.close(...)` — deleting a chat must delete its raw data and drop its DuckDB connection.

**This task is the whole interface Plan B consumes.** Everything the assistant-ui thread will render — the tool-step trail, the "Data used / SQL / Analysis code / Methodology" disclosures, the chart, the caveats, the storyline candidates — has to already be in this payload. Treat a field missing here as a Plan B blocker, not a nice-to-have.

**`reconcile` is where the design stops being a claim and becomes a check.** Everything upstream exists so that two answers *can* be compared; nothing until now actually compares them. The endpoint takes two answers from a conversation and reports whether they rest on the same rows and, when they do, whether their numbers agree.

The comparison is not "two totals, subtract". Two answers legitimately differ when their slices differ, so:

1. Read both answers' `analysis` blobs. If `population_hash_a != population_hash_b`, return immediately: `same_population=False`, `agrees=False`, and an explanation naming both base views. **Do not compute anything** — comparing numbers across populations is the exact mistake the whole design is built to prevent, and computing them anyway invites someone to read the difference as meaningful.
2. Same population → compute the measure over **the intersection of the two slices**, from each answer's own cube, in DuckDB. Intersecting is what makes the comparison fair: answer A filtered to Germany and answer B unfiltered agree about Germany, and that is the only thing they can be expected to agree about.
3. A slice column that is not a dimension of both cubes makes the intersection inexpressible on at least one side. Say so — `agrees=False` with an explanation reading `"cannot compare at this slice: df_2 does not carry `device`, so the Germany-and-iOS subset cannot be isolated from it"` — rather than silently comparing at a wider slice and reporting a false disagreement.
4. Both cubes must be additive for the measure at the intersected dimension set (Task 10's rule 2 applies unchanged). A `COUNT(DISTINCT)` on either side that would need to roll up → explain, do not guess.
5. Then call `reconcile(...)` from Task 7 with the two computed values and its `tolerance`.

`explanation` is written for a human and is the field the UI actually shows. `"both answers were computed over checkout_sessions (population 4f3a…); over Germany in August, revenue is 1,240,551.20 from df_1 and 1,240,551.20 from df_2 — they agree"` is the shape to aim for. A disagreement over the same population is a **real finding**, usually a truncated cube or a non-additive roll-up, so name the likely cause when `meta.truncated` or `meta.non_additive` is set on either side.

**Reuse, do not reinvent (the download route):** the storyline export route (`api.py` ~1078-1120) already solved the filename problem. Copy its dual-form pattern verbatim — an ASCII slug for `filename=` plus RFC 5987 `filename*=UTF-8''…`, bounded to 60 chars — because Starlette encodes response headers as latin-1 and a raw unicode question title raises. Also copy its `expose_headers` handling: `Content-Disposition` is **not** CORS-safelisted, so the browser cannot read it unless the CORS middleware exposes it.

**Errors:** unknown tenant → the existing `tenant_or_404`; unknown conversation, label, or message id → 404; a `ValueError` from `ExtractStore`'s id validation → 400 (never let it become a 500, and never let it reach the filesystem). An answer with no `population_hash` (the aggregate path) is a **200 with `same_population=False`** and an explanation saying it was computed without a base view — not an error. That is a real, expected state, and the user asking to reconcile it deserves the reason rather than a stack trace.

- [ ] **Step 1: Write the failing tests**

```python
# --- download ---------------------------------------------------------------

def test_download_returns_csv_for_a_real_extract(app, tenant):
    ...produce a cube via the extract flow...
    r = call(app, "GET", "/stakeholder/{t}/conversations/c1/extracts/df_1/download", tenant)
    assert r.media_type == "text/csv"
    assert r.body.decode().splitlines()[0] == "country,device,revenue"
    assert "filename*=UTF-8''" in r.headers["content-disposition"]

def test_download_404s_for_an_unknown_label(app, tenant):
    with pytest.raises(HTTPException) as e:
        call(app, "GET", "/stakeholder/{t}/conversations/c1/extracts/df_99/download", tenant)
    assert e.value.status_code == 404

def test_download_400s_on_a_traversal_attempt(app, tenant):
    with pytest.raises(HTTPException) as e:
        call(app, "GET", "/stakeholder/{t}/conversations/c1/extracts/..%2F..%2Fetc/download", tenant)
    assert e.value.status_code == 400

def test_deleting_a_conversation_deletes_its_extracts(service, tenant, tmp_path):
    ...produce an extract, then delete the conversation...
    assert service.data_cache.store.list_metas(tenant, "c1") == []

# --- reconcile ---------------------------------------------------------------

def test_two_answers_over_one_base_reconcile(app, tenant, two_answers_same_base):
    a, b = two_answers_same_base
    r = call(app, "POST", "/stakeholder/{t}/conversations/c1/reconcile", tenant,
             {"answer_a": a, "answer_b": b, "measure": "revenue"})
    assert r["same_population"] is True and r["agrees"] is True
    assert r["value_a"] == pytest.approx(r["value_b"])

def test_different_populations_are_refused_without_computing(app, tenant,
                                                             answers_over_two_bases,
                                                             spy_workspace):
    a, b = answers_over_two_bases
    r = call(app, "POST", "/stakeholder/{t}/conversations/c1/reconcile", tenant,
             {"answer_a": a, "answer_b": b, "measure": "revenue"})
    assert r["same_population"] is False and r["agrees"] is False
    assert r["value_a"] is None and r["value_b"] is None
    assert spy_workspace.query_count == 0        # nothing was computed -- deliberately

def test_the_comparison_is_made_at_the_intersected_slice(app, tenant, germany_and_all):
    """A filtered to Germany, B unfiltered. They agree about Germany, and that is
    the only thing they can be asked to agree about."""
    a, b = germany_and_all
    r = call(app, "POST", "/stakeholder/{t}/conversations/c1/reconcile", tenant,
             {"answer_a": a, "answer_b": b, "measure": "revenue"})
    assert r["agrees"] is True
    assert "Germany" in r["explanation"]

def test_an_inexpressible_slice_is_explained_not_faked(app, tenant, slice_not_in_both):
    r = call(app, "POST", "/stakeholder/{t}/conversations/c1/reconcile", tenant,
             {"answer_a": a, "answer_b": b, "measure": "revenue"})
    assert r["agrees"] is False and "does not carry" in r["explanation"]

def test_a_real_disagreement_names_a_likely_cause(app, tenant, one_truncated_cube):
    r = call(app, "POST", "/stakeholder/{t}/conversations/c1/reconcile", tenant,
             {"answer_a": a, "answer_b": b, "measure": "revenue"})
    assert r["same_population"] is True and r["agrees"] is False
    assert "truncat" in r["explanation"].lower()

def test_an_aggregate_path_answer_reconciles_with_nothing(app, tenant, aggregate_answer,
                                                          cube_answer):
    r = call(app, "POST", "/stakeholder/{t}/conversations/c1/reconcile", tenant,
             {"answer_a": aggregate_answer, "answer_b": cube_answer, "measure": "revenue"})
    assert r["same_population"] is False
    assert "no base view" in r["explanation"].lower()

def test_reconcile_404s_on_an_unknown_message_id(app, tenant):
    with pytest.raises(HTTPException) as e:
        call(app, "POST", "/stakeholder/{t}/conversations/c1/reconcile", tenant,
             {"answer_a": "nope", "answer_b": "nope", "measure": "revenue"})
    assert e.value.status_code == 404

# --- replay ------------------------------------------------------------------

def test_replay_carries_the_population_provenance(app, tenant):
    msgs = call(app, "GET", "/stakeholder/{t}/conversations/c1", tenant)["messages"]
    a = next(m for m in msgs if m.get("analysis"))["analysis"]
    assert a["population_hash"] and a["base_view"] and "base_view_approved" in a
```

Remember: `call()` invokes route closures directly and **bypasses middleware**, so it cannot prove the CORS `expose_headers` change works. Verify that part manually in the browser step below.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_api.py -q -k "download or reconcile or deleting_a_conversation"`
Expected: FAIL — route not found.

- [ ] **Step 3: Implement**

Stream the download with `df.to_csv(index=False)` into a `Response`. At the materialised ceiling this is a large body — that is the accepted cost of "let me download the data"; do not silently sample it. If `ExtractMeta.truncated` is true, that fact is already in `extract_meta`; do not alter the CSV.

`reconcile` reads its two cubes through `AnalyticalWorkspace.query`, which already runs `QueryPolicy` and caps the result — do not reach into Parquet directly to save a step. Compose the comparison SQL in Python from the intersected filters; it is a two-line `SELECT SUM(...) FROM <label> WHERE ...` per side and needs no LLM anywhere near it.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/api.py analytics_platform/stakeholder.py tests/test_api.py && git commit -m "feat(api): CSV download, cross-answer reconcile endpoint, provenance in replay"
```

---

### Task 16: Retention sweep

**Files:**
- Modify: `analytics_platform/retention.py`
- Test: `tests/` — extend whatever module covers `retention.py` (`grep -rln "retention" tests/`)

Extract Parquet at a 1,000,000-row ceiling accumulates fast. Wire `ExtractStore.sweep(settings.policy.extract_retention_days)` into the existing retention job following that module's established pattern — do not invent a new scheduler. One test: a conversation directory older than the cutoff is removed, a fresh one is kept.

- [ ] **Step 1:** Write the failing test
- [ ] **Step 2:** Run it, confirm it fails
- [ ] **Step 3:** Implement
- [ ] **Step 4:** `.venv/bin/python -m pytest tests/ -q` → PASS
- [ ] **Step 5:** Commit

```bash
git add analytics_platform/retention.py tests/ && git commit -m "feat(retention): sweep expired conversation extracts"
```

---

## Verification

**Automated**

```bash
.venv/bin/python -m pytest tests/ -q
```

Expected: 478 pre-existing tests still pass (1 skipped), plus `tests/test_extract_store.py`, `test_column_profiler.py`, `test_semantic.py`, `test_base_view.py`, `test_schema_context.py`, `test_workspace.py`, `test_data_manager.py`, `test_attribution.py`, `test_extract_flow.py`, and the additions to `test_dataframe_cache.py`, `test_python_sandbox.py`, `test_stakeholder.py`, `test_junior.py`, `test_api.py`.

`cd frontend && npx tsc --noEmit` still runs clean because **nothing in `frontend/` changed** — run it once at the end as a guard that nobody drifted into Plan B's territory.

**End-to-end, against the real tenant.** Plan A has no UI, so this is driven through the API. Boot the backend:

```bash
./start_session.command
```

Set `T` to your tenant id, then work through the steps below. Each `curl` is a full check — read the JSON, do not just confirm a 200.

0. **Profile first, and read what it found.** Trigger a profile pass, then fetch a `"Column Profile: <table>"` node. Confirm the low-cardinality columns carry a complete value list with real casing (`'mobile'`, not `'Mobile'`), the date columns carry a true range, `distinct_count` looks plausible on every column you intend to group by, and any column that fans out across a session/order key carries a non-zero `fanout_by_key`. **If a categorical you know is multi-valued shows fan-out 0.0, stop** — the sample missed it, and every downstream attribution decision will be wrong. Raise `profile_sample_rows` and re-run before continuing.

1. **Define one real metric and approve it.** `POST /knowledge/$T/semantic/metrics` with a metric your business actually uses — its formula, its grain, its dimensions, and its mandatory filters — then approve it via the existing review endpoint. This is the step that makes the rest of the verification meaningful; a semantic layer with nothing in it proves nothing.

2. **Define one real base view at ID grain and approve it.** `POST /knowledge/$T/base-views` with the `source_sql` you would actually stand behind — the joins, the test-traffic exclusion, the partition floor, and any attribution CTEs — at one row per `session_id` (or your equivalent). Approve it. Then confirm the grain probe ran and passed:
   ```bash
   curl -s "localhost:8000/knowledge/$T/base-views" | python3 -m json.tool | grep -E 'grain_verified|row_count_estimate|population_hash'
   ```
   `grain_verified` must be `true` and `row_count_estimate` must be the real row count, not the profile sample size. **If `grain_verified` is false, stop and fix the SQL** — every cube built on it would multiply its measures, and `GROUP BY` would hide that completely. Note the `population_hash`; several later steps compare against it.

3. **Ask a genuinely analytical first question** that uses the metric. Confirm in the response JSON:
   - `queries_run[0]` **starts with `WITH base AS (`** and contains your `source_sql` byte for byte. It is a `GROUP BY` query, and that is correct — the point is not that it avoids aggregation but that it aggregates a *governed* population at a dimension set that can be reused.
   - it uses the **real column names and real filter literals** from the profile. `WHERE service_line = 'Mobile'` when the data says `'mobile'` means the schema block is not reaching the prompt.
   - the metric's mandatory filter is enforced — inside the base, not bolted on as a slice
   - `python_cells` (or `analysis.workspace_sql`) is non-empty — the trail that was missing entirely
   - `analysis.population_hash` equals the hash from step 2, and `analysis.base_view_approved` is `true`
   - `analysis.reconcilable` is `true`, `analysis.coverage.decision == "retrieve"`, and `analysis.semantics_used` names the metric

4. **Download the cube** and confirm it is a cube, not a duplicate-riddled extract:
   ```bash
   curl -s "localhost:8000/stakeholder/$T/conversations/c1/extracts/df_1/download" -o /tmp/df_1.csv
   python3 -c "import pandas as pd; d=pd.read_csv('/tmp/df_1.csv'); dims=['country','device']; print(len(d), len(d.drop_duplicates(dims)))"
   ```
   The two numbers must match — one row per dimension combination. They will, because `GROUP BY` guarantees it; the check that actually matters is step 2's `grain_verified`, which is why it comes first.

5. **Ask a follow-up that is a pure re-cut** over a dimension the cube already carries (*"now just by device"*). This is the headline check: `queries_run` must be `[]`, `analysis.workspace_sql` (or `python_code`) must be non-empty, and `analysis.coverage.reason` must name the cube it reused. **A warehouse query here means the Data Manager is not doing its job** — the previous behaviour was two SQLs and no reuse, and this is the exact regression to watch.

6. **Reconcile the two answers.** This is the step that proves the whole design, and the one thing no earlier version of this system could do at all:
   ```bash
   curl -s -X POST "localhost:8000/stakeholder/$T/conversations/c1/reconcile" \
     -H 'content-type: application/json' \
     -d '{"answer_a":"<msg_id_1>","answer_b":"<msg_id_2>","measure":"revenue"}' | python3 -m json.tool
   ```
   `same_population` must be `true`, `agrees` must be `true`, and `explanation` must read like something you would forward to a stakeholder. Now ask a third question with a **different filter** (*"revenue in Germany"*) and reconcile it against the first: still `same_population: true`, because a filter is a slice and not a population, and `agrees: true` at the intersected slice. **If a slice changes the population hash, the design has been implemented wrong** — go back to Task 7's canonicalisation.

7. **Confirm the Parquet landed in the right tenant, and only there:**
   ```bash
   find "${ANALYTICS_DATA_DIR:-.}/tenants" -name '*.parquet' -newermt '-10 minutes'
   ```
   Every path must contain the tenant you were chatting as; no other tenant's directory may have gained a file.

8. **Restart and reopen.** Run `./start_session.command` again, reopen the same conversation, ask another re-cut. It must still answer with zero warehouse queries — this proves the workspace rebuilds its DuckDB views from disk, and is impossible today.

9. **Ask for a dimension the cube does not carry** (*"and by service line?"*). Confirm `analysis.coverage.decision == "widen"`, that the new SQL groups by the **union** of old and new dimensions (not just the new one), that `analysis.supersedes` names `df_1`, that both cubes survive on disk, and that the new cube's `population_hash` is unchanged. Then reconcile the widened answer against the very first one — same population, agreeing numbers. That is the property that makes widening safe: no earlier answer is invalidated.

10. **Ask a question whose date range extends past the cube** (*"and how did that look in July?"*). This gap **is** fetchable in isolation: confirm the new SQL carries only the July window, and that both `df_1` and the new cube survive.

11. **Ask about a measure you have deliberately NOT defined** (*"what is our churn rate?"*). Confirm a caveat says it is not a defined metric and `analysis.unresolved_terms` contains it. An analyst that confidently invents a churn definition is the failure mode this whole layer exists to prevent.

12. **Exercise the attribution path.** Run `POST /knowledge/$T/attribution/propose` and confirm the junior raised a DRAFT rule for the multi-valued column you know about, naming its fan-out. Approve one with a real business ranking. Then confirm:
    - the approved rule appears in the rendered context block, and the DRAFT ones do not
    - a base view proposed **after** approval carries a `ROW_NUMBER() OVER (PARTITION BY session_id …)` CTE joined back on `rn = 1` whose `CASE` reflects your ranking — not `GROUP BY session_id, service_line`
    - that proposed base passes its grain probe. If it does not, the attribution CTE is producing more than one row per key, which is the exact bug the probe exists to catch.
    - every answer over an attributed base carries the attribution caveat, **including a pure `reuse` turn that ran no SQL** — the number still depends on the ranking

13. **Ask a question needing a large cube**, wide enough that `estimated_cells` exceeds `max_transport_rows`. Confirm in `tmp/api.log` that it took **more than one round trip**, that no page used `OFFSET`, and that the row count in `extract_meta` is the sum of the pages. Then push it past `raw_extract_row_limit` and confirm `extract_meta.truncated` is true **and** a truncation caveat appears in the answer text.

14. **Exercise day one.** In a fresh tenant with no base views, ask an analytical question. Confirm the planner proposed one, it was stored as DRAFT (`GET /knowledge/$T/base-views?approved_only=false`), it is at **ID grain** and not at dimensional grain, `analysis.base_view_approved` is `false`, and the answer carries the *provisional* caveat. Then approve it and ask again — the caveat must disappear and the `population_hash` must be unchanged, because approval is a status change and not an edit.

15. **Ask a question with no plausible base** (an operational lookup) and confirm the honest degradation: `analysis.reconcilable` is `false` and the answer says it cannot be reconciled against the others. Then try to reconcile it against a cube answer and confirm the endpoint explains why rather than returning a number.

16. **Check `tmp/api.log`** for the sandbox: the largest cube you produced must not raise `MemoryError`. If it does, `EXTRACT_MEMORY_MB` is too low for this dataset — raise it in `config.py` rather than shrinking the ceiling.

**Cost note to watch:** `_plan_turn` adds one LLM call per turn, and the retrieve path adds an analysis call that turn 1 did not previously make. Against that, the cube path *removes* the SQL-synthesis call entirely — `compose_cube` is deterministic — so a retrieve turn is roughly cost-neutral and a `reuse` turn is dramatically cheaper. The semantic + base-view + schema block makes every prompt materially larger; a wide table with several capped value lists can add a few thousand tokens per call. Compare `cost` across a 4-turn conversation before and after; the crossover should land by turn 2. If the context block dominates, the levers are the 8-table cap and `PROFILE_TOP_VALUES` — **not** dropping the schema, and never the semantics or the base views.

---

## What Plan B picks up

Plan A ends with a fully functional analyst behind an unchanged, hand-rolled chat UI. Plan B — written after this plan is approved — covers:

1. **assistant-ui replaces `StakeholderChat.tsx`.** Thread, composer, markdown, message primitives, and the collapsible tool/evidence disclosures come from the library. `ChartRenderer.tsx` and the conversation sidebar behaviour are the parts worth carrying over.
2. **SSE step events.** `answer()` becomes a generator yielding the named steps this plan already carved out (`understanding → planning → checking workspace → retrieving → analysing → interpreting`), served at `GET /stakeholder/{t}/answer/stream` and driving assistant-ui's `ExternalStoreRuntime`. The existing blocking `POST /answer` stays as a thin wrapper over the generator so nothing already built breaks.
3. **The disclosure surface** — "▸ Data used / ▸ SQL / ▸ Analysis code / ▸ Methodology" — rendered from the `analysis` artifact this plan records. Transparency without clutter: the answer is prose, everything else is one click away. The population fields are part of this: which base view an answer rests on, its `population_hash` and `projection_hash`, the slice filters applied, whether the base was approved (a **provisional** badge when it was not), and what the answer supersedes.
4. **The reconcile affordance.** Two answers in a thread, selected, and a "do these agree?" action over Task 15's endpoint — rendering `same_population`, the two values, and the `explanation` verbatim. This is the most visible payoff of Plan A's base views and the one thing a stakeholder can act on directly, so it is worth a real surface rather than a debug panel. An answer whose `reconcilable` is `false` shows why instead of offering the action.
5. **Chart rendering** from `analysis.chart_spec`.
6. **Storyline selection and generation** — turn-level checkboxes over the conversation, then a narrative pass that sequences and de-duplicates the selected findings rather than concatenating them, reusing the existing `storyline.py` assembly and `.docx`/Markdown renderers, plus PDF and PPTX. Selected turns that do not share a `population_hash` should be flagged before export, not silently stitched into one narrative — a report whose figures come from two populations is exactly the artifact Plan A exists to make impossible.

Nothing in that list is blocked on anything except Plan A's payload, which Task 15 is responsible for making complete.
