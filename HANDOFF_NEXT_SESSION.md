# HANDOFF — Next Session

Prepared: 2026-08-07 · Repo: `/Users/abhinav.gupta/Documents/ai_analytics_advanced`
Goal: standalone, company-independent AI analytics copilot (see `STANDALONE_ANALYTICS_PLATFORM_PLAN.md`).

## State
- **HEAD `f6d74b3` (CP-L8), tree clean.** Original `AI analytics/` folder untouched.
- **103/103 standalone tests pass** (all `tests/` modules except the legacy `test_ui_and_db`), plus the
  **3 live tests (2 `MetabaseLive` + 1 `TestJuniorMetabaseLive`) PASS when `ANALYTICS_MB_LIVE=1`**
  (skipped otherwise) — **live-CONFIRMED 2026-08-07, 3/3 OK in ~8s**. Run:
  `cd <repo> && .venv/bin/python -m unittest tests.test_brain tests.test_browser_session tests.test_cli
  tests.test_api tests.test_ingest tests.test_integration tests.test_junior tests.test_metabase_live
  tests.test_migration tests.test_onboarding tests.test_pipeline_e2e tests.test_policy tests.test_tenancy
  tests.test_triage  # -> 106 tests (3 live skipped)`
  **Caveat (pre-existing, environmental):** `unittest discover -s tests` hangs on this machine in
  `test_ui_and_db.test_pipeline_with_form_metadata` — `core.IngestionPipeline.run`'s Step-4 Cypher
  generation waits on a Neo4j that isn't reachable here. It is legacy `core.*` code, unrelated to
  `analytics_platform/`, and was left untouched.
- **Live Metabase E2E CONFIRMED (CP-L6 + CP-L8)** — host `metabase.om.yo-digital.com`, DB `59`, browser-
  cookie only (no token). **The junior live path is now confirmed too** (verify anytime):
  `ANALYTICS_MB_LIVE=1 ANALYTICS_MB_DATABASE_ID=59 ANALYTICS_MB_EXPECTED_HOST=metabase.om.yo-digital.com \
   .venv/bin/python -m unittest tests.test_metabase_live -v`
  → 3/3 OK: session valid + read-only `SELECT 1` (`MetabaseLive`) and junior stage‑3
  reproduction/catalog over the live browser executor (`TestJuniorMetabaseLive`).
- Live API verified; **36 routes**, incl. `/triage/*` (summary/queue/conflicts/approve/reject/bulk)
  and `/junior/*` (stage/catalog/datasets/questions/reproduce).

## Progress (Brain v2 migration — in progress)
- **CP1 — mapper (DONE)**: added `NodeKind.IDIOM`; new `analytics_platform/migration/mapper.py`
  (pure: snapshot dict → `list[NodeSpec]`; QUERY + derived DEFINITIONs via `ingest.extract`,
  IDIOM, BUSINESS_RULE, stage/table DEFINITIONs; enriches query reasoning from `intents` by
  `card_id`; all default `CANDIDATE`; stable content-hashed `source_ref`s for idempotency
  even when `rule_type`/`name` collide). Seed fixture `tests/fixtures/snapshot_seed.json`
  + 9 mapper tests. **49/49 tests pass.**
- **CP2 — loader + CLI `migrate` (DONE)**: `analytics_platform/migration/loader.py`
  (`migrate_specs` / `migrate_from_snapshot`; idempotency via `source_ref`; provenance +
  confidence; tenant-scoped). `cli.py` gains `analytics-platform migrate <tenant> --snapshot
  <path> [--no-derive-definitions] [--created-by]`. Loader + gated real-snapshot tests
  (16 migration tests total, **56/56 overall**). Verified on the real snapshot: 1229 nodes
  (158 QUERY / 598 DEFINITION / 180 IDIOM / 293 BUSINESS_RULE), all CANDIDATE, 0 auto-approved;
  CLI is idempotent across runs.
- **CP3 — real migrate + docs (DONE)**: verified real migrate (1229 nodes; 158 QUERY / 598
  DEFINITION / 180 IDIOM / 293 BUSINESS_RULE; all CANDIDATE; **0 auto-approved**; idempotent)
  and CLI end-to-end. README roadmap marked **Brain v2 migration DONE**; this doc refreshed.
  **Brain v2 migration is complete (56/56 tests).**

## Progress — Live Metabase bind (current)
- **CP-L1 — config + factory (DONE)**: `config.py` gained `ANALYTICS_MB_LIVE/HOST/DATABASE_ID/EXPECTED_HOST`
  → `Settings.metabase_live/base_url/database_id/expected_host` (+`from_env`). `BrowserSessionExecutor`
  gained `from_env(runner=...)` classmethod + module `make_live_executor(settings=...)`; exported from
  `execution/`. database_id parsed to int when numeric. **62/62 tests.**
