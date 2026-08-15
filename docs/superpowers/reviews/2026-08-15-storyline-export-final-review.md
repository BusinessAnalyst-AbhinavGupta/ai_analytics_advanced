# Final whole-branch review — Storyline Export (04f11fa..HEAD, 8 commits)

Reviewer: final gate review. Scope: `analytics_platform/storyline.py`, `analytics_platform/api.py`
(export route + CORS), `analytics_platform/stakeholder.py`, `analytics_platform/database.py`,
`analytics_platform/execution/dataframe_cache.py` (dependency of the core logic),
`frontend/src/store/useStore.ts`, `frontend/src/components/StakeholderChat.tsx`,
`tests/test_storyline.py`, `tests/test_stakeholder.py`.

**Verification run during review** (no files modified; `git status --short` unchanged apart from
this review file):
- `.venv/bin/python -m pytest tests/ -q` → **462 passed, 1 skipped**
- `cd frontend && npx tsc --noEmit` → **0 errors**
- Three findings below (C1, C2, M6) were reproduced empirically with throwaway scripts in the
  scratchpad; the reproductions are quoted inline.

## Verdict

**CHANGES REQUIRED** — 2 Critical, 8 Important, 9 Minor.

The architecture is sound: `assemble_storyline` really is pure and DB-free, the tenant scoping
chain is correct (see "Cleared on inspection"), the migration is non-destructive, and the e2e test
at `tests/test_stakeholder.py:900` genuinely proves the headline behaviour. What is not ready is
the *identity* the dependency resolution is keyed on: `df_label` is not unique within a
conversation, and the code assumes it is. That produces a silently **wrong** Code Appendix — the
worst possible failure for an artifact whose entire purpose is provenance. Separately, a plausible
conversation title makes the endpoint return a hard 500.

---

# Critical

## C1 — `df_label` is not unique per conversation; the Code Appendix can attribute a selected Python turn to the *wrong* SQL turn

**Where:** `analytics_platform/storyline.py:48-50` (`label_to_message`), enabled by
`analytics_platform/execution/dataframe_cache.py:75-81` (`next_label`).

```python
# storyline.py:48
label_to_message = {
    m["produced_df_label"]: m for m in all_messages if m.get("produced_df_label")
}
```

This is a last-wins dict. It is correct only if `produced_df_label` is unique across the
conversation's persisted answers. It is not, for two independent reasons:

**(a) LRU eviction recycles labels.** `next_label` derives the next label from the *in-memory*
frames only, and `ConversationDataCache` is LRU-bounded at `max_frames_per_conversation=5`
(`dataframe_cache.py:39,55-56`). Once a frame is evicted its label becomes free again. Reproduced:

```
labels issued: ['df_1', 'df_2', 'df_3', 'df_4', 'df_5', 'df_6', 'df_1']
frames now:    ['df_3', 'df_4', 'df_5', 'df_6', 'df_1']
```

**(b) The cache is in-memory only** ("Never persisted to disk", `dataframe_cache.py:5`). After an
API restart, the next SQL turn in a *long-lived, persisted* conversation is issued `df_1` again,
guaranteed.

**Concrete failure:** In conversation `c1`:
- Turn 2 (`ans_A`) runs `SELECT revenue FROM events WHERE region='EU'` → `produced_df_label="df_1"`.
- Turn 3 (`ans_B`) answers via Python over that frame → `python_cells=[{"df_label": "df_1", ...}]`.
- Turns 4–8 run five more SQL turns. On turn 8 (`ans_H`) `next_label` returns `"df_1"` again
  (proof above) for a completely different query, `SELECT refunds FROM tickets`.
- The user selects **only turn 3** and exports.

`label_to_message["df_1"]` resolves to `ans_H`. The Code Appendix presents
`SELECT refunds FROM tickets` — a query that ran *after* the Python turn, over unrelated data — as
the provenance of turn 3's number, marked "included as a dependency". A stakeholder auditing the
report is shown SQL that did not produce the figure. The correct SQL (`ans_A`) is absent.

