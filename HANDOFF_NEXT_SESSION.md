# HANDOFF — Next Session

Prepared: 2026-08-08 · Repo: `/Users/abhinav.gupta/Documents/ai_analytics_advanced`
Goal: standalone, company-independent AI analytics copilot (see `STANDALONE_ANALYTICS_PLATFORM_PLAN.md`).

> **Status spine: `STANDALONE_ANALYTICS_PLATFORM_PLAN.md` → "Plan Status & Progress"** is the source of
> truth for phase status / beyond-plan additions / backlog. This handoff is a session-to-session
> snapshot on top of it — keep the plan's spine updated when work lands.

## State
- **HEAD `e99580e` **CP-14** (`main`) — clean tree; everything committed.** P0–P9 core + CP-10 + CP-11 +
  CP-12 + CP-13 + CP-14 + triage + launcher/docs are all in `git log`. Original `AI analytics/`
  folder untouched.
  Latest checkpoints: `e99580e` **CP-14** (draining senior review + no-repeat junior) · `f427179`
  **CP-13** (LLM bill guard) · `59000c7` **CP-12** (live junior + persisted caps).
- **CP-14 (draining senior review) is IN** — the review inbox no longer grows:
  * `SeniorService.queue` now **excludes approved AND rejected** runs (it used to leave
    approved ones in the inbox), so approving or rejecting visibly empties a row. Approved
    analyses are governed **FINDING** nodes in the knowledge graph (already the case via
    `promote_finding`); rejected ones are recorded as declined.
  * The **junior never re-asks a question it already answered**: `JuniorWorker.pick_problem_statement`
    now skips any question present in `analysis_runs` (`_answered_questions`), so once a funnel
    question is explored it is not asked again — it favours unexplored approved queries, then
    freshly suggested questions, then a baseline.
  * A **review-backlog gate** (`junior_review_backlog_max`, config `analytics_platform/config.py`,
    env `ANALYTICS_JUNIOR_REVIEW_BACKLOG_MAX`, default 3): once that many completed analyses are
    still awaiting/back for senior review, the worker backs off (`reason="review_backlog"`) until
    the inbox drains; `force=True` (test/operator lever) bypasses it.
  * The UI senior-inbox now shows a clean one-line result and **reruns** after Approve/Reject so the
    item drops out immediately (removed the fragile `st.success(res) if ... else st.error(...)` bit
    that surfaced a Streamlit DeltaGenerator dump).
- **CP-13 (LLM bill guard) is IN** — the OpenRouter enrichment for suggested questions/hypotheses is
  now throttled **process-wide** (module-level TTL cache, default 60 min per tenant), so Streamlit
  reruns (any checkbox/button tick) or any API caller fires at most **one** LLM generation per tenant
  per TTL; a **persisted daily LLM budget** (`llm_daily:{tid}:{date}` in `scheduler_state`, default 20
  generations/tenant/day) is the hard stop and survives app restarts; the UI caches junior reads with
  `@st.cache_data` (stage capped at 3 repro queries). Verified live: 3× `/junior/{tid}/questions` →
  exactly **1** OpenRouter call (`OK`), the rest `CACHED`, budget counter stays at 1.
- **CP-12 (live junior) is IN** — background analyses are now real: profiler + rules +
  **OpenRouter-narrated insight**, explicit **assumptions** + **actionables**, MD written per analysis,
  `CANDIDATE` review status. The **1-per-hour and 3-per-UTC-day caps are persisted in SQLite
  `scheduler_state`** (`junior_last:*`, `junior_daily:*:<date>`) so closing/restarting the app never
  resets them; `POST /tenants/{tid}/junior/run` drives a self-picked reproducible approved funnel query
  on-demand (`force` relaxes window/rate only for tests; serial + disable-gate always hold). The live
  deployment runs on `data/migration.db` tenant `tnt_56a8295f82c3` (DT funnel profile + 3 approved
  funnel queries) over live Metabase DB 59 with the scheduler on (`ANALYTICS_WATCHER=1`).