- **CP-L2 — CLI `browser` live command (DONE)**: `cli.py` subcommand `analytics-platform browser
  [--sql ...] [--database-id N] [--expected-host HOST] [--host URL] [--head N]`; prints
  `session_status()` (valid/needs_login/unknown + detail), fails-with-pause before any query,
  runs a read-only query and shows row_count/columns/head. Offline CLI tests in `tests/test_cli.py`
  (5) → **67/67 tests.**
- **CP-L3 — gated live test + docs (DONE)**: `tests/test_metabase_live.py` (`MetabaseLive`,
  skipped unless `ANALYTICS_MB_LIVE=1`; asserts `valid` session + a read-only execute).
  README gained a Live-Metabase "Run it" block + roadmap bullet; this doc refreshed.
  **67/67 offline tests** (live test is skipped here). The final real E2E run happens on your
  machine: `ANALYTICS_MB_LIVE=1 ANALYTICS_MB_DATABASE_ID=.. ANALYTICS_MB_EXPECTED_HOST=.. \
  .venv/bin/python -m unittest discover -s tests -k MetabaseLive -v` (Chrome logged into Metabase).
- **CP-L4 — tab-targeted runner (DONE)**: `browser_session.py` now finds the **Metabase tab by
  URL** across Chrome windows/tabs (via `build_osascript_command(js, host)` + `make_osascript_runner`),
  with active-tab fallback; `rebind_runner()` recomputes after `--host`/`--expected-host` overrides
  (CLI calls it). This fixes the "wrong Chrome profile / wrong active tab → needs_login" case the
  Telekom profile surfaced. Offline tests `TestOsascriptTargeting` (5) → **95 tests (2 skipped).**
  Connection values (from `scripts/`): host `metabase.om.yo-digital.com`, `database_id 59` —
  executor targets the URL-matching tab, no API token needed (cookie-only, same-origin).
- **CP-L5 — async roundtrip fix (DONE)**: the live check exposed that Chrome's AppleScript
  `execute javascript` never awaits promises, so our `(async()=>...)()` returned empty
  ("Active tab is not Metabase ()"). Fixed like `scripts/download_base_tables.py`: async results
  are stashed in `window.__mb` and read back with separate synchronous calls
  (`PROBE_KICK_JS`/`_build_execute_kick_js`, `READ_STATE_JS`, `RESET_JS`, `_run_roundtrip()`).
  Offline stub `make_runner` now mirrors reset→kick→read. **95 tests (2 skipped).**
- **CP-L6 — live Metabase E2E CONFIRMED (DONE)**: two more live bugs fixed — (1) the tab-loop
  AppleScript used a bare `execute t javascript …` statement whose return value osascript
  discards (→ empty); now `return (execute …)`. (2) `fetch` got a raw JS object body → Metabase
  `Unrecognized token 'object'`; payload is now embedded as a JS string literal. Live run green:
  `MetabaseLive` 2/2 **OK** on `metabase.om.yo-digital.com` DB 59 (session valid + `SELECT 1`).
  **96 tests (2 skipped).**

## Progress — Triage (current)
- **CP-T1 — service reads (DONE)**: `analytics_platform/triage.py` — `TriageService`
  (`queue`, `summary`, `conflicts` read-only inbox over the Brain; `ACTIONABLE` =
  CANDIDATE/UNDER_REVIEW/REVISION_REQUIRED). `tests/test_triage.py` (9). **78 tests (2 skipped).**
- **CP-T2 — approve/reject/bulk + CLI `review` (DONE)**: `cli.py` `analytics-platform review
  <tenant> [--kind K] [--limit N] [--approve/--reject id,..] [--bulk-approve/--bulk-reject]
  [--by senior] [--conflicts] [--quiet]`. Service `approve/reject/bulk` (approve = submit-then-
  approve; only ACTIONABLE touched). CLI tests in `tests/test_cli.py` (3) → **81 tests (2 skipped).**
- **CP-T3 — docs + E2E smoke (DONE)**: verified on the real migrated snapshot (1229 CANDIDATEs
  → `review --conflicts` shows value-set Definition conflicts → bulk-approve IDIOM 180 →
  actionable 1229→1049). README gained a Triage run block + "Triage: DONE" roadmap bullet;
  this doc refreshed. **Triage is complete (81 tests, 2 live skipped).**

## Progress — Junior maturity-stage engine (current)
- **CP-J1 — engine core (DONE)**: `analytics_platform/junior.py` — `JuniorEngine` (read-only):
  `approved_queries()` (usable QUERY with sql), `reproduce_metrics()` (runs approved queries via
  injectable executor; reports attempted/reproduced/failed), `stage()` (0 provisioning → 1
  schema/EDA → 2 metric-understanding → 3 process-analysis, requiring reproduced + targets).
  `tests/test_junior.py` (4). **85 tests (2 skipped).**
