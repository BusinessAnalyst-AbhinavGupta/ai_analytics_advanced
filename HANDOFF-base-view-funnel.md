# Handoff — real base view + funnel drop-off analysis

Written 2026-08-19, end of the session that implemented Plan A and then tested it
against live tenant data. Read this before touching anything; the first two
sections will save you the hours they cost me.

---

## Start here: two traps that already burned a session

**1. The backend is not uvicorn.** It runs as:

```
.venv/bin/python -m analytics_platform serve 8000
```

`analytics_platform.api` has no module-level `app` — only `create_app()`. So
`pkill -f "uvicorn analytics_platform"` **matches nothing**. The old process
keeps serving, the replacement fails to bind, and `/health` answers happily the
whole time. Several "live test results" in the previous session were run against
stale code because of this.

```bash
pkill -f "analytics_platform serve"
```

After any restart, confirm the process actually turned over before trusting a
result — check the start time via `lsof -ti tcp:8000` or tail `tmp/api.log` for a
fresh startup banner. Compare it against `git log -1` to be sure your fix is live.

**2. Live queries need a Chrome tab sitting on Metabase.** Queries run through
`BrowserSessionExecutor`, which does a same-origin fetch inside a real Chrome tab
at `metabase.om.yo-digital.com`. If that tab navigates away you get:

```
needs_login: Active tab is not Metabase
```

That is what killed the last discovery query. It is not a code fault. Re-point
the tab before starting, or step 1 below fails immediately.

**Environment for a live LLM:**

```bash
set -a; . ./.env; set +a
eval "$(grep -E '^export OPENROUTER_API_KEY=' ~/.zshrc)"
```

`.env` already sets `ANALYTICS_MB_LIVE=1` and `ANALYTICS_WATCHER=1`.

**Tenant:** `tnt_d23cd823d4c6` (Acme Retail GmbH) — this *is* the "DTDL" tenant.
It is now the only tenant; two empty demo duplicates were deleted, backed up to
`~/Documents/ai_analytics_tenant_backups/2026-08-18-stray-tenants/`.

---

## Next session, in order

1. **Measure the true history extent (`MIN(event_date)`), unfiltered**
2. **Check whether `attribute_checkout_type` and `service_line` are single-valued
   per session** — that decides whether they need attribution rules or can be
   carried directly
3. **Write the base view with no date filter, register it as DRAFT** for the
   user's approval
4. **Add the "ask when no timeframe is given" clarification branch**
5. **Re-run the consent drop-off question end to end**

Notes on each are below.

---

## Why this work exists

The user asked a real question through the UI:

> "why are users dropping off after reaching consent page (there is also an
> alternate of consent page i don't quite recall right now) and not going to the
> order place. break this down by service line and category and limit to DE.
> Also let's just work on last 30 days worth of data."

The answer was shallow, both SQL blocks looked alike, and both ended in `LIMIT 5`.
The cause was **not** the Plan A pipeline failing. The question never reached it:

```
MODE: SKILL_EXECUTED_ANALYSIS
caveats: used specialized skill: advanced-funnel-dropoff-analysis
python_cells: []
```

`answer()` tries `_run_analyst_pipeline` first (stakeholder.py:370) and only falls
through to skill matching (stakeholder.py:482) when the pipeline returns `None`.
It returned `None` because **the only base view was a one-day toy** I registered
during testing:

```sql
WHERE event_date = DATE '2026-08-17'   -- one day, no natco, MIN() attribution
```

Its own description says `TEST ARTIFACT: MIN() stands in for a real attribution
ranking.` A 30-day DE consent→order funnel cannot be built on it, so the pipeline
gave up and the skill caught the question.

**That toy view is still registered and still APPROVED.** Replacing it is the
whole job. Do not repeat my mistake of self-approving one.

---

## Discovery already done (live, against Athena)

Run through the in-process executor. All verified, except where noted.

**Natcos and volume, last 30 days:**

| natco | sessions | events |
|-------|----------|--------|
| de    | 704,827  | 25,327,943 |
| pl    | 132,555  | 3,213,678 |
| sk    | 38,442   | 2,138,139 |
| cz    | 40,599   | 1,524,762 |
| plrb, mk, hr, me | smaller | |

