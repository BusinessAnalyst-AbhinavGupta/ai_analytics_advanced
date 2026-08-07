# HANDOFF — Next Session

Prepared: 2026-08-07 · Repo: `/Users/abhinav.gupta/Documents/ai_analytics_advanced`
Goal: standalone, company-independent AI analytics copilot (see `STANDALONE_ANALYTICS_PLATFORM_PLAN.md`).

## State
- Git clean at `9321854` (handoff committed). **Original `AI analytics/` folder is untouched.**
- **40/40 tests pass**: `cd <repo> && .venv/bin/python -m unittest discover -s tests`
- Live API verified (provision→tables→legacy→review→readiness smoke). 23 routes.

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

## How to run (all in repo root)
```bash
.venv/bin/python -m analytics_platform.cli demo      # offline E2E demo (synthetic company)
.venv/bin/python -m unittest discover -s tests -v    # tests
.venv/bin/python -m analytics_platform serve 8000     # FastAPI → http://localhost:8000/docs
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
- `api.py` — `create_app(ctx)`; `make_context()`; onboarding endpoints; 23 routes.
- `fixtures/` — synthetic retail warehouse + golden queries (athena-dialect SQL; transpiled at runtime).

## Next steps (Brain v2 + Live-Metabase wired; Triage + Junior engine DONE)
1. **Wire the junior engine to the live `BrowserSessionExecutor`.** Run `JuniorEngine` with a
   live executor so stage‑3 assessment (`reproduce_metrics`/`catalog`) works against real Metabase
   data — same code path, now over the browser executor.
2. **Finish the live-Metabase E2E (your Chrome).** Run the gated `MetabaseLive` test /
   `analytics-platform browser` with your `database_id` + `expected_host`.
3. **Keep triaging the remaining ~871 CANDIDATEs.** `cli review` — bulk approve by kind,
   `--conflicts` to dedupe the value-set Definition candidates.
4. (optional) Expose `triage`/`junior` as FastAPI endpoints; add an LLM hook for richer
   stage-3 questions (NullClient today → `GatewayClient`).

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
