# Raw Extract + Python Analysis Loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Stakeholder Analyst extract raw, ID-grain data via SQL once, persist it per tenant as Parquet, and answer both the first question and every follow-up by running Python over that extract — with the extract visible and downloadable in the UI.

**Architecture:** A new LLM planning step chooses an *extraction grain* (session_id / user_id / order_id / …) before SQL is written, biased toward extracting rather than aggregating. The extract is written to Parquet under the tenant's own directory and registered in the existing `ConversationDataCache`, which becomes a memory-hot / disk-durable two-layer store. `answer()` is restructured so the extract path always ends in a Python cell, so even turn 1 shows a SQL + Python trail. The Python sandbox loads large frames from Parquet inside the child process instead of pickling them through a pipe.

**Tech Stack:** Python 3 / FastAPI / pandas / pyarrow 24.0.0 (already a dependency) / SQLite per tenant / Next.js + Zustand frontend.

---

## Context

The Stakeholder Analyst was specified to work in two stages: the LLM writes a query, the **raw data is downloaded**, and then — for the initial question and every follow-up — the LLM decides whether it needs *new SQL* (data it doesn't have) or *Python over the data it already has* (a different cut of the same rows). In a recent real test run only two SQL queries appeared, no raw data was found anywhere, and there was no trace of Python. The SQL itself was a pre-aggregated answer.

Root cause, confirmed by reading the code:

1. **`analytics_platform/stakeholder.py:617-627`** — the `_synthesize_sql` system prompt says "write a query to answer the user's question." It never mentions grain or raw rows, so the LLM correctly emits `SELECT country, SUM(revenue) … GROUP BY country`. The cached result is an answer, not data.
2. **`stakeholder.py:550-593`** — `_choose_compute_path` is not broken, it is *starved*. It returns `("sql", "")` immediately when nothing is cached, and when something *is* cached it only sees `label / description / columns`. Given a frame that is already the answer to turn 1, a follow-up like "break that down by service line" genuinely cannot be computed from it — so `"sql"` is the right answer, and that is exactly why two SQLs and zero Python appeared.
3. **`analytics_platform/execution/dataframe_cache.py:5`** — "Never persisted to disk." There is no raw artifact to find, and it evaporates on restart, so reopening a conversation can never use Python.
4. Only `exec_res.data.head(3)` (`stakeholder.py:328-334`) ever reaches the synthesis LLM, and `_synthesize_python`'s prompt (`stakeholder.py:704-714`) explicitly forbids returning the raw frame.

The intended outcome: one grain-level SQL extract per data need, persisted and downloadable, with subsequent slicing done in Python — far cheaper than a fresh warehouse round trip per follow-up.

## Decisions taken (confirmed with the user)

| Decision | Choice |
|---|---|
| When to extract at grain | LLM decides, **grain-first bias**; aggregate only when it explicitly judges grain pointless |
| Where the extract lives | **Parquet on disk, per tenant**, with the in-memory cache as a hot layer |
| Row ceiling for extracts | **1,000,000 rows** (aggregate path keeps the existing 50,000) |
| UI surface | **Schema panel + CSV download** |

## Global Constraints

- **Tenant isolation is filesystem-level.** Every tenant is a different company with its own SQLite file. Extract Parquet files go under `<tenants_dir>/<tenant_id>/extracts/…` where `tenants_dir` is `Settings.tenants_dir` (`analytics_platform/config.py:77-78`). A `tenant_id` column or filter is **not** isolation. Never build a path from unsanitized `tenant_id` / `conversation_id` / `label` — validate each against `^[A-Za-z0-9_-]{1,64}$` before it touches a path.
- **Commit directly to `main`.** No feature branches, no worktrees.
- `RAW_EXTRACT_ROW_LIMIT = 1_000_000`. The existing `default_row_limit = 50000` (`config.py:12`) stays as-is for the aggregate path.
- This repo has **no httpx and no TestClient**. API tests use `call(app, method, path_template, tenant, *body)` from `tests/test_api.py`, which invokes route closures directly and therefore **bypasses middleware**.
- `analytics_platform/api.py` uses **relative imports** (`from .storyline import …`).
- `frontend/src/store/useStore.ts` is `create<AppState>((set) => ({…}))` — no `get` destructured; actions read state via `useStore.getState()` and try/catch with `console.error(e)`.
- Full suite must stay green: `.venv/bin/python -m pytest tests/ -q` (currently 478 passed, 1 skipped) and `cd frontend && npx tsc --noEmit` (0 errors).

---

## File Structure

**Create**
- `analytics_platform/execution/extract_store.py` — Parquet-backed durable store for conversation extracts + JSON sidecar manifests. Sole owner of extract paths and path validation.
- `tests/test_extract_store.py`
- `tests/test_extract_flow.py` — end-to-end: extract turn → follow-up Python turn.

**Modify**
- `analytics_platform/execution/dataframe_cache.py` — accept an optional `ExtractStore`; write through on `put`, read through on `get`/`list_available`; carry grain + truncation + sample in `describe()`.
- `analytics_platform/execution/python_sandbox.py` — accept `dataframe_paths` so the child loads Parquet itself; configurable memory/timeout.
- `analytics_platform/stakeholder.py` — the bulk: `_plan_turn` (replaces `_choose_compute_path`), `_synthesize_extract_sql`, restructured `answer()`, extract metadata on `_record`.
- `analytics_platform/config.py` — `raw_extract_row_limit`, `extract_retention_days`, sandbox memory/timeout settings.
- `analytics_platform/execution/browser_session.py:159,165,305-306` — `max_rows` must not silently cap an extract at 50,000.
- `analytics_platform/database.py` (~line 227) — additive `extract_meta` column migration.
- `analytics_platform/api.py` — CSV download route; extract metadata in conversation replay.
- `frontend/src/store/useStore.ts`, `frontend/src/components/StakeholderChat.tsx` — extract panel + download action.

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

### Task 4: Raise the extract row ceiling to 1,000,000

**Files:**
- Modify: `analytics_platform/config.py:12` area (add settings, do not change `default_row_limit`)
- Modify: `analytics_platform/execution/browser_session.py:159,165,305-306`
- Test: `tests/test_execution_policy.py` (or the existing policy test module — find it with `grep -rln "QueryPolicy" tests/`)

**Interfaces:**
- Produces, on the policy settings dataclass in `config.py`:
  ```python
  raw_extract_row_limit: int = 1_000_000
  extract_retention_days: int = 30
  ```
  `QueryPolicy.validate(sql, allowed_tables=…, dialect=…, row_limit=…)` already accepts `row_limit` (`execution/policy.py:112`) — no signature change, callers just pass it.

**The trap:** an extract is capped in **three** independent places and all three must agree, or a 1,000,000-row request silently returns 50,000 and the LLM computes a confidently wrong total:
1. `execution/policy.py:112-121` injects `LIMIT {default_row_limit}` into any plain `SELECT` that lacks one.
2. `ExecutionContext.row_limit` defaults to `50000` (`execution/base.py:29`) and `execution/sampler.py:69-70` does `df.head(ctx.row_limit)`.
3. `browser_session.py` has its own `max_rows: int = 50000` (lines 159, 165, 305-306).

- [ ] **Step 1: Write the failing test**

```python
def test_extract_row_limit_is_injected_when_requested():
    policy = QueryPolicy(PolicySettings())
    d = policy.validate("SELECT session_id, revenue FROM orders WHERE dt >= '2026-01-01'",
                        row_limit=1_000_000, dialect="athena")
    assert d.allowed and "LIMIT 1000000" in d.approved_sql

def test_default_path_still_limits_to_50000():
    policy = QueryPolicy(PolicySettings())
    d = policy.validate("SELECT country, SUM(revenue) FROM orders WHERE dt >= '2026-01-01' GROUP BY country",
                        dialect="athena")
    assert "LIMIT 50000" in d.approved_sql
```

- [ ] **Step 2: Run to verify it fails / passes as expected**

Run: `.venv/bin/python -m pytest tests/ -q -k "policy"`
Expected: the second test passes today; the first passes too if `row_limit` is honoured — **if it does, keep both as regression guards and move on.** The real work of this task is items 2 and 3 below.

- [ ] **Step 3: Implement**

- Add `raw_extract_row_limit` and `extract_retention_days` to the policy settings dataclass in `config.py`, and read both from env in the `from_env`-style constructor at `config.py:96` following the existing pattern for `default_row_limit`.
- `browser_session.py`: make `max_rows` default to `None`, meaning "take it from `ctx.row_limit`". Where it currently truncates (lines 305-306), truncate to `ctx.row_limit` instead of a hardcoded 50,000, and when truncation happens **append a warning to `QueryResult.warnings`** (the field already exists, `execution/base.py:41`) reading `"result truncated at N rows"`. Callers depend on that warning to set `truncated`.
- Do **not** change `ExecutionContext.row_limit`'s default of 50000 — the extract path passes `row_limit=` explicitly.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (478+, 1 skipped)

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/config.py analytics_platform/execution/browser_session.py tests/ && git commit -m "feat(execution): honour a per-request row limit end to end, 1M ceiling for extracts"
```

---

### Task 5: `_plan_turn` — one router, three outcomes

**Files:**
- Modify: `analytics_platform/stakeholder.py:550-593` (replace `_choose_compute_path`)
- Test: `tests/test_stakeholder.py` (extend)

**This is the change that fixes the observed behaviour.** Today's router only ever picks between `python` and `sql`, sees nothing but column names, and short-circuits to `"sql"` when the cache is empty. It is replaced by a single planning call that also decides the *grain* when new data is needed.

**Interfaces:**
- Consumes: `ConversationDataCache.list_available` (now carrying `grain`, `truncated`, `sample` — Task 2).
- Produces:
  ```python
  @dataclass
  class TurnPlan:
      path: str            # "python" | "extract" | "aggregate"
      df_label: str = ""   # set when path == "python"
      grain: List[str] = field(default_factory=list)   # set when path == "extract"
      dimensions: List[str] = field(default_factory=list)
      measures: List[str] = field(default_factory=list)
      time_window: str = ""
      rationale: str = ""

  def _plan_turn(self, llm, tenant_id: str, conversation_id: str, question: str,
                 query_nodes: List[Any], defn_nodes: List[Any]) -> TurnPlan: ...
  ```
  Put `TurnPlan` in `analytics_platform/domain.py` beside the other shared dataclasses, not in `stakeholder.py`.

**Prompt requirements (write these verbatim into the system prompt):**
- Strict JSON only: `{"path": "python"|"extract"|"aggregate", "df_label": "", "grain": [], "dimensions": [], "measures": [], "time_window": "", "rationale": ""}`.
- `"python"` — the question can be fully answered by computing over one of the DataFrames listed below. **Prefer this whenever it is possible**; re-querying the warehouse for a cut of data already in hand is wasteful.
- `"extract"` — new data is needed. Choose the **finest ID grain that makes the question answerable and stays under the row ceiling** (`session_id`, `user_id`, `order_id`, `guest_id`, or an ID crossed with a date/dimension). Extracting at grain is strongly preferred, because follow-up questions can then be answered in Python without another query.
- `"aggregate"` — new data is needed **and** grain-level extraction would serve no purpose (a single scalar KPI, or a grain that would obviously exceed the ceiling). Justify it in `rationale`.
- The listed frames come with `grain`, `row_count`, `truncated`, `columns`, and a 3-row `sample` — use the sample to judge whether a column really carries what its name suggests.
- If a frame is marked `truncated: true`, do not use it to compute totals, counts, or rates over the whole population; choose `extract` instead.

**Behaviour:**
- When nothing is cached, **still call the LLM** (this is the key difference from today's early return) — it must choose between `extract` and `aggregate`, and it must pick the grain.
- Default on any parse failure, unknown `df_label`, or LLM error: `TurnPlan(path="extract")` with an empty grain. Empty grain in the extract path means "the SQL prompt asks for row-level detail without a named key" — still far better than today's aggregate default. Log the parse failure with the raw text at WARNING.

- [ ] **Step 1: Write the failing tests**

Use the existing mock-LLM sequencing pattern in `tests/test_stakeholder.py`. **Critical:** `classify()` is a pure keyword heuristic with no LLM call, and the *first* `llm.generate()` in `answer()` is `_extract_search_intent`. Count mock responses from there.

```python
def test_plan_turn_picks_python_when_a_frame_can_answer(stakeholder, tmp_cache):
    tmp_cache.put("acme", "c1", "df_1", "orders by session",
                  pd.DataFrame({"session_id": ["a"], "svc_line": ["x"], "revenue": [1]}),
                  meta=_meta("df_1", grain=["session_id"]))
    llm = MockLLM(['{"path":"python","df_label":"df_1"}'])
    plan = stakeholder._plan_turn(llm, "acme", "c1", "break that down by service line", [], [])
    assert plan.path == "python" and plan.df_label == "df_1"

def test_plan_turn_calls_the_llm_even_with_an_empty_cache(stakeholder):
    llm = MockLLM(['{"path":"extract","grain":["session_id"],"dimensions":["country"]}'])
    plan = stakeholder._plan_turn(llm, "acme", "c1", "how did revenue trend?", [], [])
    assert plan.path == "extract" and plan.grain == ["session_id"]
    assert llm.calls == 1          # regression guard: today this returns without calling

def test_plan_turn_rejects_an_unknown_df_label(stakeholder):
    llm = MockLLM(['{"path":"python","df_label":"df_99"}'])
    assert stakeholder._plan_turn(llm, "acme", "c1", "q", [], []).path == "extract"

def test_plan_turn_defaults_to_extract_on_garbage(stakeholder):
    assert stakeholder._plan_turn(MockLLM(["not json at all"]), "acme", "c1", "q", [], []).path == "extract"
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
git add analytics_platform/stakeholder.py analytics_platform/domain.py tests/test_stakeholder.py && git commit -m "feat(stakeholder): grain-aware turn planner replaces compute-path router"
```

---

### Task 6: Grain-first SQL synthesis

**Files:**
- Modify: `analytics_platform/stakeholder.py:595-643` (`_synthesize_sql`) and `:645-688` (`_synthesize_and_execute_sql`)
- Test: `tests/test_stakeholder.py` (extend)

**Interfaces:**
- Consumes: `TurnPlan` from Task 5.
- Produces: `_synthesize_sql(..., plan: Optional[TurnPlan] = None, …)` and `_synthesize_and_execute_sql(..., plan: Optional[TurnPlan] = None, …)`. With `plan is None` or `plan.path == "aggregate"`, behaviour is byte-for-byte today's — that is what keeps every existing test green.

- [ ] **Step 1: Write the failing tests**

```python
def test_extract_plan_asks_for_grain_level_rows(stakeholder):
    llm = RecordingLLM(["```sql\nSELECT session_id, country, revenue FROM orders\n```"])
    plan = TurnPlan(path="extract", grain=["session_id"], dimensions=["country"], measures=["revenue"])
    stakeholder._synthesize_sql(llm, "revenue by country", [], [], plan=plan)
    sys_prompt = llm.last_system_prompt
    assert "one row per session_id" in sys_prompt
    assert "GROUP BY" in sys_prompt          # the prompt names it as the thing to avoid

def test_aggregate_plan_keeps_the_original_prompt(stakeholder):
    llm = RecordingLLM(["```sql\nSELECT 1\n```"])
    stakeholder._synthesize_sql(llm, "q", [], [], plan=TurnPlan(path="aggregate"))
    assert "one row per" not in llm.last_system_prompt

def test_extract_execution_uses_the_million_row_ceiling(stakeholder, spy_executor):
    stakeholder._synthesize_and_execute_sql(
        RecordingLLM(["```sql\nSELECT session_id FROM orders\n```"]),
        "acme", "q", [], [], plan=TurnPlan(path="extract", grain=["session_id"]))
    assert spy_executor.last_ctx.row_limit == 1_000_000
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -q -k "grain or extract_execution"`
Expected: FAIL — `TypeError: unexpected keyword argument 'plan'`

- [ ] **Step 3: Implement**

In `_synthesize_sql`, when `plan.path == "extract"`, **append** this block to the existing `sys_prompt` (keep every existing sentence, especially the `{{...}}` placeholder rules — they are load-bearing against Metabase templates):

```
Return ROW-LEVEL DETAIL, not a summary. Emit exactly one row per
{grain_clause}, carrying the identifier column(s), the dimension columns
{dimensions}, and the measure columns {measures} at their un-aggregated
row level. Do NOT use GROUP BY, and do NOT wrap measures in SUM/COUNT/AVG
or any other aggregate -- the downstream step aggregates this data in
pandas, and an aggregated result there is useless. Apply the narrowest
correct filters, especially a date filter{time_window_clause}, so the
result stays under {row_limit} rows.
```

Where `grain_clause` is `" and ".join(plan.grain)` or, when `plan.grain` is empty, `"row of the underlying fact table"`.

In `_synthesize_and_execute_sql`:
- Pass `row_limit=self.settings.policy.raw_extract_row_limit` to `policy.validate(...)` when `plan.path == "extract"`, otherwise omit it as today.
- Build `ExecutionContext(tenant_id=…, question=…, dialect="athena", row_limit=<same value>)`. Note line 686 currently hardcodes `dialect="athena"` while `_synthesize_sql` uses `self.settings.source_dialect` — leave that inconsistency alone, it is out of scope; do not "fix" it here.
- The retry-on-failure loop is unchanged; the `plan` just rides along into each attempt.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/stakeholder.py tests/test_stakeholder.py && git commit -m "feat(stakeholder): synthesize grain-level extract SQL when the plan calls for it"
```

---

### Task 7: Restructure `answer()` around the extract loop

**Files:**
- Modify: `analytics_platform/stakeholder.py:248-360` (`answer`), `:690-728` (`_synthesize_python`), `:730-778` (`_synthesize_and_execute_python`), `:817-852` (`_record`)
- Modify: `analytics_platform/database.py` (~line 227, additive migration)
- Test: Create `tests/test_extract_flow.py`

**Interfaces:**
- Produces: `_record(..., extract_meta: Optional[Dict[str, Any]] = None)`; the answer dict and the `stakeholder_answers` row gain `extract_meta`.
- New column: `ALTER TABLE stakeholder_answers ADD COLUMN extract_meta TEXT` — follow the exact `if "produced_df_label" not in sa_cols:` pattern at `database.py:227-228`.

**The new control flow in `answer()`,** replacing the current `_choose_compute_path` gate at line 285 and the SQL block at 314-360:

```
plan = self._plan_turn(...)                      # when llm live and conversation_id

if plan.path == "python":
    run Python over plan.df_label  →  on success, synthesize + return  (unchanged)
    on failure, fall through with plan.path = "extract"

if plan.path == "extract":
    sql, exec_res = self._synthesize_and_execute_sql(..., plan=plan)
    on success:
        label = data_cache.next_label(...)
        meta  = ExtractMeta(grain=plan.grain, row_count=len(df),
                            truncated=<row_count >= raw_extract_row_limit
                                       or "truncated" in any exec_res.warnings>,
                            sql=sql, ...)
        data_cache.put(..., label, question[:200], exec_res.data, meta=meta)
        code, py_res, toks = self._synthesize_and_execute_python(
            llm, tenant_id, conversation_id, question, label)      # ← turn 1 now runs Python
        if py_res is not None and py_res.ok:
            → synthesize from py_res.result_summary
            → _record(queries_run=[sql], python_cells=[…], produced_df_label=label,
                      extract_meta=meta_dict)
            return
        # Python over a good extract failed: still answer, from head(3) of the
        # extract, exactly as today -- but keep queries_run, produced_df_label
        # and extract_meta so the raw data is still downloadable.
    on failure: fall through to plan.path = "aggregate"

# aggregate: today's existing SQL block verbatim, plan=None
```

**Also required in this task:**
- `_synthesize_python`'s system prompt (line 704-714) must gain: *"The DataFrame is a raw, row-level extract — you are expected to aggregate, group, filter, and pivot it to answer the question."* Keep the existing "not the full raw DataFrame unmodified" sentence; the `MAX_RESULT_ROWS = 20` cap makes returning the raw frame useless anyway.
- `_synthesize_and_execute_python` must call `run_python_sandboxed` with `dataframe_paths={df_label: path}` when the cache's store has a Parquet path for the label, and `memory_mb=EXTRACT_MEMORY_MB, timeout_s=EXTRACT_TIMEOUT_S`. Fall back to the in-memory `dataframes=` form when there is no path.
- **Caveats must be honest.** When `meta.truncated` is true, append the caveat `"extract truncated at N rows -- totals and rates may be understated"` to every answer computed from that frame, on the extract turn and on every later Python turn.

- [ ] **Step 1: Write the failing end-to-end test**

```python
# tests/test_extract_flow.py
def test_first_turn_extracts_raw_rows_and_answers_in_python(tenant, tmp_path):
    """The exact scenario the user reported: one question, and the trail must
    show a grain-level SQL extract AND a Python cell -- not a lone aggregate."""
    llm = SequencedLLM([
        "revenue by country",                                            # _extract_search_intent
        '{"path":"extract","grain":["session_id"],"dimensions":["country"]}',   # _plan_turn
        "```sql\nSELECT session_id, country, revenue FROM orders WHERE dt >= '2026-01-01'\n```",
        "```python\nresult = df_1.groupby('country')['revenue'].sum().to_dict()\n```",
        "Revenue is concentrated in IN and US.",                         # _synthesize
    ])
    out = service.answer(tenant, "what is revenue by country?", conversation_id="c1")

    assert len(out["queries_run"]) == 1
    assert "GROUP BY" not in out["queries_run"][0].upper()      # it is an extract, not a summary
    assert len(out["python_cells"]) == 1                        # the missing trail, now present
    assert out["produced_df_label"] == "df_1"
    assert out["extract_meta"]["grain"] == ["session_id"]
    assert out["extract_meta"]["row_count"] > 0

def test_follow_up_slices_the_extract_with_no_new_sql(tenant, tmp_path):
    ... first turn as above ...
    llm2 = SequencedLLM([
        "service line breakdown",
        '{"path":"python","df_label":"df_1"}',
        "```python\nresult = df_1.groupby('svc_line')['revenue'].sum().to_dict()\n```",
        "Service line A leads.",
    ])
    out2 = service.answer(tenant, "break that down by service line", conversation_id="c1")
    assert out2["queries_run"] == []                            # no warehouse round trip
    assert len(out2["python_cells"]) == 1

def test_extract_survives_a_cold_cache(tenant, tmp_path):
    """Reopening a conversation in a fresh process must still be Python-capable."""
    ... first turn ...
    service2 = build_service(tenant, tmp_path)                  # fresh in-memory cache, same disk
    out = service2.answer(tenant, "now by month", conversation_id="c1")
    assert out["queries_run"] == []
    assert len(out["python_cells"]) == 1

def test_truncated_extract_produces_a_visible_caveat(tenant, tmp_path):
    ... executor returns exactly raw_extract_row_limit rows ...
    assert any("truncated" in c for c in out["caveats"])
    assert out["extract_meta"]["truncated"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_extract_flow.py -q`
Expected: FAIL — `KeyError: 'extract_meta'`, and `python_cells` empty on turn 1.

- [ ] **Step 3: Implement**

Order: migration in `database.py` first, then `_record`, then `_synthesize_python` prompt + sandbox path wiring, then the `answer()` restructure last. Construct the `ExtractStore` where `ConversationDataCache` is constructed today (`grep -rn "ConversationDataCache(" analytics_platform`) using `settings.tenants_dir`.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/stakeholder.py analytics_platform/database.py tests/test_extract_flow.py && git commit -m "feat(stakeholder): extract raw rows then analyse them in Python, from turn one"
```

---

### Task 8: CSV download endpoint + conversation replay

**Files:**
- Modify: `analytics_platform/api.py` (near the stakeholder block at 1041-1090)
- Modify: `analytics_platform/stakeholder.py:163-183` (`get_conversation`) and `:205-215` (`delete_conversation`)
- Test: `tests/test_api.py` (extend)

**Interfaces:**
- Produces:
  - `GET /stakeholder/{tenant_id}/conversations/{conversation_id}/extracts/{label}/download` → `Response` with `text/csv` and a `Content-Disposition` header.
  - `get_conversation` messages gain `"extract_meta": load_json(r["extract_meta"], {})`.
  - `delete_conversation` also calls `extract_store.delete_conversation(...)` — deleting a chat must delete its raw data.

**Reuse, do not reinvent:** the storyline export route (`api.py` ~1078-1120) already solved the filename problem. Copy its dual-form pattern verbatim — an ASCII slug for `filename=` plus RFC 5987 `filename*=UTF-8''…`, bounded to 60 chars — because Starlette encodes response headers as latin-1 and a raw unicode question title raises. Also copy its `expose_headers` handling: `Content-Disposition` is **not** CORS-safelisted, so the browser cannot read it unless the CORS middleware exposes it.

**Errors:** unknown tenant → the existing `tenant_or_404`; unknown conversation or label → 404; a `ValueError` from `ExtractStore`'s id validation → 400 (never let it become a 500, and never let it reach the filesystem).

- [ ] **Step 1: Write the failing tests**

```python
def test_download_returns_csv_for_a_real_extract(app, tenant):
    ...produce an extract via the extract flow...
    r = call(app, "GET", "/stakeholder/{t}/conversations/c1/extracts/df_1/download", tenant)
    assert r.media_type == "text/csv"
    assert r.body.decode().splitlines()[0] == "session_id,country,revenue"
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
```

Remember: `call()` invokes route closures directly and **bypasses middleware**, so it cannot prove the CORS `expose_headers` change works. Verify that part manually in the browser step below.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_api.py -q -k "download or deleting_a_conversation"`
Expected: FAIL — route not found.

- [ ] **Step 3: Implement**

Stream with `df.to_csv(index=False)` into a `Response`. At 1,000,000 rows this is a large body — that is the accepted cost of "let me download the raw data"; do not silently sample it. If `ExtractMeta.truncated` is true, that fact is already visible in the UI panel; do not alter the CSV.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/api.py analytics_platform/stakeholder.py tests/test_api.py && git commit -m "feat(api): download a conversation's raw extract as CSV"
```

---

### Task 9: Extract panel + download in the chat UI

**Files:**
- Modify: `frontend/src/store/useStore.ts`
- Modify: `frontend/src/components/StakeholderChat.tsx` (near the existing `CollapsibleCode` blocks at 248-260)

**Interfaces:**
- Consumes: `extract_meta` on each message (Task 8) and the download route.
- Produces: `downloadExtract(conversationId: string, label: string): Promise<void>` on the stakeholder slice, and an `ExtractPanel` rendered above the existing SQL/Python blocks.

**Follow the file's existing conventions exactly:** the store is `create<AppState>((set) => ({…}))` with **no `get`** destructured — read state via `useStore.getState()`. Async actions use try/catch with `console.error(e)`. There is no shared base-URL constant; build the URL the same way the sibling actions do. Reuse the `exportError` pattern added for the Report Builder: set an `extractError` string from the response `detail`/`statusText` and render it with `role="alert"` — an export that fails silently was already fixed once here, do not reintroduce it.

The panel shows, for any message with a non-empty `extract_meta`:
- `Raw data extracted — one row per {grain.join(" × ")}`, or `"row level"` when grain is empty
- `{row_count.toLocaleString()} rows × {columns.length} columns`
- a warning chip when `truncated` is true: `Truncated at {row_count.toLocaleString()} rows — totals may be understated`
- the column list, collapsed by default
- a **Download CSV** button calling `downloadExtract`

- [ ] **Step 1: Add the type and the store action**

Extend the message type alongside the existing `queries_run` / `python_cells` fields with `extract_meta?: { label: string; grain: string[]; columns: string[]; row_count: number; truncated: boolean }`.

- [ ] **Step 2: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: 0 errors

- [ ] **Step 3: Render the panel**

- [ ] **Step 4: Typecheck again and verify in the browser**

Run: `cd frontend && npx tsc --noEmit`, then follow the Verification section below.

- [ ] **Step 5: Commit**

```bash
git add frontend/src && git commit -m "feat(frontend): show the raw extract and let the user download it"
```

---

### Task 10: Retention sweep

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

Expected: 478 pre-existing tests still pass (1 skipped), plus the new `tests/test_extract_store.py`, `tests/test_extract_flow.py`, and the additions to `test_dataframe_cache.py`, `test_python_sandbox.py`, `test_stakeholder.py`, `test_api.py`.

```bash
cd frontend && npx tsc --noEmit
```

Expected: 0 errors.

**End-to-end, against the real tenant**

```bash
./start_session.command
```

This kills any running session and boots the backend on :8000 and the UI on :3000.

Then, in the Stakeholder Analyst:

1. Ask a genuinely analytical first question (the same kind as the failing test run — e.g. *"how has revenue trended by country this year?"*). Confirm on **turn 1**:
   - the "SQL executed" block contains **no `GROUP BY`** and selects an ID column
   - a **"Python executed"** block is present — this is the trail that was missing
   - the **Raw data** panel shows the grain, row count, and column count
2. Click **Download CSV**. Confirm the file downloads, opens, and has one row per ID at the stated grain and the stated row count.
3. Ask a follow-up that is a pure re-cut of the same data (*"now break that down by service line"*). Confirm **no new SQL block appears** and only a Python block does.
4. Confirm the Parquet landed in the right tenant, and only there:
   ```bash
   find "${ANALYTICS_DATA_DIR:-.}/tenants" -name '*.parquet' -newermt '-10 minutes'
   ```
   Every path must contain the tenant you were chatting as, and no other tenant's directory may have gained a file.
5. Restart the session (`./start_session.command` again), reopen the same conversation, ask another re-cut question. Confirm it still answers with Python and no SQL — this proves the disk layer, and is impossible today.
6. Ask a deliberately huge question likely to hit the ceiling. Confirm the truncation chip appears in the panel **and** a truncation caveat appears in the answer text.
7. Check `tmp/api.log` (or `/tmp/ai_analytics_api.log`) for the sandbox: a 1M-row cell must not raise `MemoryError`. If it does, `EXTRACT_MEMORY_MB` is too low for this dataset — raise it in `config.py` rather than shrinking the ceiling.

**Cost note to watch during step 1:** `_plan_turn` adds one LLM call per turn, and the extract path adds a Python synthesis call that turn 1 did not previously make. In exchange, every follow-up that used to cost a full SQL round trip now costs one Python cell. Compare `cost` across a 4-turn conversation before and after; the crossover should land by turn 2.