Table is `silver_layer.t_link_journey_checkout_com`, ~628M rows, 44 columns.

**⚠ The date range is NOT established.** I reported 2026-07-19 → 2026-08-17, but
my query contained `WHERE event_date >= date_add('day',-30,CURRENT_DATE)` — that
is my own filter reflected back, not the extent of the data. **Step 1 exists to
fix this.** Do not assume 30 days is all there is.

**The "alternate consent page" — solved.** It is not a second page name. It is
the same page under a different checkout flow:

| page_name | attribute_checkout_type | sessions |
|-----------|------------------------|----------|
| checkout/consent | normal | 108,800 |
| checkout/consent | express-checkout | 33,933 |
| checkout/consent | (null) | 2,746 |

The skill filters `LOWER(page_name)='checkout/consent'`, which sweeps in **both**
and never separates them. About a quarter of consent traffic is express-checkout,
blended into one drop-off number. This is the missing depth.

**Funnel vocabulary (DE, 30d):**

- Entry: `checkout/consent` — 136,729 sessions
- Completion action: `purchaseSuccess` — 102,829 sessions
- `checkout/OrderConfirmation` — 149,253 sessions. Note the ~46k gap against
  `purchaseSuccess`; worth investigating separately.
- Other pages: `BASKET` (621,910), `checkout/account`, `checkout/payment`,
  `checkout/personalInfo`, `checkout/identification`, `checkout/appointment`,
  `checkout/shipping`, `checkout/providerChange`

**Errors hide inside `page_name`** as suffixes:

```
BASKET/err/Die Browser-Anfrage an unseren Server kann nicht bearbeitet werden...
checkout/OrderConfirmation/err/Etwas ist schief gelaufen...
```

So any exact-match filter on a page silently drops its errored variants. The
skill's `= 'checkout/consent'` has this bug latent. Prefer `LIKE 'checkout/consent%'`
or split on `/err/`.

**The stored column profile is not trustworthy for design.** It came from a
truncated 2,000-row single-day sample: `natco_code` shows `distinct=1`,
`event_date` `distinct=1`, and every `sample_values` is empty. `values_complete`
is correctly `False` everywhere. Re-profile or query directly.

---

## Design decisions the user made

**No date filter in the base view.** Explicit instruction:

> "Don't let the queries fail if data of last 60 days or sometime older is asked.
> We're anyway aggregating when pulling data from metabase so it'd not be an
> issue. I don't want any limits of only last 30 days."

Rationale, and a correction to something I first said wrong: a 60-day question
does **not** fail today — `missing_time_ranges` drops the cube from reuse and the
verdict falls to `decision="retrieve"`, which re-queries the warehouse
(data_manager.py:241-249). The real hard limit would be a window baked into the
**base view's own SQL**, because that CTE is inlined verbatim into every derived
query — nothing can reach past it. So: no window in the view.

The population is "checkout journey sessions", not "sessions in a window". Time
becomes a per-question slice with `time_column = event_date`. Ask for 15 days →
filtered locally from the cube, no warehouse trip. Ask for 90 → re-retrieved.
Neither is capped. Partition pruning still works, because Athena pushes the outer
date predicate into the inlined CTE.

**Dimensions are available, not mandatory.** The user pushed back on carrying
`attribute_checkout_type`:

> "may be relevant for this question but may not be relevant for other questions
> in future so can't necessitate this in queries in general"

Correct instinct, but carrying a column does not force it into any query.
`dimension_columns` lists what is *available to group by*; the planner picks per
turn, and cube size depends only on dimensions actually chosen. An unused column
costs nothing.

The genuine cost is **grain**, which is what step 2 checks: if one session can
carry more than one `attribute_checkout_type` (or `service_line`), collapsing it
needs an explicit attribution rule. Adding a fan-out column to `GROUP BY` silently
changes the grain and double-counts. `propose_attribution_rules()` in `junior.py`
exists for this, and `compose_grain_probe()` / `record_grain_check()` in
`base_view.py` verify it.