- **CP-11 senior tool + config panel is IN** — per-analyst AI config (junior/senior/stakeholder
  toggles + model, versioned per tenant in `analyst_configs` + `analyst_config_history`); senior
  review inbox (`approve`/`reject`/`revise` → FINDING; human-on-top); junior depth + human-signoff
  window; per-analysis `.md` review files; config tab + model ping.
- **193/193 standalone tests pass** (all `tests/` modules except the legacy `test_ui_and_db`).
  Run: `cd <repo> && .venv/bin/python -m unittest tests.test_api tests.test_brain
  tests.test_browser_session tests.test_cli tests.test_governance_auth
  tests.test_governance_retention tests.test_ingest tests.test_junior
  tests.test_llm tests.test_llm_cache tests.test_migration tests.test_onboarding tests.test_phase9
  tests.test_pipeline_e2e tests.test_policy tests.test_review_flow tests.test_research
  tests.test_senior_depth tests.test_stakeholder tests.test_tenancy tests.test_config_panel
  tests.test_triage tests.test_ui_client  # -> 193 tests (3 live skipped)`
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
- Live API verified; **64 HTTP routes** (incl. `/triage/*`, `/junior/*`, and new `/observability/*`).
  Phase 9 boot verified via uvicorn on `make_serve` (watcher on): access-log middleware records each
  request; `/observability/status` reports retention=30 / interval=7 / scheduler enabled; startup
  tick auto-purged once (persisted state).

## Progress (committed history — status lives in the plan's "Plan Status & Progress" section)
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
  **Brain v2 migration is complete (CP1–CP3; 56/56 tests at that time).**

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
- **CP-X2 — wire the `GatewayClient` LLM hook (DONE)**: `llm/client.py` gained `make_client_from(settings)`
  (null → `NullClient`). `JuniorEngine` now takes an injectable `llm` and enriches
  `suggest_questions` with LLM-authored questions when a provider is configured (deterministic
  fallback otherwise; failures are observability-logged, never raised; no raw rows/cookies to the
  LLM). CLI `junior` + API `/junior/*` pass `make_client_from(ctx.settings)`. `GatewayClient` still
  wraps the **static** `core.llm_gateway.LLMGateway.generate` (never instantiated). Added
  `tests/test_llm.py` (6) + junior enrichment/failure tests (3). **116 tests (3 live skipped).**
  *Honest: with the default `null` provider the LLM path is inert — needs a provider + key to go live.*
- **CP-X3 — thin Streamlit UI over the API (DONE)**: `analytics_platform/ui_client.py` (`APIClient`,
  thin `requests`-based client hitting `/tenants`, `/junior/*`, `/triage/*`) + `standalone_ui.py`
  (repo-root Streamlit page = pure API client: list/create tenant, junior stage+catalog+questions,
  triage summary/queue/approve/bulk; `ANALYTICS_API_URL` override). Legacy `app.py` (`core.*`) stays
  the reference; React/Next later (plan §5). Added `tests/test_ui_client.py` (5); boots headless
  (HTTP 200) against the running API. **121 tests (3 live skipped).**
- **CP-X5 — Triage review panel in the UI (DONE)**: `standalone_ui.py` Triage tab is now a real
  **review panel** — 4 metrics (total/actionable/approved/conflicts), **Queue review** tab
  (`st.data_editor` rows: select → Approve/Reject selected; Bulk-approve/reject by kind; reviewer
  + notes; "Inspect a node" expander with full title/summary/payload), and a **Conflicts** tab
  (per-group "keep one → reject rest" dedupe, or approve whole group). `ui_client` approve/reject/
  bulk now pass `notes`. **Fixed a latent UI bug:** `GET /tenants` serializes id as `id` (POST
  returns `tenant_id`), so the tenant selector previously always showed `None` → `_tenants()` now
  reads `id`, falling back to `tenant_id`. Validated with Streamlit `AppTest` (no exceptions). Left
  running: review API `:8001` → `data/migration.db`, UI `:8501` → `:8001`.
