# ai_analytics_advanced — Standalone Analytics Platform

A clone of the prototype AI analytics copilot, rebuilt as a **company-independent,
modular-monolith platform** per `STANDALONE_ANALYTICS_PLATFORM_PLAN.md`.
The original prototype is left untouched in `AI analytics/`.

## What is here

```
analytics_platform/
  domain.py             typed models (Tenant, CompanyProfile, KnowledgeNode, AnalysisRun, ...)
  config.py             Settings + PolicySettings (read-only defaults)
  database.py           SQLite store (stdlib) + schema + init
  tenancy.py            tenants, structured company profile, data sources (tenant-scoped)
  brain/store.py        Company Brain: lifecycle (Candidate->…->Approved/Rejected/Stale),
                        multi-dimension confidence, isolation, search, conflicts
  brain/ingest.py       legacy-query ingestion via sqlglot AST (tables/columns/filters)
  execution/base.py     QueryExecutor protocol + SessionStatus / QueryResult / ExecutionContext
  execution/policy.py   deterministic read-only / allow-list / row-limit policy (sqlglot)
  execution/sampler.py  offline executor: real SQL on pandas frames via duckdb + sqlglot transpile
  execution/browser_session.py  production executor: AppleScript->JS into the authenticated
                        Chrome tab; same-origin fetch; login detected -> needs_login (fail-with-pause);
                        injectable OS runner => unit-tested offline
  llm/client.py         injectable LLMClient (NullClient offline / GatewayClient around LLMGateway);
                        make_client_from(settings); wired into junior suggest_questions
  ui_client.py          thin HTTP client for the API (used by the Streamlit UI)
  standalone_ui.py      Streamlit page = thin API client over the running FastAPI (plan §5)
  analysis.py           wraps FastSummaryProfiler + BusinessRuleEngine; facts vs hypotheses framing
  pipeline.py           orchestration: plan -> policy -> execute -> analyze -> persist -> telemetry
  onboarding.py         P3 wizard: provision company -> main tables -> ingest legacy -> review -> readiness
  migration/            brain-v2: knowledge-graph snapshot -> NodeSpec mapper + idempotent loader + cli 'migrate'
  observability.py      every hop emits an OpenTelemetry-style span/event + /metrics
  api.py                FastAPI (modular-monolith API; components stay in-process)
  cli.py / __main__.py  CLI demo + `python -m analytics_platform serve`
  fixtures/             synthetic retail company (warehouse + golden queries)
tests/                  stdlib-unittest suite (56 tests)
```

## Design invariants (from the plan)

- **Read-only by default.** Policy blocks DML/multi-statement and allow-lists tables.
- **Humans gate knowledge.** Novel analyses + anomalies stay `CANDIDATE`; only an authorized
  "senior" review can approve/promote. Approved queries are the only auto-answer path.
- **Metabase = open-browser cookie, permanently.** `BrowserSessionExecutor` is first-class;
  it never sees the cookie (same-origin `fetch`), never logs credentials, and stops
  (`needs_login`) instead of running against a logged-out session.
- **Approval is a hard gate; confidence only ranks.**
- **Observability as-if-over-APIs.** All components emit spans/events → `/metrics`
  shows the pipeline (planning→policy→execution→analysis) with latency + status.
- **Tenant isolation** enforced in every query.

## Run it

