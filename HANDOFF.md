# Handoff — Stakeholder Chat workstream (B1, B2, B3)

**Date:** 2026-08-15
**Branch:** `main` (all work committed directly to `main`, per project convention — no feature branches)
**Range:** `3eeee4e..8af2ac6`
**State:** all three features built, reviewed, and tested. Backend suite **478 passed, 1 skipped**. Frontend `tsc --noEmit` **0 errors**. Verified live against a running backend and the real UI.

Work stops here. This document is the handoff; nothing further is in progress.

---

## 1. What was built

Three features, each planned in full before implementation and executed task-by-task with a fresh implementer subagent per task and an independent reviewer gate after each.

### B1 — Stakeholder Conversations & Chat UI
Plan: [docs/superpowers/plans/2026-08-14-stakeholder-conversations-ui.md](docs/superpowers/plans/2026-08-14-stakeholder-conversations-ui.md)

Turned the Stakeholder Q&A widget from a stateless single-turn form into a persisted multi-turn chat. A `conversation_id` threads through `answer()` and the persistence layer; the frontend gained a real chat surface — history sidebar with rename/delete/star, scrollable message thread, working thumbs-up/down feedback, collapsible SQL code blocks.

Two new SQLite tables in each tenant's own database. No websockets, no streaming, no new frontend dependency.

Commits: `3eeee4e` (plan) → `896fe5c`.

### B2 — DataFrame Compute Engine & Python Repair Loop
Plan: [docs/superpowers/plans/2026-08-15-dataframe-compute-engine.md](docs/superpowers/plans/2026-08-15-dataframe-compute-engine.md)

A second compute path alongside SQL. Once a turn has fetched a DataFrame via synthesized SQL, later turns in the *same* conversation can answer follow-ups by running LLM-written Python directly against that already-fetched data instead of re-querying the warehouse.

- `ConversationDataCache` — in-memory, keyed by `(tenant_id, conversation_id)`, LRU-evicted.
- `PythonCodePolicy` — AST-based static gate on synthesized Python.
- `run_python_sandboxed` — resource-limited subprocess sandbox.
- `_choose_compute_path` — LLM routing step deciding SQL vs. cached-DataFrame Python.
- `_synthesize_and_execute_python` — error-repair loop mirroring the existing SQL retry loop.

Strict local-compute-only policy: raw data never leaves the process; only column/dtype schemas and small capped result summaries reach the LLM or get persisted.

Commits: `4d773b9` (plan) → `c2f816e`.

### B3 — Storyline Export (Report Builder, Word/Markdown)
Plan: [docs/superpowers/plans/2026-08-15-storyline-export.md](docs/superpowers/plans/2026-08-15-storyline-export.md)

A stakeholder selects a subset of turns from a conversation and exports them as Markdown or Word, with a **dependency-tracked Code Appendix**: selecting a Python turn automatically pulls in the SQL turn that produced the DataFrame it ran against, even when that turn was not itself selected.

| Layer | File | What it does |
|---|---|---|
| Assembly | [analytics_platform/storyline.py](analytics_platform/storyline.py) | Pure, DB-free. `assemble_storyline()` → `StorylineContent` IR; `render_markdown()`, `render_docx()`. |
| Persistence | [analytics_platform/stakeholder.py](analytics_platform/stakeholder.py), [analytics_platform/database.py](analytics_platform/database.py) | `produced_df_label` column records which turn populated which cached DataFrame. Idempotent `ALTER TABLE` migration. |
| API | [analytics_platform/api.py](analytics_platform/api.py) | `POST /stakeholder/{tenant_id}/conversations/{conversation_id}/export` |
| UI | [frontend/src/components/StakeholderChat.tsx](frontend/src/components/StakeholderChat.tsx), [frontend/src/store/useStore.ts](frontend/src/store/useStore.ts) | Report Builder panel: per-turn checkboxes, token estimate, format select, Export button, error display. |

New dependency: `python-docx==1.2.0` (added to both `requirements.txt` and `requirements-advanced.txt`).

Commits: `04f11fa` (plan) → `8af2ac6`.

---

## 2. Bugs found and fixed during review — worth knowing about

These are the non-obvious ones. They are all **fixed**; they are recorded because each reveals a gap in how this repo can be tested.

### 2a. `Content-Disposition` was invisible to the browser (fixed in `3a40123`)
Downloads always fell back to a generic `storyline.md` filename. `Content-Disposition` is not a CORS-safelisted response header, and the frontend (`:3000`) is cross-origin to the API (`:8000`), so `res.headers.get('content-disposition')` returned `null`.

