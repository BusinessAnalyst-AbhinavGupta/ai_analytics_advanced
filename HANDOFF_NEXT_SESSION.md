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

## Next steps (Brain v2 migration is DONE — pick one next)
1. **Junior maturity-stage engine (plan P5).** Stage-gated agent: stage 0–1 schema/EDA
   (use `SamplerExecutor` + catalog), stage 2 metric reproduction vs approved definitions
   (the 1229 migrated CANDIDATE nodes are this engine's raw material), stage 3 goal-aligned
   questions from `CompanyProfile.targets`. Needs an LLM hook (NullClient today; swap to
   `GatewayClient` with provider config).
2. **Bind live Metabase E2E (plan P2 finish).** `BrowserSessionExecutor` is ready but never
   run against a real browser. Needs your logged-in Chrome on Metabase, a `database_id`, and
   `expected_host`; add an integration test gated by env (`ANALYTICS_MB_LIVE=1`).
3. **Triage the migrated CANDIDATEs.** 1229 nodes await senior review; a lightweight review
   workflow (CLI/API) to work through them — and `brain.conflicts()` (74 title conflicts) —
   would make them usable.

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