```bash
# offline demo (synthetic company; no Metabase/Neo4j/LLM)
.venv/bin/python -m analytics_platform.cli demo

# run tests (stdlib unittest)
.venv/bin/python -m unittest discover -s tests -v

# API server (interactive: http://localhost:8000/docs)
.venv/bin/python -m analytics_platform serve 8000

# one-click launcher: starts the backend (if not already running), opens the UI
./run_dashboard.command                  # http://localhost:8501
```
```bash
# migrate a prototype knowledge-graph snapshot into a tenant's Brain
# (lands as CANDIDATE nodes only; idempotent — safe to re-run)
TID=<tenant-id>
.venv/bin/python -m analytics_platform.cli migrate "$TID" \
  --snapshot extracted_data/knowledge_graph_snapshot.json
```
```bash
# LIVE Metabase (requires your logged-in Chrome + env)
#   session check (read-only):  .venv/bin/python -m analytics_platform.cli browser
#   run a real read-only query:
ANALYTICS_MB_DATABASE_ID=<id> \
ANALYTICS_MB_EXPECTED_HOST=<your.metabase.host> \
.venv/bin/python -m analytics_platform.cli browser \
  --sql "SELECT count(*) FROM <table-you-can-read>"
# gated live E2E test (skipped unless ANALYTICS_MB_LIVE=1):
ANALYTICS_MB_LIVE=1 \
.venv/bin/python -m unittest discover -s tests -k MetabaseLive -v
```
```bash
# Triage / senior-review the migrated CANDIDATEs:
.venv/bin/python -m analytics_platform.cli review <tenant>            # summary + queue
.venv/bin/python -m analytics_platform.cli review <tenant> --conflicts # title conflicts
.venv/bin/python -m analytics_platform.cli review <tenant> --kind IDIOM --bulk-approve --quiet
```
```bash
# Junior maturity-stage assessment (stage + EDA catalog + goal-aligned questions):
#   offline (synthetic warehouse) executor:
.venv/bin/python -m analytics_platform.cli junior <tenant>
#   against REAL Metabase (stage-3 reproduction over the live browser executor):
ANALYTICS_MB_LIVE=1 ANALYTICS_MB_DATABASE_ID=<id> \
ANALYTICS_MB_EXPECTED_HOST=<your.metabase.host> \
.venv/bin/python -m analytics_platform.cli junior <tenant>
```

### API quick start
```bash
TID=$(curl -s -X POST localhost:8000/tenants \
  -H 'content-type: application/json' -d '{"name":"Acme"}' | jq -r .tenant_id)
curl -s -X PUT  localhost:8000/tenants/$TID/company-profile \
  -H 'content-type: application/json' \
  -d '{"name":"Acme","industry":"ecommerce","description":"retail",
       "customers":"consumers","value_creation":"fast fulfilment",
       "revenue_model":"product margin",
       "targets":[{"name":"Grow orders","category":"growth","priority":1}]}'
curl -s -X POST localhost:8000/tenants/$TID/questions \
  -H 'content-type: application/json' -d '{"question":"Order completion rate by month"}'
curl -s localhost:8000/tenants/$TID/metrics
curl -s localhost:8000/triage/$TID/summary          # senior-review inbox
curl -s localhost:8000/junior/$TID/stage            # maturity stage (0-3)
curl -s localhost:8000/junior/$TID/catalog          # schema / EDA of mapped tables
curl -s localhost:8000/junior/$TID/questions        # goal-aligned suggestion questions
```

### Standalone UI (thin Streamlit client)
```bash
# recommended: one click — starts the API if needed, then opens the UI
./run_dashboard.command                          # http://localhost:8501

# manual (two terminals)
# terminal 1 - the API
.venv/bin/python -m analytics_platform serve 8000          # http://localhost:8000/docs
# terminal 2 - the UI (thin APIClient -> the running API)
.venv/bin/streamlit run standalone_ui.py                   # http://localhost:8501
# point the UI at any DB-backed API, e.g. the review DB:
ANALYTICS_API_URL=http://localhost:8001 .venv/bin/streamlit run standalone_ui.py
# (ANALYTICS_API_URL default http://localhost:8000)
```
> `standalone_ui.py` is the CURRENT UI (Business / Junior / Triage / Stakeholder /
> Research / Governance / Observability). `app.py` is the **legacy** SQL-generator UI
> from the original prototype (`AI analytics/`) and is **not** launched anymore.

## Roadmap status
- **P1–P3 foundation: DONE** — modular monolith, executor abstraction, deterministic policy,
  governed Brain, tenancy, observability, offline execution, FastAPI (36 routes).
