# AI_Analytics_Advanced — Standalone Analytics Platform

This is a clone of an existing prototype AI analytics copilot, rebuilt as a **company-independent, modular-monolith platform** based on `STANDALONE_ANALYTICS_PLATFORM_PLAN.md`. The original prototype remains untouched in the `AI analytics/` directory.

## What's Included

```
analytics_platform/
  domain.py                typed models (Tenant, CompanyProfile, KnowledgeNode, AnalysisRun, etc.)
  config.py                Settings + PolicySettings (read-only defaults)
  database.py              SQLite store (stdlib) + schema + initialization
  tenancy.py               tenants, structured company profile, data sources (tenant-scoped)
  brain/store.py           Company Brain: lifecycle (Candidate->…->Approved/Rejected/Stale),
                           multi-dimension confidence, isolation, search, conflicts
  brain/ingest.py          legacy-query ingestion via sqlglot AST (tables/columns/filters)
  execution/base.py        QueryExecutor protocol + SessionStatus / QueryResult / ExecutionContext
  execution/policy.py      deterministic read-only / allow-list / row-limit policy (sqlglot)
  execution/sampler.py     offline executor: real SQL on pandas frames via duckdb + sqlglot transpile
  execution/browser_session.py production executor: AppleScript->JS into the authenticated Chrome tab;
                           same-origin fetch; login detected -> needs_login (fail-with-pause);
                           injectable OS runner => unit-tested offline
  llm/client.py            injectable LLMClient (NullClient offline / GatewayClient around LLMGateway);
                           make_client_from(settings); wired into junior suggest_questions
  ui_client.py             thin HTTP client for the API (used by Streamlit UI)
  standalone_ui.py         Streamlit page = thin API client over running FastAPI (plan §5)
  analysis.py              wraps FastSummaryProfiler + BusinessRuleEngine; facts vs hypotheses framing
  pipeline.py              orchestration: plan -> policy -> execute -> analyze -> persist -> telemetry
  onboarding.py            P3 wizard: provision company -> main tables -> ingest legacy -> review -> readiness
  migration/               brain-v2: knowledge-graph snapshot -> NodeSpec mapper + idempotent loader + cli 'migrate'
  junior.py                junior engine: maturity stages, EDA catalog, goal-aligned questions (LLM hook)
  junior_worker.py         P9/CP-12 background junior: system-window, serial single-flight, persisted
                           1/hr + 3/day caps; picks reproducible approved funnel queries; full analysis + .md
  senior.py                CP-10 senior: per-analyst AI config + review inbox (approve/reject/revise -> FINDING);
                           CP-11: human promote/downgrade of junior question-depth + human-signoff window
  markdown.py              CP-11: every analysis renders as a reviewable .md (question/SQL/facts/hypotheses)
  scheduler.py             P9 weekly auto-purge scheduler (persisted due-state)
  triage.py                senior-review inbox service (approve/reject/bulk + conflicts dedupe)
  stakeholder.py           P6 stakeholder analyst (classify -> approved-knowledge-first -> escalate)
  research.py              P7 external research (allow/block sources, citations, senior-gated promote)
  observability.py         every hop emits an OpenTelemetry-style span/event + /metrics
  api.py                   FastAPI (modular-monolith API; components stay in-process)
  cli.py / __main__.py     CLI demo + `python -m analytics_platform serve`
  fixtures/                synthetic retail company (warehouse + golden queries)
tests/                     stdlib-unittest suite (169 tests)
```

## Design Invariants

- **Read-only by default.** Policy blocks DML/multi-statement and allow-lists tables.
- **Humans gate knowledge.** Novel analyses + anomalies stay `CANDIDATE`; only an authorized "senior" review can approve/promote. Approved queries are the only auto-answer path.
- **Metabase = open-browser cookie, permanently.** `BrowserSessionExecutor` is first-class; it never sees the cookie (same-origin `fetch`), never logs credentials, and stops (`needs_login`) instead of running against a logged-out session.
- **Approval is a hard gate; confidence only ranks.**
- **Observability as-if-over-APIs.** All components emit spans/events → `/metrics` shows pipeline latency + status.
- **Tenant isolation** enforced in every query.

## Running the Platform

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
TID=<tenant-id>
.venv/bin/python -m analytics_platform.cli migrate "$TID" \
  --snapshot extracted_data/knowledge_graph_snapshot.json
```
```bash
# LIVE Metabase (requires your logged-in Chrome + env)
ANALYTICS_MB_DATABASE_ID=<id> \
ANALYTICS_MB_EXPECTED_HOST=<your.metabase.host> \
.venv/bin/python -m analytics_platform.cli browser \
  --sql "SELECT count(*) FROM <table-you-can-read>"