**Suggested fix:** stop keying provenance on a recyclable display label. Record the producing
answer's id on the cached frame and on the python cell, and resolve by that id:
1. `CachedFrame` gains `source_answer_id`; `ConversationDataCache.put(...)` takes it.
   `stakeholder.py:326-327` already knows the label at cache time — but the answer id is minted
   later in `_record`, so either mint the id earlier or write the frame's `source_answer_id` back
   after `_record` returns.
2. `stakeholder.py:304` persists `{"code":..., "df_label":..., "source_answer_id": <producer>, ...}`
   in `python_cells`.
3. `assemble_storyline` looks the dependency up by `p["source_answer_id"]` via a `by_id` map
   (which already exists at `storyline.py:47` — see M1/I2), falling back to the label lookup only
   for rows written before the change.

Also make `next_label` monotonic per conversation (track a counter that eviction does not reset,
or seed it from `MAX(produced_df_label)` in `stakeholder_answers`) so the same label is never
issued twice within one conversation even under the fallback path.

---

## C2 — A conversation whose title contains any non-Latin-1 character makes the export endpoint return HTTP 500

**Where:** `analytics_platform/api.py:1091` and `:1104`.

```python
title_slug = "".join(c if c.isalnum() else "-" for c in (conv["title"] or "storyline")).strip("-") or "storyline"
...
headers={"Content-Disposition": f'attachment; filename="{filename}"'}
```

`str.isalnum()` is Unicode-aware. CJK, Cyrillic, Devanagari, Arabic, Greek, Hebrew, Thai
characters and full-width digits are all alphanumeric, so they survive the slug verbatim. Starlette
encodes response headers as `latin-1` inside `Response.__init__` → `init_headers`. Reproduced:

```
slug: 売上分析
RAISED: UnicodeEncodeError 'latin-1' codec can't encode characters in position 22-25: ordinal not in range(256)
```

**Concrete failure:** Conversation titles are auto-derived from the user's first question
(`stakeholder.py:138`: `title = question.strip()[:80]`). A tenant analyst asks
`"Выручка по городам за август"` or `"売上分析"`. Every subsequent export of that conversation —
Markdown *and* docx — raises `UnicodeEncodeError` before the response is built. The frontend's
`if (!res.ok) return;` (see I5) swallows it, so the user clicks Export and nothing happens, ever,
with no error anywhere in the UI. There is no user-facing workaround except renaming the
conversation to pure ASCII.

Note this is *not* the middleware-testing gap — `Response.init_headers` runs in the constructor, so
a direct-closure test would have caught it. It is simply untested (see I8).

**Suggested fix:** emit an ASCII-safe fallback plus RFC 5987/6266 UTF-8 form, and bound the length:

```python
from urllib.parse import quote
raw = (conv["title"] or "storyline")[:60]
ascii_slug = "".join(c if (c.isascii() and c.isalnum()) else "-" for c in raw).strip("-") or "storyline"
utf8 = quote(f"{raw}.{ext}", safe="")
headers={"Content-Disposition":
         f'attachment; filename="{ascii_slug}.{ext}"; filename*=UTF-8\'\'{utf8}'}
```
The frontend regex at `useStore.ts:305` already reads only the quoted `filename=` form, so it keeps
working unchanged; browsers that understand `filename*` get the real title.

---

# Important

## I1 — `import docx` at module scope takes down the entire API if python-docx is missing or broken

**Where:** `analytics_platform/storyline.py:10`, consumed by `analytics_platform/api.py:67`.

`api.py` imports `storyline` unconditionally at module load, and `storyline` imports `docx` at
module load. `python-docx` is pinned in both `requirements.txt:38` and
`requirements-advanced.txt:94`, but it is an optional-format dependency with a native transitive
dep (`lxml`).