- **P2 browser-session hardening: DONE** — `BrowserSessionExecutor` hardened (host verification,
  result caps, `needs_login` fail-with-pause) with an **injectable OS runner** → fully
  unit-tested offline (no live Chrome needed) in `tests/test_browser_session.py`.
- **P3 onboarding wizard: DONE** — `OnboardingService` + `/onboarding*` API: provision company,
  map main tables, ingest legacy SQL as CANDIDATE, senior review (approve/reject), and a
  readiness/stage report to gate the junior analyst.
- **Brain v2 migration: DONE** — `analytics_platform/migration/` maps
  `extracted_data/knowledge_graph_snapshot.json` into governed `KnowledgeNode`s
  (QUERY + derived DEFINITION, IDIOM, BUSINESS_RULE) as **CANDIDATE**, idempotently via
  `cli migrate`. Verified on the real snapshot: **1229 nodes** (158 QUERY / 598 DEFINITION /
  180 IDIOM / 293 BUSINESS_RULE), **0 auto-approved**; needs senior review to become usable.
- **Live Metabase bind: DONE + E2E CONFIRMED** — `Settings`/`from_env` + executor
  `from_env`/`make_live_executor` (`ANALYTICS_MB_*`), CLI `analytics-platform browser`
  (fail-with-pause session check + read-only execute), and a `MetabaseLive` E2E test
  gated by `ANALYTICS_MB_LIVE=1`. **Live run green** on `metabase.om.yo-digital.com` DB 59
  (session valid + read-only query).
- **Triage: DONE** — `analytics_platform/triage.py` (`TriageService`: queue/summary/conflicts +
  approve/reject/bulk by kind) and CLI `analytics-platform review <tenant>` (submit-then-approve;
  only CANDIDATE/UNDER_REVIEW/REVISION_REQUIRED are touched). Verified E2E on the migrated
  snapshot: 1229 CANDIDATEs → bulk-approve IDIOM (180) → actionable 1229→1049.
- **Junior maturity-stage engine: DONE** — `analytics_platform/junior.py` (`JuniorEngine`,
  read-only): `stage()` (0 provisioning → 3 process-analysis, needing reproduced approved
  queries + targets), `reproduce_metrics()` (runs approved queries via the injectable executor),
  `catalog()` (schema/EDA of registered tables), `suggest_questions()` (Company
  `Profile.targets` ↔ approved definitions/queries). CLI `analytics-platform junior <tenant>`.
- **Junior wired to live Metabase: DONE + LIVE-CONFIRMED** — `analytics-platform junior` runs the *same*
  `JuniorEngine` over the live `BrowserSessionExecutor` when `ANALYTICS_MB_LIVE=1`
  (host-guarded, cookie stays in the browser, read-only) and falls back to the offline
  `SamplerExecutor` otherwise. The seam is unit-tested offline in `tests/test_junior.py` /
  `tests/test_cli.py`, and the real-Metabase path is covered by the gated
  `TestJuniorMetabaseLive` (skipped unless `ANALYTICS_MB_LIVE=1`) — **3/3 live tests OK** on
  `metabase.om.yo-digital.com` DB 59 (incl. junior stage-3 reproduction + catalog).
- **Triage + junior as FastAPI endpoints: DONE** — `/triage/{tenant_id}` (summary/queue/conflicts +
  approve/reject/bulk) and `/junior/{tenant_id}` (stage/catalog/datasets/questions/reproduce)
  expose the previous CLI-only services over HTTP (36 routes total; offline executor by default,
  live browser executor when `ANALYTICS_MB_LIVE=1`). Covered by `tests/test_api.py` (endpoints hit
  over a real `create_app(ctx)`, no new dependencies).
- **LLM hook wired: DONE** — `JuniorEngine` now takes an injectable `llm`
  (`llm/client.make_client_from`): with a configured provider (non-`null`) it enriches
  `suggest_questions` with LLM-authored questions; with the default `NullClient` everything stays
  deterministic. `GatewayClient` wraps the **static** `core.llm_gateway.LLMGateway.generate`
  (never instantiated; no raw rows/cookies to the LLM). Covered by `tests/test_llm.py` + junior
  enrichment/failure tests.