```
```bash
# Triage / senior-review the migrated CANDIDATEs:
.venv/bin/python -m analytics_platform.cli review <tenant>
.venv/bin/python -m analytics_platform.cli review <tenant> --conflicts
.venv/bin/python -m analytics_platform.cli review <tenant> --kind IDIOM --bulk-approve --quiet
```
```bash
# Junior maturity-stage assessment (stage + EDA catalog + goal-aligned questions):
ANALYTICS_MB_LIVE=1 ANALYTICS_MB_DATABASE_ID=<id> \
ANALYTICS_MB_EXPECTED_HOST=<your.metabase.host> \
.venv/bin/python -m analytics_platform.cli junior <tenant>
```

### API Quick Start
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
.venv/bin/python -m analytics_platform serve 8000          # http://localhost:8000/docs
.venv/bin/streamlit run standalone_ui.py                   # http://localhost:8501
```
> `standalone_ui.py` is the CURRENT UI (Business / Junior / Triage / Stakeholder / Research / Governance / Observability). `app.py` is the **legacy** SQL-generator UI from the original prototype (`AI analytics/`) and is **not** launched anymore.

## Roadmap Status

- **P1–P3 foundation: DONE** — modular monolith, executor abstraction, deterministic policy, governed Brain, tenancy, observability, offline execution, FastAPI (36 routes).
- **P2 browser-session hardening: DONE** — `BrowserSessionExecutor` hardened (host verification, result caps, `needs_login` fail-with-pause) with an injectable OS runner → fully unit-tested offline in `tests/test_browser_session.py`.
- **P3 onboarding wizard: DONE** — `OnboardingService` + `/onboarding*` API: provision company, map main tables, ingest legacy SQL as CANDIDATE, senior review (approve/reject), and a readiness/stage report to gate the junior analyst.
- **Brain v2 migration: DONE** — `analytics_platform/migration/` maps knowledge-graph snapshot into governed `KnowledgeNode`s (QUERY + derived DEFINITION, IDIOM, BUSINESS_RULE) as CANDIDATE, idempotently via `cli migrate`. Verified on real data: 1229 nodes, no auto-approved; senior review needed.
- **Live Metabase bind: DONE + E2E CONFIRMED** — `Settings`/`from_env` + executor `from_env`/`make_live_executor`, CLI `analytics-platform browser`, and a `MetabaseLive` E2E test. Live run green on `metabase.om.yo-digital.com`.
- **Triage: DONE** — `analytics_platform/triage.py`: queue/summary/conflicts + approve/reject/bulk by kind. Verified on migrated snapshot: 1229 CANDIDATEs → bulk-approve IDIOM (180) → actionable 1229→1049.
- **Junior maturity-stage engine: DONE** — `analytics_platform/junior.py`: stage(), reproduce_metrics, catalog, suggest_questions. CLI `analytics-platform junior <tenant>`.
- **Junior wired to live Metabase: DONE + LIVE-CONFIRMED** — runs the same JuniorEngine over live BrowserSessionExecutor; falls back offline otherwise.
- **Triage + junior as FastAPI endpoints: DONE** — `/triage/{tenant_id}` and `/junior/{tenant_id}`, exposing previous CLI-only services over HTTP (36 routes total).
- **LLM hook wired: DONE** — `JuniorEngine` now takes injectable llm (`llm/client.make_client_from`). Covered by tests.
- **Thin Streamlit UI over the API: DONE** — `standalone_ui.py`, pure API client. Covered by `tests/test_ui_client.py`.
- **P6 Stakeholder analyst: DONE** — `stakeholder.py`: classify → approved-knowledge-first → escalate high-risk. `/stakeholder/{tid}/*`. Tests covered.
- **P7 External research: DONE** — `research.py` allows/block sources, captures citations, promotes writes as NodeKind.EXTERNAL (senior gate).
- **P8 Commercial hardening: DONE (auth off by default)** — Signed tokens, role RBAC, cross-tenant isolation. Routes `/auth`, `/billing/*`, `/retention/*`.
- **P9 Metric/Observability layer (owner-facing): DONE (core)** — `api_logs` table + HTTP access-log middleware recording every request; auto-purges API logs weekly.
- **CP-10 Senior-analyst tool: DONE** — `senior.py`: per-analyst AI toggles, review inbox over junior analyses. Config panel in UI.

Next on the roadmap involves live OIDC/SSO and security tailoring before advancing to Stage 4–5 autonomy for junior tasks. The senior review of the migrated Brain is complete (536 approved / 693 rejected).