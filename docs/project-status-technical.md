# Remediation Program — Status (2026-08-14)

Technical reference. For the plain-language version, see
[project-status-summary.md](project-status-summary.md).

Source of truth for scope and rationale: [docs/superpowers/plans/2026-08-13-INDEX.md](superpowers/plans/2026-08-13-INDEX.md)
and the five plan documents it indexes. This document records **status**, not
design — read the plan docs for full architectural detail.

## Where this came from

An architecture evaluation on 2026-08-13 found five structural gaps in
`ai_analytics_advanced`, spanning tenancy, retrieval, governance, skills, and
confidence scoring. Two of the five gaps have since been fully remediated and
merged to `main`. This document tracks all five.

## 1. Tenant Store Isolation — MERGED (main, PR #6, commit `3d58a63`)

**Problem:** every tenant is a different company, but isolation was a
`WHERE tenant_id = ?` clause on a shared SQLite file, not a file boundary. The
default path (`data/platform.db`) held four tenants co-mingled. Isolation
depended on an environment variable being set correctly, not an invariant.

**What was built:**
- Schema split into a **control plane** (`tenants`, `scheduler_state`,
  `api_logs`, `auth_principals` — one shared file) and a **tenant plane**
  (all other tables — one file per company at `tenants/<id>/tenant.db`).
- `TenantStoreProvider`: maps `tenant_id → Store`, caches connections
  (thread-safe), refuses ids that escape the tenants root, and stamps each
  database file with its owning tenant_id so opening company A's file as
  company B raises `TenantIsolationError` instead of silently mixing data.
- All tenant-scoped services (Stakeholder, Triage, Junior, Onboarding,
  Observability) now resolve a store per call via the provider rather than
  holding one shared `Store`.