- **Thin Streamlit UI over the API: DONE** — `standalone_ui.py` is a pure API client
  (`analytics_platform/ui_client.py`) hitting `/tenants`, `/junior/*`, `/triage/*` (list/create
  tenant, stage + catalog + questions, triage summary/queue/approve/bulk). Covered by
  `tests/test_ui_client.py`; boots headless against the running API. Legacy `app.py` (`core.*`)
  remains the reference; React/Next later per plan §5. The Triage tab is a full **review panel**:
  metrics, a **Definitions** review tab (grouped by column; shows each value-set + its source SQL
  before you approve/reject), per-row approve/reject via `st.data_editor`, bulk by kind, a node
  inspector, and a **Conflicts** tab (keep-one/reject-rest dedupe). Fixed a latent UI bug
  (GET `/tenants` returns `id`, POST returns `tenant_id`). Tabs now: Junior / Triage /
  **Stakeholder** / **Research** / **Governance**.
- **P6 Stakeholder analyst: DONE** — `stakeholder.py` (`StakeholderService`): classify →
  approved-knowledge-first → refresh/cite → low-cost route → escalate high-risk → feedback +
  quality. `/stakeholder/{tid}/*`. `tests/test_stakeholder.py`.
- **P7 External research: DONE** — `research.py` (`ResearchService`): allow/block sources +
  credibility; cited, `origin="external"` claims; capture; promote writes a `NodeKind.EXTERNAL`
  node that starts **CANDIDATE** (senior gate only). `/research/{tid}/*`. `tests/test_research.py`.
- **P8 Commercial hardening: DONE (auth off by default)** — `auth.py` (signed tokens, role RBAC,
  cross-tenant isolation, OIDC seam; on only with `ANALYTICS_AUTH_SECRET` +
  `ANALYTICS_AUTH_ENABLED=1`), `billing.py` (per-tenant usage + USD cost from telemetry),
  `retention.py` (per-tenant purge + full tenant deletion with audit). `/auth`, `/billing/*`,
  `/retention/*`, `DELETE /tenants/{tid}`. `tests/test_governance_auth.py` +
  `tests/test_governance_retention.py`.
- **P9 Metric/Observability layer (owner-facing): DONE (core)** — `api_logs` table + an HTTP
  access-log middleware (`create_app`) recording every request (30-day retention, no credentials);
  `analytics_platform/scheduler.py` `Scheduler` auto-purges API logs **weekly** (persisted due-state,
  `ANALYTICS_WATCHER=1` + `uvicorn analytics_platform.serve:make_serve --factory`); an autonomous
  background **`JuniorWorker`** (`analytics_platform/junior_worker.py`) that runs only inside its
  system-time window (default 10:00–19:00, `ANALYTICS_JUNIOR_WORK_START/END`), **one problem
  statement per hour** (`ANALYTICS_JUNIOR_MIN_INTERVAL_MINUTES`), executing **serially** (single-flight
  lock — never two queries at once). Routes `/observability/{status,logs,purge,junior/run}`; a UI
  **Observability** tab; `tests/test_phase9.py` + API/ui_client coverage. `AppContext` now exposes
  `scheduler` / `junior_worker`.

- **CP-10 Senior-analyst tool: DONE** — `senior.py` (`SeniorService`): per-analyst AI
  toggles + model (junior / senior / stakeholder) stored per tenant (`analyst_configs`,
  versioned in `analyst_config_history`); a senior **review inbox** over junior analyses
  (approve / reject / revise → promote to a governed FINDING); the human plays the senior
  role through the same surface when the senior AI is off (human-on-top). Config panel in
  the UI; covered by the API + `ui_client` test suites.

Next per plan: an **OpenRouter config panel** (list provider models via a live ping, save
config, log config state/changes), then wire the operator tail for P8 (live OIDC/SSO +
per-tenant browser profile) and the security tail (threat model / pen test / DR / SOC2
readiness). Senior review of the migrated Brain is complete (536 approved / 693 rejected).