**Ask when no timeframe is given.** New behaviour, not config. The planner
currently proceeds on whatever the LLM infers, so you can silently get a default
window nobody chose. `NEEDS_CLARIFICATION` already exists (skills use it for
missing params) but needs wiring into the analyst pipeline. Real work plus tests.

---

## Target shape for the base view

Session grain, no date filter, dimensions available for the planner to choose:

- Grain: `session_id`
- Dimensions: `service_line`, `category`, `sub_category`, `natco_code`, and
  `attribute_checkout_type` (pending the step-2 fan-out check)
- Measures/flags: reached consent, reached `purchaseSuccess`, event counts
- `time_column = event_date`

Then the consent question becomes **one cube over one population**, and "which
reason drives mobile tariffChange's 83.6%" is answerable — unlike today, where
segments and reasons are two queries that are never joined.

**Register as DRAFT (`ReviewStatus.CANDIDATE`) and stop.** AGENTS.md requires
human-in-the-loop approval. Approval is the user's step, not yours.

---

## State of the code

Plan A is complete: 16 tasks, all committed. **PR #7** —
https://github.com/BusinessAnalyst-AbhinavGupta/ai_analytics_advanced/pull/7 —
carries 29 commits on branch `plan-a-analytical-workspace`. `origin/main` was
deliberately left where it was so the whole change is reviewable. Repo convention
is otherwise to commit straight to `main`, no feature branches.

881 tests passing, 1 skipped.

**Verified working live** (two-turn sequence, run twice, identical both times):

```
turn 1   1 warehouse query    coverage: retrieve   pop: ea34018e9a
turn 2   0 warehouse queries  coverage: reuse      pop: ea34018e9a
```

Shares summed to 100%, totals to 31,273 = the full population, same
`population_hash` across turns. Cube reuse works; a follow-up re-cut costs nothing.

**Defects found and fixed live this session** — worth knowing, since several were
silent and each looked like success from the inside:

1. Profiles never resolved (qualified vs bare table names) → cube guard failed
   closed on every dimension → everything fell through to one-off LLM SQL. Fixed
   by resolving names against profile names as well as the catalog.
2. Empty profiling counted as success, emitting no caveat.
3. Synthesis narrated every cube from `rows[:3]`, unsorted — a correct 5-row cube
   lost `mobile` (32% of the population) from an answer that read as complete.
4. "COMPLETE" was asserted about the query rather than the population, so a
   `LIMIT 1` re-cut became "the only category present, therefore 100%".
5. A slice of a small cube now arrives with the whole cube behind it.
6. Coverage matched measures by alias, so a renamed `COUNT(*)` re-queried.
7. Truncated sample claiming complete value lists; NULL grain key reported as a
   duplicate; editing an approved base view kept its approval; Metabase's own row
   cap undetected while `max_transport_rows` sat 5× above it.
8. Skill results were cut to five rows in **three** independent places — SQL
   `LIMIT 5`, `rows.head(5)` in the engine, and a call site that flattened every
   step into a nested blob. All three fixed; `PREVIEW_ROWS = 500` now, and
   `_skill_context` labels each step with its true row count and states that
   separate steps are not joined.

---

## Open items

- **Measure identity is by alias, not expression.** A renamed `COUNT(*)` reads as
  a missing measure. Mitigated by showing the planner existing measure names; the
  robust fix is carrying measure expressions in `ExtractMeta`.
- **Skill banner comments** are surfaced raw, and parameter substitution is
  applied to comments as well as code, producing self-referential nonsense like
  `--   de     -- lowercase natco code, e.g. 'de'`. User explicitly parked this.
- **The skill path bypasses Plan A entirely** — no population hash, no cube reuse,
  no governance. Building the base view moves funnel questions onto the pipeline,
  but the skill path itself remains ungoverned. Larger decision, deferred.
- **`tenants/DTDL/`** still exists on disk — pre-migration data, nothing reads it.
  May be the only copy of the pre-migration state; left alone deliberately.
- Two uncommitted runtime logs in `tmp/` (`api.log`, `test_api.log`). Checked for
  secrets — none, only an HF warning string containing "TOKEN".