- `adopt-db` CLI command migrates a legacy shared-schema file into the new
  per-tenant layout (read-only source, explicit tenant_id required, refuses
  a source that doesn't exist).
- Real production data (`tenants/DTDL/platform.db`, 1245 knowledge nodes,
  tenant `tnt_d23cd823d4c6`) adopted into the new layout and row-count
  verified before the legacy file was removed.

**Impact:** tenant isolation is now a filesystem boundary. Per-tenant export,
backup, and deletion are single filesystem operations (confirmed later, as a
side effect: `RetentionService.delete_tenant` needed zero code changes to
correctly delete a tenant's search index once Plan 1 added it — the file
boundary handled it automatically). Existing `tenant_id` columns remain as
defence-in-depth, not the primary control.

**6 tasks, all complete, reviewed, no unresolved findings.**

## 2. Brain Retrieval Rebuild — MERGED (main, commit `50c7f63`)

**Problem:** semantic search had never executed in production. The vector
index (ChromaDB) was wired into 1 of 12 construction sites, and the
Stakeholder Analyst — the component that actually answers questions — was not
one of them. Where it was wired, every query raised
`ValueError: Expected where to have exactly one operator`, caught by a bare
`except: pass`. Retrieval silently degraded to
`title LIKE '%<the user's entire question>%'`, which matches nothing —
so the Brain reported itself empty regardless of how much knowledge it held.

**What was built:**
- ChromaDB removed entirely (dependency, code, and on-disk `.chroma_db`
  directories). Zero new dependencies added.
- SQLite-native hybrid retrieval, both legs living in the tenant's own
  database (from Plan 0):
  - **Lexical leg:** FTS5 + BM25 over `knowledge_fts`, with user-question
    sanitisation into safe FTS5 match expressions and SQL-side candidate
    chunking past SQLite's 900-parameter limit.
  - **Dense leg:** normalised sentence-transformer embeddings stored as BLOBs
    in `knowledge_vectors`, brute-force cosine similarity (appropriate at the
    few-thousand-node-per-tenant scale actually in play).
  - **Fusion:** Reciprocal Rank Fusion (RRF, k=60) combines the two rankings
    by position (score-free, no per-corpus tuning), then a confidence signal
    (review status + freshness) acts strictly as a tie-breaker — never able
    to override a real relevance difference (see final-review fix below).
- `BrainIndex` + a configurable embedder (env-configurable model, explicit
  null fallback for embeddings-disabled deployments) injected at every one of
  the (now audited) construction sites across the codebase — Stakeholder,
  Triage, Junior, JuniorWorker, Onboarding, Research, Anomaly, Pipeline, API.
  Embedder is a process-wide singleton (loads a real ML model); `BrainIndex`
  is cheap and constructed per-tenant wherever a tenant's store is resolved.
- `reindex_tenant()` + a `reindex` CLI command to backfill any nodes written
  before this system existed.

**Final whole-branch review (the last gate before merge) found and fixed:**
- **Critical:** `rank_nodes()`'s confidence multiplier (range `[1.0, 1.3]`)
  overlapped RRF's real per-rank score gaps closely enough that a
  higher-confidence, lower-relevance node could outrank the true best match —
  in one hand-verified case, pushing the correct answer from rank 1 to rank 8
  of 12, which would have dropped it out of the `limit=3` window the
  Stakeholder Analyst answers from. Fixed: confidence is now a strict
  secondary sort key, unable to move a node across any real fused-score
  difference. A new test using real `rrf_fuse()` output (not synthetic
  scores) guards against regression.
- **Important:** the dense vector leg could silently return empty (wrong
  embedding model, tenant never backfilled) with no log line — now logs a
  WARNING naming the tenant and expected model.
- **Important:** `embedding_enabled`/`embedding_model`/`embedding_query_prefix`
  existed as config fields but were never read from environment variables —
  now wired into `Settings.from_env()` like every other setting.
- **Important:** the knowledge-search API endpoint's caller-supplied `limit`
  had no ceiling and amplified 25x downstream on an unauthenticated-by-default
  route — now clamped to 200 at the API boundary.

**Impact:** semantic and lexical search both function end-to-end for the
first time. The Stakeholder Analyst can retrieve and cite real Brain content
instead of operating on an empty index. Test suite grew from 276 passing
tests (pre-Plan-0 baseline) to 360 passing / 1 skipped, 0 failed.

**9 tasks + 1 final-review fix round, all complete, reviewed clean.**

## 3. Brain Governance — NOT STARTED

**Problem:** `AGENTS.md` requires human approval before anything becomes
company fact. Three code paths currently disagree: the junior background
worker self-approves its own low-level findings (cap 500) despite a
docstring claiming it never writes the Brain; the review API endpoint calls
`brain.approve` with no auth gate and a caller-supplied reviewer identity
string; the AI senior prompts an LLM for a verdict and approves
unconditionally regardless of what the model actually said. Two further
write paths (`bulk_ingest_json`, `evaluate_kpis`) call methods that don't
exist and fail into silence, so proactive KPI monitoring has never fired.

**Planned fix:** auto-promotion becomes an opt-in, default-off tenant
setting; the review endpoint takes its reviewer identity from a verified
principal, not a caller-supplied string; the AI verdict is parsed and
decides (defaulting unparseable output to "revise," never "approve"); the
two dead paths get repaired with regression tests.

**5 tasks.** Task 1 depends on Plan 1's `CompanyBrain(..., index=...)`
signature — now available. **Ready to start.**

## 4. Skills Portability — NOT STARTED

**Problem:** the platform's one analytics skill has never executed — its
template engine substitutes `$key` while every template uses `{{KEY}}`, so
the literal string `{{STEP_1_PAGE}}` is sent to the warehouse. The skill also
hardcodes one organisation's table names and column values in the shared
repository, offered to every tenant. Skills only run when the Brain returns
nothing, so as retrieval improves (as it just did), they become progressively
unreachable; successful runs are discarded rather than filed for review.

**Planned fix:** a skill declares an abstract data contract; each tenant
supplies a binding mapping it onto their real tables
(`tenants/<id>/skill_bindings.json`, gitignored, alongside that tenant's
database from Plan 0). The SQL template becomes identical across tenants.
Skill selection becomes independent of Brain hits rather than a fallback,
and results land as reviewable `CANDIDATE` findings.

**5 tasks.** Independent of Plans 1 and 2; assumes Plan 0's per-tenant
layout (available). **Ready to start.**

## 5. Frameworks & Confidence — NOT STARTED

**Problem:** the platform's four documented "friction types" and its Metrics
Tree framework return zero grep hits in the codebase — they exist only in
docs no model has ever been shown. Separately, of six documented confidence
dimensions only two are ever updated; `freshness` is pinned at 1.0 forever
despite being read for ranking (Plan 1's fusion now depends on it
meaningfully). `AGENTS.md` also names a `data_quality` dimension that was
never implemented.

**Planned fix:** one versioned `frameworks.py` feeding stakeholder/junior
prompts, plus a validator flagging driver recommendations with no guardrail;
freshness decays exponentially, computed on read; evidence is seeded from a
node's backing run; a scheduled sweep marks decayed nodes stale; documented
and stored confidence dimensions are reconciled.

**4 tasks.** Depends on Plan 1's ranking change (now merged). **Ready to
start.**

## Built but not merged: PR #3

[PR #3](https://github.com/BusinessAnalyst-AbhinavGupta/ai_analytics_advanced/pull/3)
(`feat/stakeholder-vector-sql-synthesis`) — **OPEN, not merged, held
deliberately.**

It was branched before Plan 0 merged, and constructs `StakeholderService`/
`TriageService` with the pre-isolation `store: Store` + `vector_store: Any`
signature. `main` now takes `stores: TenantStoreProvider` with no
`vector_store` parameter — GitHub confirms `mergeable: CONFLICTING`, and the
conflict isn't resolvable by picking a side; it needs re-implementation
against the current constructors.

**Decision (2026-08-14, confirmed):** hold it, and port only its two durable,
backend-agnostic features onto the now-merged Plan 1 `search()`:
- `_extract_search_intent()` — an LLM call that distills a verbose question
  into a 2-4 word topic before retrieval, improving recall.
- `_synthesize_sql()` — lets the stakeholder write ad-hoc SQL from approved
  context instead of only reusing an approved query verbatim.

Not portable / superseded: the Chroma `$and` filter fix, `reindex_vectors()`
(Chroma-specific — Plan 1 shipped its own `reindex_tenant()`), and the
`vector_store` threading through `AppContext` (Plan 1's `embedder` replaces
it).

**Status now that Plan 1 has merged:** the hold condition is satisfied. Next
action is to port the two features above onto current `stakeholder.py`, open
a new PR, and close #3 without merging (its diff no longer applies cleanly).

## Deferred / known technical debt

Recorded during review, explicitly not blocking, not yet scheduled:
- `analytics_platform/database.py:19` — dead duplicate logger, harmless.
- `analytics_platform/cli.py`'s `reindex` command doesn't guard against a
  typo'd `--tenant` silently creating an empty tenant database.
- `BrainIndex.reindex_tenant`'s `batch` parameter is accepted but unused.
- `get_sentence_embedding_dimension()` emits a `FutureWarning` on the
  installed `sentence-transformers` version (renamed upstream) — degrades
  loudly when it breaks, not urgent.
- `AnomalyService` has zero construction call sites anywhere in the repo —
  Plan 1 wired an embedder into currently-dead code.
- The `reindex` CLI command has no operator-facing documentation outside the
  plan file itself.
- Orphaned `.chroma_db` directories may still be present on disk in
  deployments that ran the old code — harmless, nothing reads them, safe to
  delete manually.

## Suggested next order

Per the plan index's dependency ordering: **Plan 2 (Governance) and Plan 3
(Skills Portability) are both unblocked and independent of each other** —
can run in either order or in parallel. **Plan 4 (Frameworks & Confidence)**
is also unblocked (its one dependency, Plan 1's ranking change, is merged),
but is lowest urgency of the three. Porting PR #3's two features can happen
alongside any of these — it's a small, independent piece of stakeholder.py
work.

## Process note

Per current instruction, all future work lands as direct commits to `main`
— no new feature branches or worktrees.