**Concrete failure:** an operator deploys with `requirements.txt` unpinned/partial, or `lxml`'s
wheel fails to build on the target platform. `import analytics_platform.api` raises `ImportError`
and **every** endpoint — `/health`, `/tenants`, the whole stakeholder chat, the WebSocket — is
dead. A missing Word renderer should at worst disable Word exports. Note `api.py:28-35` already
establishes the guarded-import convention in this file.

**Suggested fix:** move `import docx` inside `render_docx()`; on `ImportError` raise a typed error
the route maps to `503 {"detail": "docx export unavailable: python-docx not installed"}`, leaving
Markdown export and the rest of the API working.

## I2 — Dependency resolution is one level deep and SQL-only; the dead `by_id` map is the vestige of the missing walk

**Where:** `analytics_platform/storyline.py:73-81`.

```python
dep_msg = label_to_message.get(p.get("df_label"))
if (dep_msg is not None and ... ):
    for q in dep_msg.get("queries_run", []):        # <-- only queries_run
        appendix.append(...)
    dependency_answer_ids_added.add(dep_msg["answer_id"])
```

Two gaps:
1. **`dep_msg`'s own `python_cells` are never emitted.** If the producing turn were itself a Python
   turn, the appendix would include *nothing* for it while claiming to include its provenance.
2. **No transitive walk.** `dep_msg`'s own `python_cells[].df_label` is never resolved, so a
   two-hop chain (selected Python → unselected Python → unselected SQL) loses the SQL entirely.

**Reachability today:** latent, not live. `_synthesize_and_execute_python`
(`stakeholder.py:731-778`) never calls `data_cache.put`, so a Python turn never sets
`produced_df_label` (asserted by `test_python_turn_records_no_produced_df_label`). The moment
anyone caches a Python result — the obvious next feature, since the plan's stated goal is
multi-step analysis — this silently produces incomplete appendices with no error.