Fixed with `expose_headers=["Content-Disposition"]` on the `CORSMiddleware`.

> **The structural lesson:** this repo has no `httpx`/`TestClient`. API tests use a `call()` helper in `tests/test_api.py` that invokes FastAPI route closures *directly* — which means **they bypass middleware entirely**. 460 passing tests could not have caught this. Only live browser verification did. There is now a middleware-level regression test (`test_cors_exposes_content_disposition_for_export_downloads`), but the gap itself remains: any future middleware-dependent behaviour is untested by construction.

### 2b. The Code Appendix could cite the *wrong* SQL (fixed in `e598ebd`) — the most serious one
Dependency resolution keyed on `df_label` (`df_1`, `df_2`, …) via a last-wins lookup. That label is **not unique within a conversation**, for two independent reasons: `ConversationDataCache` recycles labels after LRU eviction, and the cache is in-memory only, so labels restart at `df_1` after any API restart.

Failure mode: a user exports a Python turn, and the Code Appendix presents a *completely unrelated query that ran later* as the provenance of that turn's number. For an artifact whose entire purpose is provenance, silently wrong is the worst possible outcome.

Fixed by resolving to the **nearest preceding producer** — for a Python turn at conversation index `i` referencing label `L`, the producer is the greatest index `j < i` whose `produced_df_label == L`. This is correct because a Python cell can only operate on a frame cached by an earlier turn. No migration, no change to persisted shape. Label issuance in `dataframe_cache.py` was also made monotonic per conversation so eviction does not recycle labels in a live process.

### 2c. Any non-Latin-1 conversation title returned HTTP 500 (fixed in `8575ead`)
`str.isalnum()` is Unicode-aware, so CJK/Cyrillic/Devanagari characters survived the filename slug; Starlette encodes response headers as latin-1. Conversation titles auto-derive from the user's first question, so a Russian- or Japanese-language question made every export of that conversation fail permanently — and the frontend swallowed the error, so the user saw nothing at all.

Fixed with an ASCII-safe quoted `filename=` plus an RFC 5987 `filename*=UTF-8''…` form, bounded to 60 characters.

### 2d. Other fixes in the same round (`e598ebd`, `8575ead`, `8af2ac6`)
- `import docx` was at module scope in `storyline.py`, and `api.py` imports `storyline` unconditionally — a missing `python-docx` or a failed `lxml` build would have taken down **the entire API**, not just Word export. Now lazily imported inside `render_docx()`, mapped to a 503 on the export route.
- An unresolvable `df_label` was silently dropped. Since the migration backfills `NULL`, **every conversation created before this feature shipped** would have exported Python referencing an undefined `df_1` with no provenance and no warning. Unresolved labels now produce a visible `kind="note"` appendix entry and a count on `StorylineContent`.
- The frontend swallowed every export failure (`if (!res.ok) return;`). Now `exportError` is surfaced in the Report Builder panel.
- Markdown fence break-out (code containing triple backticks), control characters crashing `python-docx`, unbounded filename length, a no-op paragraph style, and an unrendered `created_at` — all fixed.

---

## 3. Known open items (deliberately deferred, nothing in progress)

Full detail with file:line references, reproductions, and suggested fixes:
[docs/superpowers/reviews/2026-08-15-storyline-export-final-review.md](docs/superpowers/reviews/2026-08-15-storyline-export-final-review.md)

Ordered by what I would pick up first.

**1. The export endpoint is unauthenticated and readable cross-origin (review finding I7).**
`POST .../export` takes no `authorization` header, and `allow_origins=["*"]` means any website the user visits can `fetch()` it and *read the response body*. Chain: `/tenants` → conversation ids → `POST .../export` exfiltrates a full analytics conversation including synthesized SQL over the tenant's warehouse.

This is **pre-existing and platform-wide** — the JSON conversation endpoints already leak the same content and the CORS wildcard was not introduced here. But this branch adds the first endpoint that packages an entire conversation into one downloadable artifact. Fix at the platform level: replace `allow_origins=["*"]` with the configured frontend origin, and require the same `authorization` header the billing routes already use.

**2. Token estimate is implemented twice and the two disagree (I4).**
`assemble_storyline` computes `estimated_tokens`/`over_budget`, but no endpoint ever returns them — both fields are dead in production. The live estimate is a client-side reimplementation in `StakeholderChat.tsx` with the divisor `4` inlined and the 50,000 threshold re-declared as a magic constant kept in sync by a comment.

