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
                        Chrome tab; same-origin fetch; login detected -> needs_login (fail-with-pause)
  llm/client.py         injectable LLMClient (NullClient offline / GatewayClient around LLMGateway)
  analysis.py           wraps FastSummaryProfiler + BusinessRuleEngine; facts vs hypotheses framing
  pipeline.py           orchestration: plan -> policy -> execute -> analyze -> persist -> telemetry
  observability.py      every hop emits an OpenTelemetry-style span/event + /metrics
  api.py                FastAPI (modular-monolith API; components stay in-process)
  cli.py / __main__.py  CLI demo + `python -m analytics_platform serve`
  fixtures/             synthetic retail company (warehouse + golden queries)
tests/                  stdlib-unittest suite (26 tests)
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
```

## Roadmap status
P1–P3 foundation is implemented (module monolith, executor abstraction, policy,
governed Brain, tenancy, observability, offline execution, API). Next per plan:
browser-session hardening against a live Metabase, company-independent onboarding
wizard, Company Brain v2 migration from the prototype Neo4j graph, and the junior
maturity-stage engine.