- **CP-X6 — Definitions review tab (DONE)**: the Triage area now has a dedicated **Definitions**
  tab that is a value-set review surface — each DEFINITION is shown as `column uses values […]
  (from source query)`, **grouped/sortable by column**, with per-row approve/reject + a "see its
  source SQL" expander (`st.code`), plus bulk-approve-all-DEFINITIONs. So a reviewer can actually
  read the definition and its provenance before deciding. Also fixed duplicate-button-ID errors
  (all review buttons now carry unique `key`s). Validated with Streamlit `AppTest` (no exceptions);
  UI restarted on `:8501 → :8001`.
- **CP-X7 — prune value-set definitions (data task, DONE)** on `data/migration.db` tenant
  `tnt_56a8295f82c3`, per the operator rule: first **rejected every node whose context contains a
  "Business Problem"** (`payload.sql` for QUERY, `payload.source_sql` for DEFINITION) → **603 rejected
  (123 QUERY + 480 DEFINITION)**; then **deduped the remaining actionable by title** (keep one, reject
  rest) → **19 groups, 41 rejected**; result **REJECTED 644, CANDIDATE 112 (QUERY 35 + DEFINITION 77),
  APPROVED 473**, with **0 title-conflicts among the remaining actionable** (the summary still reports
  `conflicts: 74` — that's the global brain count, now mostly REJECTED and not actionable). Also the
  **UI review views now filter to actionable statuses only** (REJECTED/APPROVED hidden), so the queue
  shows just the 112 you can act on.
- **CP-X8 — second triage pass (approve f_* / journey-stage, then random dedupe) (DONE)** on the
  same `data/migration.db`: approved **43** (2 DEFINITION whose column starts `f_`: `f_basket`,
  `f_pi_continue`; + **41** whose context contains "journey stage" = 15 QUERY + 26 DEFINITION);
  then among the remaining actionable there were **0 conflict groups** so no random-pick drops were
  needed. Final `REJECTED 644, APPROVED 516, CANDIDATE 69 (QUERY 20 + DEFINITION 49)` — 0 actionable
  title-conflicts.
- **CP-X9 — approve all QUERY kind (DONE)**: approved the remaining **20 actionable QUERY** on
  `data/migration.db`. QUERY kind is now **fully resolved: APPROVED 35 (15 journey-stage pass + 20
  now), REJECTED 123, CANDIDATE 0**. Overall `REJECTED 645, APPROVED 536, CANDIDATE 48 (all DEFINITION)`.
  *Note: during this op one DEFINITION (`kn_4550f49aca16`) was seen REJECTED with the UI-default note
  "rejected in triage" (timestamped in this window) — a concurrent UI Conflicts-tab action, not from
  the QUERY-approve (which only ran `approve`).*
- **CP-X10 — reject all remaining (review complete) (DONE)**: the 48 remaining CANDIDATE DEFINITIONs
  were all rejected → **actionable is now 0**. There were **0 conflict groups** among them (all unique
  titles) so the "randomly keep one per conflict" step had nothing to apply. Final matrix:
  `APPROVED 536 (QUERY 35 / DEFINITION 28 / IDIOM 180 / BUSINESS_RULE 293)`, `REJECTED 693 (QUERY 123 /
  DEFINITION 570)`. The migrated Brain for this tenant is now fully governed — no CANDIDATE nodes.
- **CP-P6 — Stakeholder analyst (DONE)**: `stakeholder.py` (`StakeholderService`) — classify →
  retrieve **approved** knowledge first → refresh an approved query (reuse) → low-cost LLM route →
  escalate high-risk questions → citations + freshness + caveats; `record_feedback` + `quality`
  (acceptance/escalation/reuse/cost). Tables `stakeholder_answers` + `stakeholder_feedback`.
  Endpoints `/stakeholder/{tid}/answer|feedback|quality`; UI tab. Verified: reuse-with-citation,
  definition fall-through, escalation, cannot-answer, feedback/quality (see `tests/test_stakeholder.py`).
- **CP-P7 — External research (DONE)**: `research.py` (`ResearchService`) — allow/block source
  list + credibility classification; **cited** search with `origin="external"` flagged; captures
  docs; promotion writes a **`NodeKind.EXTERNAL` node that starts CANDIDATE** (only the senior
  triage gate can ever APPROVE it — research can never silently become company fact). Endpoints
  `/research/{tid}/sources|search|capture|docs|promote|overview`; UI tab. See `tests/test_research.py`.
- **CP-P8 — Commercial hardening (DONE, auth off by default)**: `auth.py` (`Role`/`AuthGate`/
  `issue`/`verify`) — signed tokens, role+rank RBAC, **cross-tenant isolation** enforced in the
  auth layer, OIDC/SSO seam (`oauth_issuer`), **off unless `ANALYTICS_AUTH_SECRET` +
  `ANALYTICS_AUTH_ENABLED=1`** so existing routes/tests stay open. `billing.py` (`BillingService`) —
  per-tenant usage + USD cost from telemetry. `retention.py` (`RetentionService`) — per-tenant
  purge by `retention_days` (dry-run/review) + **full tenant deletion** with an append-only audit
  record. Endpoints `/auth/login|me`, `/billing/{tid}/usage`, `/billing/report`,
  `/retention/review|purge`, `DELETE /tenants/{tid}`. See `tests/test_governance_auth.py` +
  `tests/test_governance_retention.py`.

## How to run (all in repo root)
```bash
.venv/bin/python -m analytics_platform.cli demo      # offline E2E demo (synthetic company)
.venv/bin/python -m analytics_platform serve 8000     # FastAPI → http://localhost:8000/docs
.venv/bin/streamlit run standalone_ui.py              # thin UI over the API → :8501 (ANALYTICS_API_URL)
# standalone tests (169 = 166 pass + 3 live skipped); NOTE: plain `discover -s tests`
# additionally includes the legacy tests/test_ui_and_db which can hang on this machine
# (core.IngestionPipeline -> Neo4j not reachable) - see State caveat, it is untouched:
.venv/bin/python -m unittest tests.test_api tests.test_brain tests.test_browser_session \
  tests.test_cli tests.test_governance_auth tests.test_governance_retention \
  tests.test_ingest tests.test_integration tests.test_junior tests.test_llm \
  tests.test_metabase_live tests.test_migration tests.test_onboarding tests.test_phase9 \
  tests.test_pipeline_e2e tests.test_policy tests.test_research tests.test_stakeholder \
  tests.test_tenancy tests.test_triage tests.test_ui_client
```
Env overrides: `ANALYTICS_DB_PATH`, `ANALYTICS_LLM_PROVIDER/MODEL/API_KEY`, `ANALYTICS_OLLAMA_URL`.
Deps frozen in `requirements-advanced.txt` (incl. `fastapi==0.141.1`).

## What exists (`analytics_platform/`)
- `domain.py`/`config.py` — typed models; `Settings.source_dialect` default `"athena"`; read-only policy defaults.
- `database.py` — SQLite store (stdlib), tenant-scoped schema, JSON columns.
- `tenancy.py` — tenants, structured company profile (targets), data sources.
- `brain/store.py` — Company Brain: lifecycle `CANDIDATE→UNDER_REVIEW→APPROVED/(APPROVED_WITH_CAVEATS|REVISION_REQUIRED|REJECTED)→STALE→SUPERSEDED/ARCHIVED`; approval is hard gate; `search()` returns usable-only; `conflicts()`.
- `brain/ingest.py` — legacy SQL → CANDIDATE QUERY/DEFINITION nodes via **`sqlglot` AST**; `column_business_definitions()`.
- `execution/base.py` — `QueryExecutor` protocol, `SessionStatus/QueryResult/ExecutionContext`.
- `execution/policy.py` — deterministic: blocks DML/multi-statement, allow-list tables, injects LIMIT.
- `execution/sampler.py` — offline executor: **`sqlglot` transpile source→duckdb**, runs on pandas frames.
- `execution/browser_session.py` — PRODUCTION executor: AppleScript→JS into authenticated Chrome tab; same-origin `fetch` (cookie stays in browser); `runner=` param injectable → offline-testable; `needs_login` fail-with-pause; `expected_host` guard. Requires `database_id`.
- `llm/client.py` — `LLMClient` protocol; `NullClient` (offline); `GatewayClient` wraps **static** `core.llm_gateway.LLMGateway` (never instantiate it); `make_client`/`make_client_from(settings)`; wired into `junior` `suggest_questions` LLM enrichment.
- `ui_client.py` — thin HTTP client (`APIClient`) for the FastAPI; used by `standalone_ui.py`; config-panel + senior queue/review + MD + provider-model-ping methods (CP-11).
- `markdown.py` — CP-11: `render_analysis_md`/`write_analysis_md` → per-analysis `.md` for human review (persisted under `data/reviews/<tenant>/`).
- `analysis.py` — reuses `core.profiler.FastSummaryProfiler` + `core.rules.BusinessRuleEngine`; frames facts/hypotheses.
- `pipeline.py` — plan→policy→execute→analyze→persist; novel/anomalous → `REQUIRES_SENIOR_REVIEW`; `register_approved_query()`, `promote_finding()`.
- `onboarding.py` — `OnboardingService`: provision_company / add_main_tables / ingest_legacy / candidates / review / readiness (stage 0–3) / digest.
- `junior.py` — `JuniorEngine`: maturity stages (0–3), schema/EDA catalog, goal-aligned `suggest_questions` (LLM hook).
- `junior_worker.py` — Phase 9 background `JuniorWorker`: system-window, 1/hr, serial single-flight.
- `senior.py` — CP-10 `SeniorService`: per-analyst AI config (toggles + model) + review inbox (approve/reject/revise → governed FINDING; human-on-top).
- `scheduler.py` — Phase 9 weekly auto-purge `Scheduler` (persisted due-state).
- `triage.py` — `TriageService`: review inbox, approve/reject/bulk, conflicts dedupe (keep-one).
- `stakeholder.py` — P6 `StakeholderService`: classify → approved-knowledge-first → escalate → feedback + quality.
- `research.py` — P7 `ResearchService`: allow/block sources, citations, EXTERNAL nodes CANDIDATE → senior-gated promote.
- `auth.py`/`billing.py`/`retention.py` — P8: RBAC tokens (off unless `ANALYTICS_AUTH_ENABLED=1`), per-tenant usage/cost, retention + deletion.
- `serve.py`/`__main__.py`/`cli.py` — FastAPI `serve` + CLI (demo / review / migrate / browser).
- `observability.py` — every hop emits span/event → `/metrics`.
- `api.py` — `create_app(ctx)`; `make_context()`; tenants/onboarding/triage/junior/stakeholder/research/billing/retention/auth/observability endpoints; **64 routes**.
- `fixtures/` — synthetic retail warehouse + golden queries (athena-dialect SQL; transpiled at runtime).

## Progress — Triage of the re-migrated Brain (current)
- **CP-X4 — triage the migrated CANDIDATEs (data task, DONE)**: the prior sessions' migrated
  Brain wasn't in the repo's `data/`, so I re-migrated the snapshot idempotently (CANDIDATE-only)
  into `data/migration.db` (tenant `tnt_56a8295f82c3`; `data/*.db` is gitignored): **1229 nodes**
  (158 QUERY / 598 DEFINITION / 180 IDIOM / 293 BUSINESS_RULE, 0 approved, 74 conflicts). Then
  **bulk-approved IDIOM (180) + BUSINESS_RULE (293) = 473 approved** (low-risk semantic kinds,
  matching the CP-T3 precedent). **756 CANDIDATEs remain (QUERY 158 + DEFINITION 598) — left for
  human review, respecting the approval hard gate**; 74 value-set Definition conflicts are the dedup
  target. Reproduce anytime:
  `ANALYTICS_DB_PATH=data/migration.db .venv/bin/python -m analytics_platform.cli review tnt_56a8295f82c3 --conflicts`
  (or in the UI via `/triage/{tid}`). *Honest: auto-approving QUERY/DEFINITION offline could
  encode SQL/value-sets that aren't verified against real Metabase, so they stay CANDIDATE for a
  senior reviewer.*

## Progress — Phase 9: Metric/Observability layer (current)
- **CP-9 — owner-facing observability + background junior (DONE, this checkpoint).** This is the
  "§9 Metric/Observability layer (owner-facing)" pending item.
- **API logs + 30-day retention + weekly auto-purge**: new `api_logs` table; an HTTP access-log
  middleware in `create_app` records every request (method/path/status/duration_ms/tenant, no
  credentials); `Observability.purge_logs(retention_days=30, now=…)`; `analytics_platform/scheduler.py`
  `Scheduler` runs the purge once per `maintenance_interval_days` (7) with **persisted** due-state
  in `scheduler_state` (no re-run inside the interval across restarts). Start it via
  `ANALYTICS_WATCHER=1` and `uvicorn analytics_platform.serve:make_serve --factory` (fanned out into
  `serve.py::make_serve`). Settings: `log_retention_days`, `maintenance_interval_days`.
- **Autonomous background junior**: `analytics_platform/junior_worker.py` `JuniorWorker` — runs only
  between `junior_work_start`..`junior_work_end` (10:00–19:00 system time), at most one
  problem statement per `junior_min_interval_minutes` (60), and executes **serially** (a lock makes it
  single-flight — never two queries at once, so it can't blast the engine). Picks a goal-aligned
  question from `JuniorEngine.suggest_questions`, resolves SQL (prefers an approved reproducible
  query, else a safe single SELECT from the catalog), runs it via the injected executor, records an
  `analysis_runs` row + observability events. `AppContext` now carries `scheduler`/`junior_worker`.
- **API routes**: `/observability/status|logs|purge` and `/observability/junior/run` (manual single
  cycle, still honours window/rate). **UI Observability tab** added to `standalone_ui.py`;
  `ui_client` gains `observability_*` methods.
- **Tests**: `tests/test_phase9.py` (8) — scheduler weekly due/not-due + purge-older-than-retention,
  junior window / 1-per-hour rate / serial single-flight; API-contract tests in `test_api.py` and
  `ui_client` tests incl. the connectivity scan. **169/169 standalone tests OK.**
- Status: API-log retention + weekly purge + background-junior are the **core** (CP-9); the layered
  **senior-analysis review + promote (CP-10) is DONE**. The remaining next phase is the OpenRouter
  config panel (see Next steps).

## Next steps (aligned with the plan's Backlog)
1. **P8 operator tail** — wire a real OIDC/SSO provider and per-tenant browser profiles.
2. **P7 provider connections** — connect approved external search providers.
3. **Security tail** — threat model / pen test / DR / SOC2 readiness.
4. **Junior Stage 4–5** — external/competitive research autonomy + governed proactive investigations.
5. **Vector retrieval** for unstructured notes (plan §8).
6. **Junior depth → mastery badges** — automatic depth promotion once a senior-approval threshold is
   met (human still holds the override). The OpenRouter config panel and the per-analysis MD /
   depth-scaling senior-review depth are **DONE (CP-11)**.

**Triage is complete for the migrated tenant** (`tnt_56a8295f82c3`): 0 CANDIDATEs remain
(`APPROVED 536 / REJECTED 693`). To get junior value from it: the tenant already has a
CompanyProfile with 3 targets; point an executor at real data (`ANALYTICS_MB_LIVE=1 …`) to lift
`junior stage` to 2→3.

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