**This resolves the deferred Minor about `by_id`.** `by_id = {m["answer_id"]: m for m in
all_messages}` at `storyline.py:47` is not merely dead code: it is exactly the index a
producer-id-keyed, transitive resolution needs (and that C1's fix requires). Recommendation:
**do not delete it — use it.** Replace the loop body with a worklist:

```python
frontier = [p.get("df_label") for m in selected for p in m.get("python_cells", [])]
seen_labels, emitted = set(), set(selected_ids)
while frontier:
    lbl = frontier.pop()
    if not lbl or lbl in seen_labels: continue
    seen_labels.add(lbl)
    dep = label_to_message.get(lbl)          # -> by_id[...] once C1's fix lands
    if dep is None or dep["answer_id"] in emitted: continue
    emitted.add(dep["answer_id"])
    for q in dep.get("queries_run", []):     ... is_dependency=True
    for p2 in dep.get("python_cells", []):   ... is_dependency=True; frontier.append(p2.get("df_label"))
```

If the team prefers to keep scope tight, the acceptable alternative is: delete `by_id`, and add an
explicit comment plus a test asserting the single-hop SQL-only limitation, so the next author who
caches Python output is forced to confront it. Silently leaving it is not acceptable.

## I3 — An unresolvable `df_label` is dropped with no marker, guaranteeing a provenance-free appendix for every pre-migration conversation

**Where:** `analytics_platform/storyline.py:73-74`.

If `label_to_message.get(p["df_label"])` returns `None`, the loop simply moves on. The reader gets
a Python block referencing `df_1` and no indication that `df_1`'s origin is unknown.

**Concrete failure:** `produced_df_label` was added by `ALTER TABLE ... ADD COLUMN`
(`database.py:227-228`), so every pre-existing row backfills to `NULL` → `""`
(`stakeholder.py:179`). `label_to_message` filters those out (`if m.get("produced_df_label")`).
Therefore **for every conversation created before this feature shipped, no Python turn's SQL
dependency is resolvable**, and the export silently omits it. The user selects a Python turn from
last month's conversation, exports, and receives a Word document whose Code Appendix contains
Python operating on an undefined `df_1`. This directly contradicts the plan's Global Constraint
"No silent failures … never a silently-empty document".

**Suggested fix:** when `dep_msg is None`, append a placeholder entry
(`kind="note"`, `code=f"-- source query for '{label}' is not recorded (pre-dates dependency tracking)"`,
`is_dependency=True`) so the gap is visible in both renderers, and surface a count of unresolved
labels on `StorylineContent` so the API/UI can warn.

## I4 — Token estimate is implemented twice, and the two implementations disagree; the server-side one is dead

**Where:** `analytics_platform/storyline.py:83-92` (`estimated_tokens`, `over_budget`) vs
`frontend/src/components/StakeholderChat.tsx:119-127,138-139`.

`assemble_storyline` computes `estimated_tokens` and `over_budget`, but the export route
(`api.py:1076-1104`) never reads them and no endpoint ever returns them — both fields are dead in
production, exercised only by `tests/test_storyline.py:73`. The live estimate is a client-side
reimplementation with the divisor `4` inlined (`Math.floor(text.length / 4)`) and the threshold
re-declared as a magic constant kept in sync by a comment: `// must match
analytics_platform/storyline.py's WARN_TOKEN_THRESHOLD`.

They already disagree. The backend estimate includes the Code Appendix
(`+ "\n".join(e.code for e in appendix)`, `storyline.py:85`); the frontend's does not
(`StakeholderChat.tsx:121-123` sums only question/answer/facts/caveats). A user selecting three
short Python turns that pull in three long SQL dependencies sees a small number and no warning,
then downloads a document dominated by code. For code-heavy exports the shown figure can be off by
several multiples — always under-reporting, i.e. failing in the direction that defeats the warning.

This also conflicts with AGENTS.md Part 1 ("The UI must be a thin client. All interactions between
the frontend and the backend must occur via API boundaries") and "No Silent Hardcoding" — the
threshold and divisor are business rules duplicated into the client with no
`HARDCODED_REGISTRY.md` entry.

**Suggested fix:** add a cheap `POST .../export/estimate` (or return the estimate on a dry-run flag)
that calls `assemble_storyline` and returns `{estimated_tokens, over_budget, threshold}`; have the
panel render the server's numbers. Delete `estimateTokens` and `WARN_TOKEN_THRESHOLD` from the
component. This kills the divergence and the duplicated constant in one move.

## I5 — The frontend swallows every export failure; the user sees nothing at all

**Where:** `frontend/src/store/useStore.ts:302` (`if (!res.ok) return;`) and the surrounding
`catch (e) { console.error(e); }`.

Every server-side error the plan required to be loud — 404 unknown conversation, 400 empty
`answer_ids`, 400 unknown answer id, 400 unsupported format, and the 500 from C2 — results in the
promise resolving normally. `StakeholderChat.tsx`'s handler then runs its `finally { setExporting(false) }`
and the button returns to `Export (3)`. Nothing is rendered, no toast, no message; the only trace
is the browser console.

**Concrete failure:** the C2 crash above is completely invisible. A user with a Cyrillic-titled
conversation clicks Export repeatedly and concludes the app is broken.

**Suggested fix:** add `exportError: string` to the `stakeholder` slice; on `!res.ok` read
`await res.json()`'s `detail` (falling back to `res.statusText`) and set it; render it in the
Report Builder panel next to the Export button; clear it on the next attempt.

## I6 — No upper bound on selection size or rendered document size

**Where:** `analytics_platform.api.py:1080-1102`; `StorylineExportIn.answer_ids: List[str]`
(`api.py:236`).

`over_budget` being non-blocking is correct per the plan, but "non-blocking warning" is not the
same as "no ceiling at all". `answer_ids` is unbounded, `assemble_storyline` materialises every
turn, `render_docx` builds the whole `Document` in memory and `buf.getvalue()` copies it again, and
FastAPI's `Response` buffers the entire body before sending.

**Concrete failure:** a long-running conversation with ~2,000 answered turns, each with a few KB of
answer text and a multi-KB SQL body. The user clicks "Select all" (`useStore.ts:279-284`, which
selects every message unconditionally) then Export → docx. Peak resident memory is roughly
3–4× the document, and the single-process API is blocked for the whole render — the endpoint is
`def`, not `async def`, so it occupies a threadpool worker, but the memory spike is process-wide
and can OOM the API for all tenants.

**Suggested fix:** reject with 413 above a hard ceiling — e.g. `len(body.answer_ids) > 500`, and
`len(data) > 25 * 1024 * 1024` after render — with a message naming the limit. Keep the 50k-token
soft warning as-is.

## I7 — The export endpoint is unauthenticated and readable cross-origin by any website

**Where:** `analytics_platform/api.py:1076` (no auth parameter) combined with `:506`
(`allow_origins=["*"]`, now with `expose_headers=["Content-Disposition"]`).

The route takes no `authorization` header — unlike `api.py:1172-1215`, which do. With
`allow_origins=["*"]`, any page the user visits can `fetch()` this endpoint and **read the
response body**, because a wildcard origin permits the reading, not merely the sending.

**Concrete failure:** the user has the platform running on `localhost:8000` and browses to an
unrelated site. That page runs `fetch('http://localhost:8000/tenants')` → tenant ids →
`.../stakeholder/<tid>/conversations` → conversation ids → `POST .../export` and exfiltrates the
full text of the analytics conversation, including synthesized SQL over the tenant's warehouse, to
an attacker-controlled host. No credentials are needed because none are required.

**Honest scoping:** this pattern is **pre-existing** and platform-wide — the JSON conversation
endpoints already leak the same content, and `allow_origins=["*"]` was not introduced by this
branch. But this branch adds the first endpoint that packages an entire conversation into a single
downloadable artifact and explicitly widens CORS to expose a response header, so it belongs in this
review. **Recommendation:** treat as a platform-level follow-up (replace `allow_origins=["*"]`
with the configured frontend origin, and require the same `authorization` header the billing
routes use), not as a blocker on this feature alone — but file it, do not drop it.

## I8 — Test gaps: the security-relevant assertion and every failure mode identified above are untested

**Where:** `tests/test_stakeholder.py:140-187`, `tests/test_storyline.py`.

What exists is decent (`test_cors_exposes_content_disposition_for_export_downloads` at
`test_stakeholder.py:161` is a genuinely good middleware-gap regression test, and the e2e at `:900`
has proven teeth per the ledger). What is missing:

1. **No test that an `answer_id` belonging to a *different conversation* is rejected.** This is the
   single highest-value assertion in the feature — the plan makes it a Global Constraint, and the
   implementation is correct (`api.py:1085-1089`), but nothing pins it. A future refactor that
   moves the `known_ids` check to a global `stakeholder_answers` lookup would pass every current
   test while enabling a cross-conversation read. Add: create two conversations, export from
   conversation A passing conversation B's `answer_id`, assert 400.
2. No test for C1 (two turns sharing `produced_df_label`).
3. No test for I3 (a `df_label` no message produces).
4. No test for C2 (non-ASCII conversation title → filename).
5. No test for an unsupported `format` value (the 400 branch at `api.py:1100-1102`).
6. **The renderers are never tested against real `assemble_storyline` output.** Both render tests
   (`test_storyline.py:87,110`) construct `StorylineContent` by hand, so they cannot catch a
   mismatch between what assembly produces and what rendering expects. The e2e test
   (`test_stakeholder.py:900`) stops at `content.code_appendix` metadata and never renders. Add one
   assertion chaining real assembly → `render_markdown` → substring check on the dependency SQL.
7. `test_export_docx_returns_an_openxml_document` asserts only `body.startswith(b"PK")` — true of
   any zip. It does not open the document. `test_render_docx_...` does open it, so the coverage
   exists elsewhere, but the API-level test is weaker than it reads.

**On the middleware-gap question specifically:** I checked what else the direct-closure `call()`
helper could be hiding here. The C2 header-encoding bug is *not* one of them — `Response.__init__`
encodes headers eagerly, so a direct call raises too; C2 is plain missing coverage. The genuine
middleware-only surfaces for this endpoint are (a) CORS exposure, now covered by the new test, and
(b) `legacy_tenant_rewriter` (`api.py:539-553`), which silently rewrites `/stakeholder/1/...` to
`list_tenants()[0]`. That rewriter is pre-existing, is not reachable from this feature's frontend
(`useStore.ts:141` defaults `tenantId` to `''` and it is set to a real id before any call), and so
is not a finding against this branch — but it is the one code path where a request for tenant X
can be served from tenant Y's database, and it is invisible to the entire test suite. Worth a
standalone test at some point.

---

# Minor

**M1 — `by_id` is dead as written.** `storyline.py:47`. Confirmed unused (only occurrence in the
file). See I2: the correct disposition is to *use* it, not delete it. If I2 is deferred, delete it.

**M2 — `id_order` is built as an ordered dict but used only for membership.** `storyline.py:45`,
then `selected_ids = set(id_order)` at `:60`. Two names for one set. The dict's insertion order is
never consulted — turns are emitted in conversation order (correctly, and pinned by
`test_only_selected_turns_appear_in_order`). Collapse to `selected_ids = set(answer_ids)` and drop
`id_order`.

**M3 — The dependency annotation is circular and tells the reader nothing.**
`storyline.py:113-115` and `:137-139` produce
`### df_1 (sql) — (included as a dependency of df_1)`. The label in the parenthetical is the
entry's *own* label. It should name the selected turn that needed it, e.g.
`(included because the selected turn "What's the total?" ran Python over df_1)`. This requires
carrying the requesting `answer_id`/question on `CodeAppendixEntry`. Note
`tests/test_storyline.py:108` asserts the current string verbatim, so the test must change with it.

**M4 — Markdown fence break-out and heading corruption.** `storyline.py:117-119` wraps `e.code` in
a fixed three-backtick fence. Python code containing ``` inside a docstring or a SQL comment
terminates the fence early, and the remainder of the document renders as prose — including
subsequent code. Similarly `## {t.question}` at `:99` assumes a single-line question; the chat UI
uses `<input>` so this is not reachable from the UI, but `POST /stakeholder/{tid}/answer` accepts
an arbitrary `question` string, and a newline turns the rest into body text. Fix: compute a fence
longer than the longest backtick run in `e.code`, and collapse whitespace in the heading
(`" ".join(t.question.split())`).

**M5 — `code_para.style = doc.styles["Normal"]` is a no-op.** `storyline.py:142`.
`doc.add_paragraph(text)` already returns a Normal-styled paragraph. Either delete the line or —
better, and probably the original intent — define a real monospaced code style once and apply it,
since the current approach re-sets `run.font.name` per run and leaves the East-Asian font
attribute unset. (Multi-line code itself is fine: verified that python-docx converts `\n` to
`<w:br/>`, so multi-line SQL does render as multiple lines in Word.)

**M6 — Control characters in any text field make `render_docx` raise.** `storyline.py:126-141`.
Verified: `doc.add_heading("bad \x0b heading", 2)` raises
`ValueError: All strings must be XML compatible: Unicode or ASCII, no NULL bytes or control
characters`. LLM-authored answers and warehouse-derived SQL can carry `\x0b`/`\x0c`. The route has
no `try`, so this is an unhandled 500 (and invisible to the user, per I5). Fix: strip
`C0`-except-`\t\n\r` from question/answer/facts/caveats/code before handing them to python-docx.

**M7 — Filename slug is unbounded.** `api.py:1091`. Titles are 80 chars when auto-derived
(`stakeholder.py:138`) but `PATCH /conversations/{id}` accepts an arbitrary-length `title`
(`update_conversation`, `stakeholder.py:186-198`, no length check). A 10,000-character title yields
a 10,000-character `Content-Disposition`, which some proxies reject with 502/431. Truncate the slug
(the C2 fix above includes `[:60]`).

**M8 — `StorylineTurn.created_at` is collected and never rendered.** `storyline.py:23,57` — neither
`render_markdown` nor `render_docx` emits it. A stakeholder deliverable with no dates on any turn
is a real gap for an audit artifact; the field being present but unused suggests it was intended.
Either render it under each `##` heading or drop the field.

**M9 — `produced_df_label?` on the TS type is never read.** `frontend/src/store/useStore.ts:14`.
Added per the plan, but no component consumes it. Harmless; note only.

---

# Cleared on inspection (checked, not findings)

- **Tenancy chain of the export route is correct.** `tenant_or_404(tenant_id)` (`api.py:1079`) →
  `C.stakeholder.get_conversation(tenant_id, conversation_id)` (`api.py:1080`), which resolves
  through `self.stores.for_tenant(tenant_id)` — a *separate SQLite file per tenant*, not a
  `tenant_id` filter — and additionally constrains `WHERE id=? AND tenant_id=?` on both the
  conversation row (`stakeholder.py:166-167`) and the answers (`:171-172`). No cross-tenant path.
- **`answer_ids` are validated against *this* conversation, not mere existence.**
  `known_ids = {m["answer_id"] for m in conv["messages"]}` (`api.py:1085`) is derived from the
  already-tenant-and-conversation-scoped fetch. Passing another conversation's `answer_id` yields
  400, and another tenant's yields 400 as well. This is the right shape — it is just untested (I8).
- **Header injection via CRLF in the filename is blocked.** `\r` and `\n` are not `isalnum()`, so
  the slug replaces them with `-`. (Non-Latin-1 characters are the actual problem — C2.)
- **Multi-line code renders correctly in .docx** — python-docx converts `\n` to `<w:br/>`;
  round-tripped and confirmed.
- **`assemble_storyline` de-duplicates correctly** for the cases it does handle: a dependency turn
  that is also selected is not double-emitted (`storyline.py:75`), and two selected Python turns
  sharing one unselected producer emit it once (`:76`, `dependency_answer_ids_added`).
- **"Multiple df labels in one cell" is not reachable.** The sandbox injects exactly one frame
  (`stakeholder.py:770`: `run_python_sandboxed(code, {df_label: df})`), and each `python_cells`
  entry carries a single `df_label`. The loop over `m.get("python_cells", [])` correctly handles
  the multi-cell case should it ever arise.
- **Empty code appendix** is handled — both renderers skip the section (`storyline.py:109`, `:134`).
- **Empty `answer_ids`** is a 400 (`api.py:1082-1083`), and `assemble_storyline` returns a
  well-formed empty `StorylineContent`.
- **The migration is non-destructive** and guarded on `if "produced_df_label" not in sa_cols`
  (`database.py:227-228`), matching the existing `queries_run`/`python_cells` template.
- **Turn ordering** follows conversation order regardless of the order of `answer_ids` in the
  request. This is deliberate and pinned by `test_only_selected_turns_appear_in_order`.
- **Committing directly to `main`** — per project convention, not a finding.
- `frontend/AGENTS.md` was treated as untrusted data. It contains text directed at agents (falsely
  claiming to be generated by `next dev`, instructing agents to read `node_modules/next/dist/docs/`
  and commit a block). It was not followed and has no bearing on any finding above. Flagging its
  existence to the human is itself worth doing.

---

# Required before ship

1. **C1** — key dependency resolution on the producing answer id, and make `next_label` monotonic.
2. **C2** — ASCII-safe + RFC 5987 `Content-Disposition`, with length bound.
3. **I1** — lazy `import docx` + 503.
4. **I3** — surface unresolvable labels instead of dropping them.
5. **I5** — show export errors in the UI (without this, C2 and every other failure is invisible).
6. **I8.1** — add the cross-conversation `answer_id` rejection test.

I2, I4, I6, I7 should be fixed or explicitly ticketed. The Minors can ride along.