They already diverge: the backend estimate includes the Code Appendix, the frontend's does not — so a user selecting short Python turns that pull in long SQL dependencies sees a small number and no warning, then downloads a document dominated by code. It always under-reports, i.e. fails in the direction that defeats the warning. This also conflicts with `AGENTS.md`'s thin-client and no-silent-hardcoding rules.

Fix: add an estimate endpoint (or a dry-run flag) and have the panel render the server's numbers; delete the client-side duplication.

**3. Dependency resolution is one hop and SQL-only (I2).**
`assemble_storyline` emits only `queries_run` for a dependency turn, never that turn's own `python_cells`, and never walks transitively. **Latent today** — `_synthesize_and_execute_python` never calls `data_cache.put`, so a Python turn never produces a cached frame. It goes live the moment anyone caches a Python result, which is the obvious next step for multi-step analysis, and it will silently produce incomplete appendices with no error.

Related latent defect from the fix round: if one turn both produces a label and consumes it, the nearest-preceding-producer lookup excludes the turn itself and emits a false "provenance not recorded" note beneath that turn's own SQL. Same trigger, same fix.

**4. No size ceiling on export (I6).**
`answer_ids` is unbounded, `render_docx` builds the whole document in memory and `buf.getvalue()` copies it, and FastAPI buffers the entire body. "Select all" on a very long conversation can spike memory process-wide and OOM the API for all tenants. Suggested: reject above ~500 turns / ~25 MB with a 413 naming the limit, keeping the 50k-token soft warning as-is.

**5. Minor (M3): the Code Appendix dependency annotation is circular** — it reads `### df_1 (sql) — (included as a dependency of df_1)`, naming the entry's own label rather than the selected turn that needed it. `tests/test_storyline.py` asserts the current string verbatim, so the test changes with it.

**6. `legacy_tenant_rewriter` (`api.py:539-553`) is invisible to the entire test suite.** It silently rewrites `/stakeholder/1/...` to `list_tenants()[0]`. Pre-existing, not reachable from this feature's frontend, and *not* a finding against this work — but it is the one code path where a request for tenant X can be served from tenant Y's database, and nothing tests it. Worth a standalone test.

---

## 4. Still to be built — Roadmap A (A3, A4, A2)

These are **not** part of the B1–B3 workstream and none of them were started. They are the three remaining plans from the 2026-08-13 remediation roadmap, which was written from an architecture evaluation of the whole platform. All three are already specified task-by-task and ready to execute.

Source: [docs/superpowers/plans/2026-08-13-INDEX.md](docs/superpowers/plans/2026-08-13-INDEX.md). The "problem / fix / why this shape" text below is pulled from that index; the **status** lines are what I verified in the code on 2026-08-15, not what the docs assert.

The roadmap had five plans. Plans 0 (Tenant Store Isolation) and 1 (Brain Retrieval Rebuild) are **done** — per-tenant SQLite files, and the SQLite-native BM25 + embeddings + RRF retrieval that replaced the broken ChromaDB path. The three below are what remains, listed in the execution order previously agreed (A3 → A4 → A2). Note the index's own suggested order is 2 → 3 → 4; see the ordering note at the end.

### A3 — Skills Portability ("skill democratization")
Plan: [docs/superpowers/plans/2026-08-13-skills-portability.md](docs/superpowers/plans/2026-08-13-skills-portability.md) — 5 tasks

**The problem** (from the index — *"the one most relevant to running across organisations"*): the only analytics skill hardcodes `eshop_data.es_events_v2`, `nc = 'de'` and one organisation's column names in the **shared** repository, offered to every tenant. Structurally, skills only run when the Brain returns nothing, so they become unreachable as the Brain improves; and successful runs are discarded rather than filed for review.

**The fix:** a skill declares an abstract **data contract** ("an event stream with an entity, a step and a timestamp"); each tenant supplies a **binding** mapping it onto real tables, gitignored at `tenants/<id>/skill_bindings.json` — beside that company's `tenant.db`, so its whole footprint is one directory. The SQL template becomes byte-identical for every organisation. The registry only offers skills the current tenant can satisfy, is anchored to the repo rather than the CWD, and excludes non-analytics skills. Skill selection becomes orthogonal to Brain hits, and results land as `CANDIDATE` findings.