- **CP-J2 — schema/EDA catalog (DONE)**: `JuniorEngine.catalog()`/`datasets()` — describes
  registered tables via `SELECT * FROM t LIMIT 0` (dialect-agnostic, injectable executor);
  reports columns/types/errors. Tests +2 → **87 tests (2 skipped).**
- **CP-J3 — questions + CLI `junior` (DONE)**: `suggest_questions()` (CompanyProfile.targets ↔
  approved definitions/queries) + `cli.py` `junior <tenant> [--limit N]` (stage + catalog +
  questions). Verified: demo tenant → stage 3, 2/2 reproduced, 1 table described, 2 goal-aligned
  questions. Tests +3 → **90 tests (2 skipped).** **Junior engine is complete.**

## Progress — Junior wired to live Metabase (current)
- **CP-L7 — junior engine over the live `BrowserSessionExecutor` (DONE)**: `cli.py` `junior`
  now picks the executor via `_resolve_junior_executor(settings, offline)` — `make_live_executor()`
  when `ANALYTICS_MB_LIVE=1` (host-guarded, cookie stays in the browser, read-only), else the
  offline `SamplerExecutor`. The exact same `JuniorEngine.stage()/catalog()/reproduce_metrics()`
  runs over whichever executor is injected.
  - Offline seam test: `tests/test_junior.py::test_runs_over_browser_executor_seam_offline` drives
    `JuniorEngine` with a `BrowserSessionExecutor` (stub runner, `metabase.om.yo-digital.com` host
    guard) → stage reproduction + catalog green.
  - CLI selection tests: `tests/test_cli.py::TestCliJunior` asserts `junior` uses the live executor
    when `metabase_live` and the offline one otherwise.
  - Live gated test: `tests/test_metabase_live.py::TestJuniorMetabaseLive` (skipped unless
    `ANALYTICS_MB_LIVE=1`) runs junior stage-3 over real Metabase. **98 tests (3 live skipped).**
  - README: `junior` Run-it block shows both offline and `ANALYTICS_MB_LIVE=1` incantations; new
    roadmap bullet "Junior wired to live Metabase: DONE". *Honest note: not run live here — the
    live path is gated and needs your logged-in Chrome; the offline seam + CLI selection are
    unit-tested and green.*
- **CP-L8 — live run CONFIRMED (DONE)**: with `ANALYTICS_MB_LIVE=1` + `ANALYTICS_MB_DATABASE_ID=59` +
  `ANALYTICS_MB_EXPECTED_HOST=metabase.om.yo-digital.com` and a logged-in Metabase tab,
  `tests.test_metabase_live` ran **3/3 OK (~8s)** — `MetabaseLive` (session valid, read-only
  `SELECT 1`) **and** `TestJuniorMetabaseLive` (an approved query was reproduced and a mapped table
  catalogued through the live browser executor). The CP-L7 junior→live wiring is validated
  end-to-end on real Metabase.

## Progress — API + LLM + UI road (current)
- **CP-X1 — expose triage + junior as FastAPI endpoints (DONE)**: `api.py` gained `/triage/{tenant_id}`
  (summary, queue, conflicts, approve, reject, bulk) and `/junior/{tenant_id}` (stage, catalog,
  datasets, questions, reproduce) — previously CLI-only services now over HTTP (**36 routes**, was
  25). Junior endpoints use the offline SamplerExecutor by default and the live
  `BrowserSessionExecutor` only when `ANALYTICS_MB_LIVE=1` (`_api_junior_executor`). Added
  `tests/test_api.py` (8 tests) which build a real `create_app(ctx)` and invoke the registered
  route handlers directly (no httpx/TestClient needed) — hits wiring, tenant-404 gating, and the
  endpoint→service path. **106 tests (3 live skipped).** README quick-start + roadmap updated.

## How to run (all in repo root)
```bash
.venv/bin/python -m analytics_platform.cli demo      # offline E2E demo (synthetic company)
.venv/bin/python -m analytics_platform serve 8000     # FastAPI → http://localhost:8000/docs
# standalone tests (106 = 103 pass + 3 live skipped); NOTE: plain `discover -s tests`
# additionally includes the legacy tests/test_ui_and_db which can hang on this machine
# (core.IngestionPipeline -> Neo4j not reachable) - see State caveat, it is untouched:
.venv/bin/python -m unittest tests.test_brain tests.test_browser_session tests.test_cli \
  tests.test_api tests.test_ingest tests.test_integration tests.test_junior tests.test_metabase_live \
  tests.test_migration tests.test_onboarding tests.test_pipeline_e2e tests.test_policy \
  tests.test_tenancy tests.test_triage
```
Env overrides: `ANALYTICS_DB_PATH`, `ANALYTICS_LLM_PROVIDER/MODEL/API_KEY`, `ANALYTICS_OLLAMA_URL`.
Deps frozen in `requirements-advanced.txt` (incl. `fastapi==0.141.1`).