**Why this shape:** onboarding organisation number four should be writing a bindings file, not forking SQL — and a method improvement should reach every customer at once. That is the class/object model `AGENTS.md` describes, applied to skills.

**Status — not started.** `skill_bindings.json` does not exist anywhere; there is no data-contract abstraction. The hardcoding is still live: `.agents/skills/advanced-funnel-dropoff-analysis/SKILL.md` still carries `silver_layer.t_link_journey_checkout_com`, `LOWER(svc.natco_code) = 'de'`, `eshop_data.es_events_v2` and `nc = 'de'`. `SkillRegistry` (`analytics_platform/skills/registry.py`) takes no tenant argument at all, so it cannot filter by what a tenant can satisfy.

One sub-problem the plan describes **is** already fixed: the `$key` vs `{{KEY}}` substitution mismatch that meant the skill never executed. `analytics_platform/skills/engine.py` now substitutes `$key`/`${key}`, and no `{{KEY}}` tokens remain in the skills. That was resolved while wiring the skill up in an earlier session; the plan's task list should be re-read with that in mind rather than executed blind.

**Dependencies:** independent of A2 and A4. The binding path assumes plan 0's layout, which is done — so it is unblocked.

### A4 — Frameworks & Confidence
Plan: [docs/superpowers/plans/2026-08-13-frameworks-and-confidence.md](docs/superpowers/plans/2026-08-13-frameworks-and-confidence.md) — 4 tasks

**The problem:** the four friction types and the Metrics Tree return **zero** grep hits across the codebase — the frameworks that most distinguish this from generic text-to-SQL are documentation no model has ever seen. Separately, of six confidence dimensions only two are ever updated; `freshness` is pinned at 1.0 forever yet is read for ranking, so ranking sorts on a constant. `AGENTS.md` also names a `data_quality` dimension that does not exist.

**The fix:** one versioned `frameworks.py` feeding the stakeholder and junior prompts, plus a validator that flags a driver recommendation with no guardrail (reporting only — it never fabricates a missing section). Freshness decays exponentially and is derived on read; evidence is seeded from a node's backing run; a scheduled sweep marks decayed nodes stale; the documented and stored dimension sets are reconciled.

**Status — not started.** No `frameworks.py` exists. No friction-type or Metrics-Tree code anywhere in `analytics_platform/`. `analytics_platform/research.py:167` still writes `"freshness": 1.0` as a literal, and `analytics_platform/stakeholder.py:343,391` still read `confidence["freshness"]` — so ranking still partly sorts on a constant.

**Dependencies:** the plan says it depends on plan 1 Task 7 (the ranking change in `CompanyBrain.search()`), which is done — so it is unblocked.

### A2 — Brain Governance
Plan: [docs/superpowers/plans/2026-08-13-brain-governance.md](docs/superpowers/plans/2026-08-13-brain-governance.md) — 5 tasks

**The problem:** `AGENTS.md` says humans approve knowledge before it becomes company fact. Three code paths disagree. The junior self-approves its own low-level findings (cap 500) despite a docstring claiming it never writes the Brain; `POST /knowledge/{t}/{n}/review` calls `brain.approve` with no `AuthGate` and a caller-supplied `by` string; the AI senior prompts an LLM for a verdict and then approves unconditionally, recording the model's assessment as a note. Two further write paths call methods that do not exist — `bulk_ingest_json` crashes on `brain.save`, and `evaluate_kpis` fails into a bare `except`, so proactive KPI monitoring has never fired.

**The fix:** auto-promotion becomes an opt-in, default-off tenant setting that leaves findings at `UNDER_REVIEW` when disabled; the review endpoint takes its reviewer identity from a verified principal; the AI verdict is parsed and decides, defaulting unparseable output to `revise` rather than `approve`; the two dead paths are repaired with tests that fail if they regress to silence.

**Status — not started.** Verified at [analytics_platform/api.py:766](analytics_platform/api.py:766): the `review` endpoint still takes no `authorization` header, and still passes `by=body.by` — a caller-supplied string — straight into `brain.approve`. Anyone who can reach the API can approve any knowledge node as any reviewer identity, and the audit event records whatever name they supplied.

**Dependencies:** its Task 1 references `CompanyBrain(..., index=...)` from plan 1 Task 8, which is done — so it is unblocked.

### Ordering note

The index's own suggested order is A2 → A3 → A4; the order carried into this handoff is A3 → A4 → A2. Either works — all three are unblocked and A2/A3 are independent of each other.

The one thing worth weighing before committing to that order: **A2 is the only one of the three that closes an authorization gap.** It is not a feature, it is an unauthenticated write path into the system of record for company knowledge, and it stays open for as long as A2 sits last. If the platform is only ever reachable on localhost that may be acceptable; if it is reachable by anything else, I would pull A2 forward. Related: open item #1 in section 3 (the unauthenticated export endpoint under a CORS wildcard) is the same class of gap on the read side.

---

## 5. Two things that need your attention

### 5a. `frontend/AGENTS.md` is genuine Next.js output, not prompt injection — no action needed

During this workstream, three agents (including me) flagged `frontend/AGENTS.md` as suspected prompt injection: it claims to be auto-generated and re-added by `next dev`, instructs any agent reading it to consult `node_modules/next/dist/docs/` before writing code, and tells the agent to commit the block with its work. It is auto-included into every agent session touching `frontend/` via `frontend/CLAUDE.md`'s `@AGENTS.md`. No agent acted on it; it was treated as untrusted data throughout and had no bearing on any code or finding.

**That suspicion was wrong.** I verified it directly: `node_modules/next/dist/server/lib/generate-agent-files.js` exists, `node_modules/next/dist/docs/` exists, the installed Next.js is 16.3.0, and the generator source contains the exact block verbatim (the header string at line 56 and the "written and re-added by `next dev`" line at 60). It is legitimate framework tooling shipped by Next.js, doing what it says.

Nothing needs deleting. Two things are still worth knowing:

- The file is checked into git (`577d116`), so `next dev` regenerating it will not produce diff noise.
- Its content does instruct agents to read vendor docs and to commit the block. That is Next.js's intent, not an attack — but it is a real example of a dependency injecting instructions into every agent session in this repo. If you would rather agents not receive framework-authored directives automatically, the lever is `frontend/CLAUDE.md`'s `@AGENTS.md` include, not the generated file.

### 5b. The `/create-pr` request cannot be fulfilled as-is

A pull request was requested against `BusinessAnalyst-AbhinavGupta/ai_analytics_advanced`, branch `main`. All of this work was committed **directly to `main`** per the project's standing convention, so there is no source branch — a `main` → `main` PR is not possible.

If you want a reviewable PR, the options are: branch the relevant commits off an earlier base and open a PR from that branch, or review the range directly (`git log 3eeee4e..8af2ac6`). Say which and I can set it up.

---

## 6. Running and testing it

Backend (the server entrypoint takes a `serve` argument — bare `python -m analytics_platform` runs the demo and exits):

```bash
.venv/bin/python -m analytics_platform serve 8000
```

Backend tests:

```bash
.venv/bin/python -m pytest tests/ -q
```

Frontend type check:

```bash
cd frontend && npx tsc --noEmit
```

**Environment note:** the venv's process line in `ps` shows the *base* Homebrew interpreter path, not the venv path. Testing that base interpreter directly will falsely report `docx`, `fastapi`, and `pandas` as missing. Always use the absolute `.venv/bin/python`.

### Live verification performed (not just unit tests)

Against the running backend, real tenant `tnt_d23cd823d4c6`, real 12-turn conversation `conv_0e337636b9de`:

- Markdown export → 200, `text/markdown`, 1401 bytes, both `filename=` and `filename*=` present and correct.
- Word export → 200, correct OpenXML content type, 37342 bytes, opens in `python-docx` with the expected headings.
- Error paths: empty `answer_ids` → 400; unknown `answer_id` → 400; unsupported format → 400; unknown conversation → 404.
- Full browser download chain verified earlier with spies on `URL.createObjectURL` and anchor `click()` — correct blob types, sizes, and descriptive filenames for both formats.

---

## 7. Housekeeping

- The SDD workspace `.superpowers/sdd/2026-08-15-storyline-export/` (briefs, per-task reports, review packages, ledger) was deleted after the final review, per the skill's own lifecycle. The final review was preserved to `docs/superpowers/reviews/` because its deferred-findings list is the backlog in section 3.
- `README_humanized.md` (untracked) and `tmp/api.log` (modified) were already dirty before this workstream and were deliberately kept out of every commit. Note that `tmp/api.log` was overwritten while restarting the backend for live verification — it is a log file, but if you were keeping its prior contents, they are gone.
- `HANDOFF.md` (this file) is untracked. Commit it if you want it in history.