## What exists (`analytics_platform/`, 24 modules)
- `domain.py`/`config.py` — typed models; `Settings.source_dialect` default `"athena"`; read-only policy defaults.
- `database.py` — SQLite store (stdlib), tenant-scoped schema, JSON columns.
- `tenancy.py` — tenants, structured company profile (targets), data sources.
- `brain/store.py` — Company Brain: lifecycle `CANDIDATE→UNDER_REVIEW→APPROVED/(APPROVED_WITH_CAVEATS|REVISION_REQUIRED|REJECTED)→STALE→SUPERSEDED/ARCHIVED`; approval is hard gate; `search()` returns usable-only; `conflicts()`.
- `brain/ingest.py` — legacy SQL → CANDIDATE QUERY/DEFINITION nodes via **`sqlglot` AST**; `column_business_definitions()`.
- `execution/base.py` — `QueryExecutor` protocol, `SessionStatus/QueryResult/ExecutionContext`.
- `execution/policy.py` — deterministic: blocks DML/multi-statement, allow-list tables, injects LIMIT.
- `execution/sampler.py` — offline executor: **`sqlglot` transpile source→duckdb**, runs on pandas frames.
- `execution/browser_session.py` — PRODUCTION executor: AppleScript→JS into authenticated Chrome tab; same-origin `fetch` (cookie stays in browser); `runner=` param injectable → offline-testable; `needs_login` fail-with-pause; `expected_host` guard. Requires `database_id`.
- `llm/client.py` — `LLMClient` protocol; `NullClient` (offline); `GatewayClient` wraps static `core.llm_gateway.LLMGateway` (never instantiate it).
- `analysis.py` — reuses `core.profiler.FastSummaryProfiler` + `core.rules.BusinessRuleEngine`; frames facts/hypotheses.
- `pipeline.py` — plan→policy→execute→analyze→persist; novel/anomalous → `REQUIRES_SENIOR_REVIEW`; `register_approved_query()`, `promote_finding()`.
- `onboarding.py` — `OnboardingService`: provision_company / add_main_tables / ingest_legacy / candidates / review / readiness (stage 0–3) / digest.
- `observability.py` — every hop emits span/event → `/metrics`.
- `api.py` — `create_app(ctx)`; `make_context()`; onboarding + triage + junior endpoints; 36 routes.
- `fixtures/` — synthetic retail warehouse + golden queries (athena-dialect SQL; transpiled at runtime).

## Next steps (Brain v2 + Live-Metabase + Junior engine + Junior→live + API exposure DONE)
1. **Wire the `GatewayClient` LLM hook.** `llm/client.py` is `NullClient` today (pipeline fully
   deterministic); implement `GatewayClient` over the **static** `core.llm_gateway.LLMGateway`
   (never `LLMGateway(...)`) for NL→plan/SQL and richer `suggest_questions`/stage-3, respecting the
   LLM data controls (no raw rows / no cookies to the LLM).
2. **Thin Streamlit UI over the API.** `app.py` (legacy, `core.*`) stays as reference; build a
   standalone `streamlit` client hitting `/triage` + `/junior` + `/tenants` routes (a lookable
   surface; React/Next later per plan §5).
3. **Keep triaging the remaining ~871 CANDIDATEs.** `cli review` / `/triage` — bulk approve by
   kind, `--conflicts` to dedupe the value-set Definition candidates.

Re-run migration into any tenant's Brain (idempotent, CANDIDATE-only):
`ANALYTICS_DB_PATH=<db> .venv/bin/python -m analytics_platform.cli migrate <tid> --snapshot extracted_data/knowledge_graph_snapshot.json`

## Invariants (do not break)
- LLMGateway is **static-only**: always `LLMGateway.generate(...)`, never `LLMGateway(...)`.
- Read-only by default; policy blocks DML. Approval status is the hard gate; confidence only ranks.
- Metabase access = open-browser cookie only. Never log/copy cookies or credentials; never auto-login.
- Tenant isolation: every store query scoped by `tenant_id`.
- `source_dialect="athena"` — executors transpile (e.g. `date_format`→duckdb `STRFTIME`); don't hardcode duckdb-only SQL in golden fixtures.
- Side effect note: importing some `core.*` modules triggers the pre-existing `core.pipeline`
  module-level checkpoint writes (`checkpoints/`, `logs/`); `.gitignore` excludes those + `data/*.db`.
