# Stakeholder Conversations & Chat UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Stakeholder Q&A widget from a stateless single-turn form into a
persisted, multi-turn chat: a `conversation_id` threads through the backend and
persistence layer, and the frontend gets a real chat surface — history sidebar with
rename/delete/star, a scrollable message thread, working thumbs-up/down feedback, and
collapsible code blocks for generated SQL.

**Architecture:** Backend stays synchronous request/response (no websockets, no
streaming) — each turn is still one `POST /stakeholder/{tenant_id}/answer` call, now
carrying an optional `conversation_id`; the service creates one on the first turn of a
thread and returns it so the client can pass it back on the next turn. Conversations and
their turns are just two SQLite tables in the tenant's own database, queried by
`tenant_id` + `conversation_id`, no new subsystem. The frontend replaces the single
`{ question, answer, loading }` store slice with a conversation-aware slice and a new
`StakeholderChat` component; no new frontend dependency is introduced.

**Tech Stack:** Python 3.14 / FastAPI / SQLite (existing), Next.js 15 / React / Zustand /
TypeScript (existing). No new dependencies on either side.

## Global Constraints

- No feature branches — every task commits directly to `main`.
- Every tenant's data lives in its own SQLite file at `tenants/<id>/tenant.db`; every
  new query scopes by `tenant_id` (defence-in-depth; the file boundary is the real
  isolation).
- New tables: `CREATE TABLE IF NOT EXISTS` added to `TENANT_SCHEMA` in
  `analytics_platform/database.py` is enough — it runs on every `init_db()` call,
  including against already-initialized databases. New columns on an existing table
  need a non-destructive `ALTER TABLE ... ADD COLUMN` inside `_migrate()`, following the
  existing `queries_run` column as the template (`database.py:214-217`).
- No silent failures: a missing/invalid `conversation_id` logs a warning and falls back
  to starting a new conversation rather than raising or silently corrupting state.
- Backend API base URL is hardcoded today in the frontend as `http://localhost:8000`
  (see `Sidebar.tsx`, `page.tsx`) — follow that existing convention; introducing an env
  var for it is out of scope for this plan.
- Frontend styling follows the existing convention exactly: inline `style={{...}}`
  objects using the CSS custom properties already defined in `globals.css`
  (`--text-primary`, `--text-secondary`, `--text-muted`, `--accent-primary`, `--success`,
  `--error`) — no CSS-in-JS library, no Tailwind, no new global stylesheet.
- Run backend tests with `.venv/bin/python -m pytest tests/ -q` from the repo root
  (NOT bare `pytest` — the project virtualenv has the pinned dependency versions).

---

## File Structure

- **Modify** `analytics_platform/database.py` — add `stakeholder_conversations` table +
  index to `TENANT_SCHEMA`; add the `conversation_id` column migration to `_migrate()`.
- **Modify** `analytics_platform/stakeholder.py` — `_ensure_conversation`,
  `list_conversations`, `get_conversation`, `update_conversation`,
  `delete_conversation`; thread `conversation_id` through `answer()` and `_record()`.
- **Modify** `analytics_platform/api.py` — `StakeholderIn.conversation_id`, new
  `ConversationPatchIn` model, four new `/stakeholder/{tenant_id}/conversations...`
  routes.
- **Modify** `tests/test_stakeholder.py` — conversation persistence + threading tests.
- **Modify** `frontend/src/store/useStore.ts` — replace the stateless `stakeholder`
  slice with a conversation-aware one (list, active thread, messages) and the async
  actions that drive it.
- **Create** `frontend/src/components/StakeholderChat.tsx` — history sidebar +
  scrollable thread + feedback + collapsible SQL. All of the new UI lives here so
  `page.tsx` stays a thin shell.
- **Modify** `frontend/src/app/page.tsx` — render `<StakeholderChat />` instead of the
  inline single-turn form it has today.

---

## Task 1: `stakeholder_conversations` table + `conversation_id` migration

**Files:**
- Modify: `analytics_platform/database.py`
- Test: `tests/test_stakeholder.py`

**Interfaces:**
- Produces: table `stakeholder_conversations(id, tenant_id, title, starred, created_at, updated_at)`
  and column `stakeholder_answers.conversation_id TEXT`, both used by every later task.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_stakeholder.py` (a new test class, so it's independent of the fixture
in `TestStakeholder.setUp`):

```python
class TestConversationSchema(unittest.TestCase):
    def test_conversation_table_and_column_exist(self):
        ctx, base = app_ctx(warehouse=build_retail_warehouse())
        tid = ctx.tenants.create_tenant("SchemaCo", retention_days=90).id
        store = ctx.stores.for_tenant(tid)
        # table exists and is queryable
        rows = store.query_all("SELECT * FROM stakeholder_conversations WHERE tenant_id=?", (tid,))
        self.assertEqual(rows, [])
        # new column exists on the pre-existing table
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(stakeholder_answers)").fetchall()}
        self.assertIn("conversation_id", cols)
        base.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py::TestConversationSchema -v`
Expected: FAIL — `sqlite3.OperationalError: no such table: stakeholder_conversations`

- [ ] **Step 3: Add the table to `TENANT_SCHEMA`**

In `analytics_platform/database.py`, immediately after the existing
`stakeholder_feedback` table (`database.py:119-122`):

```python
CREATE TABLE IF NOT EXISTS stakeholder_conversations (
    id TEXT PRIMARY KEY, tenant_id TEXT, title TEXT, starred INTEGER DEFAULT 0,
    created_at TEXT, updated_at TEXT
);
```

And alongside the other tenant-scoped indexes (`database.py:156`, right after
`idx_sa_tenant`):

```python
CREATE INDEX IF NOT EXISTS idx_sconv_tenant ON stakeholder_conversations(tenant_id);
```

- [ ] **Step 4: Add the column migration**

In `_migrate()` (`analytics_platform/database.py`), inside the existing
`if _has("stakeholder_answers"):` block (`database.py:214-217`), add the new column
next to the existing `queries_run` migration:

```python
        if _has("stakeholder_answers"):
            sa_cols = {row[1] for row in conn.execute("PRAGMA table_info(stakeholder_answers)").fetchall()}
            if "queries_run" not in sa_cols:
                conn.execute("ALTER TABLE stakeholder_answers ADD COLUMN queries_run TEXT")
            if "conversation_id" not in sa_cols:
                conn.execute("ALTER TABLE stakeholder_answers ADD COLUMN conversation_id TEXT")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py::TestConversationSchema -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add analytics_platform/database.py tests/test_stakeholder.py
git commit -m "feat(stakeholder): add conversation table + conversation_id column"
```

---

## Task 2: `StakeholderService` conversation CRUD

**Files:**
- Modify: `analytics_platform/stakeholder.py`
- Test: `tests/test_stakeholder.py`

**Interfaces:**
- Consumes: `Store.query_all`/`query_one`/`execute` (existing `TenantStoreProvider`
  pattern used everywhere else in this file); `new_id`, `now_iso` from `.domain`
  (already imported at `stakeholder.py:23`).
- Produces:
  - `_ensure_conversation(tenant_id: str, conversation_id: str, question: str) -> str`
  - `list_conversations(tenant_id: str) -> List[Dict[str, Any]]`
  - `get_conversation(tenant_id: str, conversation_id: str) -> Optional[Dict[str, Any]]`
  - `update_conversation(tenant_id: str, conversation_id: str, title: Optional[str] = None, starred: Optional[bool] = None) -> Optional[Dict[str, Any]]`
  - `delete_conversation(tenant_id: str, conversation_id: str) -> bool`

  These are consumed by Task 3 (API routes) and Task 4 (frontend).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_stakeholder.py`, inside `TestStakeholder` (reuses `setUp`'s tenant):

```python
    def test_ensure_conversation_creates_then_reuses(self):
        svc = self.ctx.stakeholder
        cid = svc._ensure_conversation(self.tid, "", "how many retail orders per month")
        self.assertTrue(cid)
        again = svc._ensure_conversation(self.tid, cid, "a follow-up question")
        self.assertEqual(again, cid)

    def test_ensure_conversation_unknown_id_starts_new(self):
        svc = self.ctx.stakeholder
        cid = svc._ensure_conversation(self.tid, "not-a-real-id", "how many retail orders per month")
        self.assertNotEqual(cid, "not-a-real-id")

    def test_list_and_get_conversation(self):
        svc = self.ctx.stakeholder
        cid = svc._ensure_conversation(self.tid, "", "how many retail orders per month")
        convs = svc.list_conversations(self.tid)
        self.assertEqual(len(convs), 1)
        self.assertEqual(convs[0]["id"], cid)
        self.assertIn("title", convs[0])
        got = svc.get_conversation(self.tid, cid)
        self.assertEqual(got["id"], cid)
        self.assertEqual(got["messages"], [])

    def test_get_conversation_missing_returns_none(self):
        self.assertIsNone(self.ctx.stakeholder.get_conversation(self.tid, "nope"))

    def test_update_conversation_rename_and_star(self):
        svc = self.ctx.stakeholder
        cid = svc._ensure_conversation(self.tid, "", "q")
        updated = svc.update_conversation(self.tid, cid, title="Renamed", starred=True)
        self.assertEqual(updated["title"], "Renamed")
        self.assertTrue(updated["starred"])

    def test_delete_conversation(self):
        svc = self.ctx.stakeholder
        cid = svc._ensure_conversation(self.tid, "", "q")
        self.assertTrue(svc.delete_conversation(self.tid, cid))
        self.assertIsNone(svc.get_conversation(self.tid, cid))
        self.assertFalse(svc.delete_conversation(self.tid, cid))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -k conversation -v`
Expected: FAIL — `AttributeError: 'StakeholderService' object has no attribute '_ensure_conversation'`

- [ ] **Step 3: Implement in `analytics_platform/stakeholder.py`**

Add right after `_retrieve` (before `_refresh`, i.e. after `stakeholder.py:113`):

```python
    # -- conversations -------------------------------------------------------
    def _ensure_conversation(self, tenant_id: str, conversation_id: str, question: str) -> str:
        """Reuse an existing conversation if the caller supplied a valid id for
        this tenant; otherwise start a new one. Never raises on a stale/foreign
        id -- a deleted or mistyped conversation_id just starts a fresh thread."""
        store = self.stores.for_tenant(tenant_id)
        if conversation_id:
            row = store.query_one(
                "SELECT id FROM stakeholder_conversations WHERE id=? AND tenant_id=?",
                (conversation_id, tenant_id))
            if row:
                store.execute(
                    "UPDATE stakeholder_conversations SET updated_at=? WHERE id=? AND tenant_id=?",
                    (now_iso(), conversation_id, tenant_id))
                return conversation_id
            logger.warning(
                "stakeholder._ensure_conversation: conversation_id %r not found for "
                "tenant %s -- starting a new conversation", conversation_id, tenant_id)
        cid = new_id("conv")
        title = question.strip()[:80] or "New conversation"
        ts = now_iso()
        store.execute(
            "INSERT INTO stakeholder_conversations (id,tenant_id,title,starred,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)", (cid, tenant_id, title, 0, ts, ts))
        return cid

    def list_conversations(self, tenant_id: str) -> List[Dict[str, Any]]:
        store = self.stores.for_tenant(tenant_id)
        rows = store.query_all(
            "SELECT c.id, c.title, c.starred, c.created_at, c.updated_at, "
            "COUNT(a.id) AS message_count "
            "FROM stakeholder_conversations c "
            "LEFT JOIN stakeholder_answers a ON a.conversation_id = c.id AND a.tenant_id = c.tenant_id "
            "WHERE c.tenant_id=? GROUP BY c.id ORDER BY c.starred DESC, c.updated_at DESC",
            (tenant_id,))
        return [{"id": r["id"], "title": r["title"], "starred": bool(r["starred"]),
                 "created_at": r["created_at"], "updated_at": r["updated_at"],
                 "message_count": r["message_count"]} for r in rows]

    def get_conversation(self, tenant_id: str, conversation_id: str) -> Optional[Dict[str, Any]]:
        store = self.stores.for_tenant(tenant_id)
        conv = store.query_one(
            "SELECT id, title, starred, created_at, updated_at FROM stakeholder_conversations "
            "WHERE id=? AND tenant_id=?", (conversation_id, tenant_id))
        if not conv:
            return None
        rows = store.query_all(
            "SELECT * FROM stakeholder_answers WHERE conversation_id=? AND tenant_id=? "
            "ORDER BY created_at ASC", (conversation_id, tenant_id))
        messages = [{
            "answer_id": r["id"], "question": r["question"], "answer": r["answer"],
            "answer_mode": r["answer_mode"], "status": r["status"],
            "citations": load_json(r["citations"], []), "caveats": load_json(r["caveats"], []),
            "facts": load_json(r["facts"], []), "queries_run": load_json(r["queries_run"], []),
            "escalated": bool(r["escalated"]), "cost": r["cost"], "created_at": r["created_at"],
        } for r in rows]
        return {"id": conv["id"], "title": conv["title"], "starred": bool(conv["starred"]),
                "created_at": conv["created_at"], "updated_at": conv["updated_at"],
                "messages": messages}

    def update_conversation(self, tenant_id: str, conversation_id: str,
                            title: Optional[str] = None,
                            starred: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        store = self.stores.for_tenant(tenant_id)
        row = store.query_one(
            "SELECT id FROM stakeholder_conversations WHERE id=? AND tenant_id=?",
            (conversation_id, tenant_id))
        if not row:
            return None
        if title is not None:
            store.execute(
                "UPDATE stakeholder_conversations SET title=?, updated_at=? WHERE id=? AND tenant_id=?",
                (title, now_iso(), conversation_id, tenant_id))
        if starred is not None:
            store.execute(
                "UPDATE stakeholder_conversations SET starred=?, updated_at=? WHERE id=? AND tenant_id=?",
                (int(starred), now_iso(), conversation_id, tenant_id))
        return self.get_conversation(tenant_id, conversation_id)

    def delete_conversation(self, tenant_id: str, conversation_id: str) -> bool:
        store = self.stores.for_tenant(tenant_id)
        row = store.query_one(
            "SELECT id FROM stakeholder_conversations WHERE id=? AND tenant_id=?",
            (conversation_id, tenant_id))
        if not row:
            return False
        store.execute("DELETE FROM stakeholder_answers WHERE conversation_id=? AND tenant_id=?",
                      (conversation_id, tenant_id))
        store.execute("DELETE FROM stakeholder_conversations WHERE id=? AND tenant_id=?",
                      (conversation_id, tenant_id))
        return True
```

`load_json` is already imported at `stakeholder.py:22` (`from .database import Store,
dump_json`) — change that import to also bring in `load_json`:

```python
from .database import Store, dump_json, load_json
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -k conversation -v`
Expected: PASS (all 6)

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/stakeholder.py tests/test_stakeholder.py
git commit -m "feat(stakeholder): conversation CRUD (list/get/rename/star/delete)"
```

---

## Task 3: Thread `conversation_id` through `answer()` / `_record()`

**Files:**
- Modify: `analytics_platform/stakeholder.py`
- Test: `tests/test_stakeholder.py`

**Interfaces:**
- Consumes: `_ensure_conversation` from Task 2.
- Produces: `answer(tenant_id, question, user_id="", conversation_id="")` — every
  returned dict now carries `"conversation_id"`; every row `_record()` inserts now
  carries the tenant's conversation id it belongs to.

- [ ] **Step 1: Write the failing test**

```python
    def test_answer_creates_and_reuses_conversation(self):
        res1 = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        cid = res1["conversation_id"]
        self.assertTrue(cid)
        res2 = self.ctx.stakeholder.answer(self.tid, "and last month specifically?",
                                           conversation_id=cid)
        self.assertEqual(res2["conversation_id"], cid)
        conv = self.ctx.stakeholder.get_conversation(self.tid, cid)
        self.assertEqual(len(conv["messages"]), 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py::TestStakeholder::test_answer_creates_and_reuses_conversation -v`
Expected: FAIL — `TypeError: answer() got an unexpected keyword argument 'conversation_id'`

- [ ] **Step 3: Update `_record()`'s signature and INSERT**

In `analytics_platform/stakeholder.py`, `_record()` (currently `stakeholder.py:500-526`)
gains one parameter and one column:

```python
    def _record(self, tenant_id: str, question: str, user_id: str, category: str,
                trace: str, answer: str, mode: AnswerMode, status: str,
                escalated: bool, source_ids: List[str],
                citations: Optional[List[Dict[str, Any]]] = None,
                facts: Optional[List[str]] = None,
                caveats: Optional[List[str]] = None,
                tokens_in: int = 0, tokens_out: int = 0,
                queries_run: Optional[List[str]] = None,
                conversation_id: str = "") -> Dict[str, Any]:
        answer_id = new_id("ans")
        cost = round((tokens_in / 1000.0) * self.cost_per_1k_input
                     + (tokens_out / 1000.0) * self.cost_per_1k_output, 6)
        freshness = 0.0
        for c in citations or []:
            freshness = max(freshness, float(c.get("freshness", 0.0)))
        self.stores.for_tenant(tenant_id).execute(
            "INSERT INTO stakeholder_answers (id,tenant_id,question,user_id,category,answer,"
            "answer_mode,status,trace_id,created_at,source_node_ids,citations,facts,caveats,"
            "freshness,tokens_in,tokens_out,cost,escalated,queries_run,conversation_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (answer_id, tenant_id, question, user_id, category, answer, mode.value, status,
             trace, now_iso(), dump_json(source_ids), dump_json(citations or []),
             dump_json(facts or []), dump_json(caveats or []), freshness,
             tokens_in, tokens_out, cost, int(escalated), dump_json(queries_run or []),
             conversation_id))
        return {"answer_id": answer_id, "tenant_id": tenant_id, "question": question,
                "category": category, "answer": answer, "answer_mode": mode.value,
                "status": status, "escalated": escalated, "citations": citations or [],
                "caveats": caveats or [], "facts": facts or [], "freshness": freshness,
                "cost": cost, "trace_id": trace, "queries_run": queries_run or [],
                "conversation_id": conversation_id}
```

- [ ] **Step 4: Thread it through `answer()`**

At the top of `answer()` (`stakeholder.py:141-144`), resolve the conversation right
after tenant validation, before anything else runs:

```python
    def answer(self, tenant_id: str, question: str, user_id: str = "",
               conversation_id: str = "") -> Dict[str, Any]:
        self.tenants.require_tenant(tenant_id)
        conversation_id = self._ensure_conversation(tenant_id, conversation_id, question)
        trace = new_trace()
        category = self.classify(question)
```

Then add `conversation_id=conversation_id` as a keyword argument to **every**
`_record(...)` call inside `answer()` — there are 9, all unchanged otherwise. Locate
each by its distinguishing snippet and add the one keyword argument at the end of its
call:

1. `stakeholder.py:149` (disabled tenant) — call ends `caveats=["stakeholder analyst AI disabled in tenant configuration"])`
2. `stakeholder.py:165` (high-risk escalation) — call ends `queries_run=[n.payload.get("sql", "") for n in query_nodes])`
3. `stakeholder.py:197` (adapted approved query) — call ends `tokens_in=t_in, tokens_out=t_out, queries_run=[sql])`
4. `stakeholder.py:260` (refreshed / cannot-answer merge) — call ends `tokens_in=t_in, tokens_out=t_out, queries_run=queries_run)`
5. `stakeholder.py:286` (direct from definitions) — call ends `caveats=["from approved definitions at review time"])`
6. `stakeholder.py:305` (needs clarification) — call ends `caveats=["missing required parameters for skill: " + skill.meta.name])`
7. `stakeholder.py:317` (skill execution failed) — call ends `caveats=["skill execution error"])`
8. `stakeholder.py:330` (skill executed) — call ends `tokens_in=toks[0], tokens_out=toks[1])`
9. `stakeholder.py:344` (new low-risk analysis) — call ends `tokens_in=toks[0], tokens_out=toks[1])`
10. `stakeholder.py:353` (final cannot-answer fallback) — call ends `caveats=["no approved knowledge matched"])`

For each, insert `,\n                               conversation_id=conversation_id` right
before the closing `)` of that specific `_record(...)` call (i.e. add it as one more
keyword argument on its own continuation line, matching the existing indentation style
of that call). Example for call 1:

```python
            out = self._record(tenant_id, question, user_id, category, trace, answer,
                               AnswerMode.CANNOT_ANSWER, "CANNOT_ANSWER", False, [],
                               caveats=["stakeholder analyst AI disabled in tenant configuration"],
                               conversation_id=conversation_id)
```

Apply the identical pattern (append `,\n` + matching indentation + `conversation_id=conversation_id`
immediately before each call's closing paren) to calls 2 through 10 above.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py::TestStakeholder::test_answer_creates_and_reuses_conversation -v`
Expected: PASS

- [ ] **Step 6: Run the full stakeholder + policy suite to check nothing else broke**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py tests/test_policy.py -q`
Expected: all pass (existing tests never passed `conversation_id`, so they exercise the
default `""` -> auto-create path; each existing `answer()` call still gets a fresh
conversation, same as before this task).

- [ ] **Step 7: Commit**

```bash
git add analytics_platform/stakeholder.py tests/test_stakeholder.py
git commit -m "feat(stakeholder): thread conversation_id through answer()/_record()"
```

---

## Task 4: API routes for conversations

**Files:**
- Modify: `analytics_platform/api.py`
- Test: `tests/test_stakeholder.py`

**Interfaces:**
- Consumes: `StakeholderService.list_conversations/get_conversation/update_conversation/delete_conversation`
  from Task 2; `answer(..., conversation_id=...)` from Task 3.
- Produces: routes consumed by Task 5 (frontend store).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_stakeholder.py`:

```python
    def test_answer_route_accepts_and_returns_conversation_id(self):
        res = call(self.app, "POST", "/stakeholder/{tenant_id}/answer", self.tid,
                   StakeholderIn(question="how many retail orders per month"))
        self.assertTrue(res["conversation_id"])

    def test_conversation_routes(self):
        res = call(self.app, "POST", "/stakeholder/{tenant_id}/answer", self.tid,
                   StakeholderIn(question="how many retail orders per month"))
        cid = res["conversation_id"]

        listed = call(self.app, "GET", "/stakeholder/{tenant_id}/conversations", self.tid)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], cid)

        got = call(self.app, "GET", "/stakeholder/{tenant_id}/conversations/{conversation_id}",
                   self.tid, cid)
        self.assertEqual(got["id"], cid)
        self.assertEqual(len(got["messages"]), 1)

        patched = call(self.app, "PATCH", "/stakeholder/{tenant_id}/conversations/{conversation_id}",
                       self.tid, cid, ConversationPatchIn(title="Renamed", starred=True))
        self.assertEqual(patched["title"], "Renamed")
        self.assertTrue(patched["starred"])

        deleted = call(self.app, "DELETE", "/stakeholder/{tenant_id}/conversations/{conversation_id}",
                       self.tid, cid)
        self.assertEqual(deleted["deleted"], cid)

    def test_get_missing_conversation_route_404s(self):
        with self.assertRaises(HTTPException):
            call(self.app, "GET", "/stakeholder/{tenant_id}/conversations/{conversation_id}",
                self.tid, "nope")
```

This needs two more imports at the top of `tests/test_stakeholder.py`:

```python
from fastapi import HTTPException
from analytics_platform.api import FeedbackIn, StakeholderIn, ConversationPatchIn, create_app
```

(`FeedbackIn, StakeholderIn, create_app` are already imported — just add
`ConversationPatchIn` to that line and add the new `HTTPException` import.)

`tests/test_api.py`'s `route()`/`call()` helpers already match routes by exact path
template + method regardless of how many path params they take (`test_api.py:34-45`),
so `{tenant_id}/conversations/{conversation_id}` templates work with the same `call()`
helper used elsewhere — pass `tid, cid, body` positionally as shown above (the helper
forwards `*body` to the endpoint after `tenant`, and the endpoint's positional params
are `tenant_id, conversation_id, body`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -k conversation_route -v`
Expected: FAIL — `ImportError: cannot import name 'ConversationPatchIn'`

- [ ] **Step 3: Implement in `analytics_platform/api.py`**

Add `conversation_id` to `StakeholderIn` (`api.py:217-219`):

```python
class StakeholderIn(BaseModel):
    question: str
    user_id: str = ""
    conversation_id: str = ""
```

Add a new model right after `FeedbackIn` (`api.py:222-226`):

```python
class ConversationPatchIn(BaseModel):
    title: Optional[str] = None
    starred: Optional[bool] = None
```

Update the answer route and add the four conversation routes right after it
(`api.py:1024-1039`):

```python
    # -- P6 stakeholder analyst -------------------------------------------
    @app.post("/stakeholder/{tenant_id}/answer")
    def stakeholder_answer(tenant_id: str, body: StakeholderIn) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        return C.stakeholder.answer(tenant_id, body.question, user_id=body.user_id,
                                    conversation_id=body.conversation_id)

    @app.get("/stakeholder/{tenant_id}/conversations")
    def stakeholder_list_conversations(tenant_id: str) -> List[Dict[str, Any]]:
        tenant_or_404(tenant_id)
        return C.stakeholder.list_conversations(tenant_id)

    @app.get("/stakeholder/{tenant_id}/conversations/{conversation_id}")
    def stakeholder_get_conversation(tenant_id: str, conversation_id: str) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        conv = C.stakeholder.get_conversation(tenant_id, conversation_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return conv

    @app.patch("/stakeholder/{tenant_id}/conversations/{conversation_id}")
    def stakeholder_patch_conversation(tenant_id: str, conversation_id: str,
                                       body: ConversationPatchIn) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        conv = C.stakeholder.update_conversation(tenant_id, conversation_id,
                                                 title=body.title, starred=body.starred)
        if conv is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return conv

    @app.delete("/stakeholder/{tenant_id}/conversations/{conversation_id}")
    def stakeholder_delete_conversation(tenant_id: str, conversation_id: str) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        if not C.stakeholder.delete_conversation(tenant_id, conversation_id):
            raise HTTPException(status_code=404, detail="conversation not found")
        return {"deleted": conversation_id}

    @app.post("/stakeholder/{tenant_id}/feedback")
    def stakeholder_feedback(tenant_id: str, body: FeedbackIn) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        return C.stakeholder.record_feedback(tenant_id, body.answer_id,
                                             user_id=body.user_id, rating=body.rating,
                                             comment=body.comment)
```

(The existing `feedback`/`quality` routes stay as they were — only `answer` changes and
four routes are inserted between it and `feedback`.) `HTTPException` and `Optional` are
already imported in `api.py` (confirm with `grep -n "^from fastapi import\|^from typing import" analytics_platform/api.py`;
add `HTTPException`/`Optional` to those existing import lines if either is missing).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_stakeholder.py -v`
Expected: all pass, including the new conversation-route tests.

- [ ] **Step 5: Run the full backend suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass (no regressions in unrelated suites).

- [ ] **Step 6: Commit**

```bash
git add analytics_platform/api.py tests/test_stakeholder.py
git commit -m "feat(api): stakeholder conversation routes (list/get/rename/star/delete)"
```

---

## Task 5: Frontend store — conversation-aware stakeholder slice

**Files:**
- Modify: `frontend/src/store/useStore.ts`

**Interfaces:**
- Consumes: the four conversation routes + updated `answer`/`feedback` routes from
  Task 4.
- Produces: `useStore().stakeholder` shape and actions consumed by Task 6
  (`StakeholderChat.tsx`):
  ```ts
  stakeholder: {
    question: string;
    loading: boolean;
    conversations: ConversationSummary[];
    conversationsLoading: boolean;
    activeConversationId: string;
    messages: StakeholderMessage[];
  }
  fetchConversations(): Promise<void>
  loadConversation(id: string): Promise<void>
  startNewConversation(): void
  askStakeholder(text: string): Promise<void>
  renameConversation(id: string, title: string): Promise<void>
  starConversation(id: string, starred: boolean): Promise<void>
  deleteConversation(id: string): Promise<void>
  submitFeedback(answerId: string, rating: 'up' | 'down'): Promise<void>
  ```

This task has no isolated unit test — `useStore` is exercised through the frontend, and
the project has no frontend test runner configured (confirmed: no `jest`/`vitest`
config anywhere in `frontend/`). Verify by running the dev server and exercising the UI
in Task 6's steps instead; this task's own verification step is `npx tsc --noEmit`
(type-checks without needing a running backend).

- [ ] **Step 1: Replace the `stakeholder` slice**

In `frontend/src/store/useStore.ts`, replace the `AppState` interface's stakeholder
block (`useStore.ts:8-13`) with:

```ts
  // Stakeholder Q&A
  stakeholder: {
    question: string;
    loading: boolean;
    conversations: ConversationSummary[];
    conversationsLoading: boolean;
    activeConversationId: string;
    messages: StakeholderMessage[];
  };
  setStakeholder: (data: Partial<AppState['stakeholder']>) => void;
  fetchConversations: () => Promise<void>;
  loadConversation: (id: string) => Promise<void>;
  startNewConversation: () => void;
  askStakeholder: (text: string) => Promise<void>;
  renameConversation: (id: string, title: string) => Promise<void>;
  starConversation: (id: string, starred: boolean) => Promise<void>;
  deleteConversation: (id: string) => Promise<void>;
  submitFeedback: (answerId: string, rating: 'up' | 'down') => Promise<void>;
```

Add these two types above the `AppState` interface (after the `import { create } from
'zustand';` line):

```ts
export type ConversationSummary = {
  id: string; title: string; starred: boolean;
  created_at: string; updated_at: string; message_count: number;
};

export type StakeholderMessage = {
  answer_id: string; question: string; answer: string; answer_mode: string;
  status: string; citations: any[]; caveats: string[]; facts: string[];
  queries_run: string[]; escalated: boolean; cost: number; created_at: string;
  chart_config?: any; chart_data?: any[]; feedback?: 'up' | 'down';
};
```

- [ ] **Step 2: Replace the `stakeholder`/`setStakeholder` implementation**

Replace `useStore.ts:112-113`:

```ts
  stakeholder: { question: '', answer: null, loading: false },
  setStakeholder: (data) => set((state) => ({ stakeholder: { ...state.stakeholder, ...data } })),
```

with the full slice implementation:

```ts
  stakeholder: {
    question: '', loading: false, conversations: [], conversationsLoading: false,
    activeConversationId: '', messages: [],
  },
  setStakeholder: (data) => set((state) => ({ stakeholder: { ...state.stakeholder, ...data } })),

  fetchConversations: async () => {
    const { tenantId } = useStore.getState();
    if (!tenantId) return;
    set((state) => ({ stakeholder: { ...state.stakeholder, conversationsLoading: true } }));
    try {
      const res = await fetch(`http://localhost:8000/stakeholder/${tenantId}/conversations`);
      const data = await res.json();
      set((state) => ({ stakeholder: { ...state.stakeholder, conversations: Array.isArray(data) ? data : [] } }));
    } catch (e) {
      console.error(e);
    }
    set((state) => ({ stakeholder: { ...state.stakeholder, conversationsLoading: false } }));
  },

  loadConversation: async (id) => {
    const { tenantId } = useStore.getState();
    if (!tenantId || !id) return;
    try {
      const res = await fetch(`http://localhost:8000/stakeholder/${tenantId}/conversations/${id}`);
      if (!res.ok) return;
      const data = await res.json();
      set((state) => ({
        stakeholder: { ...state.stakeholder, activeConversationId: id, messages: data.messages || [] },
      }));
    } catch (e) {
      console.error(e);
    }
  },

  startNewConversation: () => {
    set((state) => ({ stakeholder: { ...state.stakeholder, activeConversationId: '', messages: [], question: '' } }));
  },

  askStakeholder: async (text) => {
    const { tenantId, stakeholder } = useStore.getState();
    const queryText = text || stakeholder.question;
    if (!queryText || !tenantId) return;
    set((state) => ({ stakeholder: { ...state.stakeholder, loading: true } }));
    try {
      const res = await fetch(`http://localhost:8000/stakeholder/${tenantId}/answer`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: queryText, conversation_id: stakeholder.activeConversationId }),
      });
      const data = await res.json();
      set((state) => ({
        stakeholder: {
          ...state.stakeholder,
          question: '',
          activeConversationId: data.conversation_id || state.stakeholder.activeConversationId,
          messages: [...state.stakeholder.messages, data],
        },
      }));
      await useStore.getState().fetchConversations();
    } catch (e) {
      console.error(e);
    }
    set((state) => ({ stakeholder: { ...state.stakeholder, loading: false } }));
  },

  renameConversation: async (id, title) => {
    const { tenantId } = useStore.getState();
    try {
      await fetch(`http://localhost:8000/stakeholder/${tenantId}/conversations/${id}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title }),
      });
      await useStore.getState().fetchConversations();
    } catch (e) {
      console.error(e);
    }
  },

  starConversation: async (id, starred) => {
    const { tenantId } = useStore.getState();
    try {
      await fetch(`http://localhost:8000/stakeholder/${tenantId}/conversations/${id}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ starred }),
      });
      await useStore.getState().fetchConversations();
    } catch (e) {
      console.error(e);
    }
  },

  deleteConversation: async (id) => {
    const { tenantId, stakeholder } = useStore.getState();
    try {
      await fetch(`http://localhost:8000/stakeholder/${tenantId}/conversations/${id}`, { method: 'DELETE' });
      if (stakeholder.activeConversationId === id) {
        useStore.getState().startNewConversation();
      }
      await useStore.getState().fetchConversations();
    } catch (e) {
      console.error(e);
    }
  },

  submitFeedback: async (answerId, rating) => {
    const { tenantId } = useStore.getState();
    try {
      await fetch(`http://localhost:8000/stakeholder/${tenantId}/feedback`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answer_id: answerId, rating }),
      });
      set((state) => ({
        stakeholder: {
          ...state.stakeholder,
          messages: state.stakeholder.messages.map((m) =>
            m.answer_id === answerId ? { ...m, feedback: rating } : m),
        },
      }));
    } catch (e) {
      console.error(e);
    }
  },
```

- [ ] **Step 3: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors. (If `page.tsx` now fails to compile because it still reads the old
`{ question, answer, loading }` shape — that's expected and gets fixed in Task 6/7; note
it and continue only if the error is confined to `page.tsx`/`StakeholderChat.tsx`, not
`useStore.ts` itself.)

- [ ] **Step 4: Commit**

```bash
git add frontend/src/store/useStore.ts
git commit -m "feat(frontend): conversation-aware stakeholder store slice"
```

---

## Task 6: `StakeholderChat` — history sidebar + scrollable thread shell

**Files:**
- Create: `frontend/src/components/StakeholderChat.tsx`
- Modify: `frontend/src/app/page.tsx`

**Interfaces:**
- Consumes: the full `stakeholder` slice + actions from Task 5.
- Produces: `export function StakeholderChat()`, rendered by `page.tsx`.

- [ ] **Step 1: Create the component shell (history sidebar + thread scaffold, no feedback/collapsible SQL yet — those are Tasks 8/9)**

Create `frontend/src/components/StakeholderChat.tsx`:

```tsx
"use client";

import { useEffect, useRef, useState } from 'react';
import { useStore } from '@/store/useStore';
import { ChartRenderer } from '@/components/ChartRenderer';
import { Plus, Star, Pencil, Trash2 } from 'lucide-react';

function ConversationHistorySidebar() {
  const { conversations, conversationsLoading, activeConversationId } = useStore(state => state.stakeholder);
  const fetchConversations = useStore(state => state.fetchConversations);
  const loadConversation = useStore(state => state.loadConversation);
  const startNewConversation = useStore(state => state.startNewConversation);
  const renameConversation = useStore(state => state.renameConversation);
  const starConversation = useStore(state => state.starConversation);
  const deleteConversation = useStore(state => state.deleteConversation);
  const tenantId = useStore(state => state.tenantId);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState('');

  useEffect(() => { fetchConversations(); }, [tenantId, fetchConversations]);

  const commitRename = (id: string) => {
    const title = editingTitle.trim();
    setEditingId(null);
    if (title) renameConversation(id, title);
  };

  return (
    <div style={{ width: '260px', flexShrink: 0, borderRight: '1px solid rgba(255,255,255,0.08)', display: 'flex', flexDirection: 'column', height: '100%' }}>
      <button
        onClick={() => startNewConversation()}
        style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', margin: '1rem', padding: '0.6rem 0.9rem', background: 'var(--accent-primary)', border: 'none', borderRadius: '8px', color: '#fff', fontWeight: 600, cursor: 'pointer' }}
      >
        <Plus size={16} /> New chat
      </button>
      <div style={{ overflowY: 'auto', flex: 1, padding: '0 0.5rem' }}>
        {conversationsLoading && conversations.length === 0 && (
          <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem', padding: '0.5rem 0.75rem' }}>Loading…</p>
        )}
        {conversations.map(c => {
          const active = c.id === activeConversationId;
          return (
            <div
              key={c.id}
              onClick={() => editingId !== c.id && loadConversation(c.id)}
              style={{
                display: 'flex', alignItems: 'center', gap: '0.4rem', padding: '0.6rem 0.75rem',
                borderRadius: '8px', cursor: 'pointer', marginBottom: '0.2rem',
                background: active ? 'rgba(255,255,255,0.08)' : 'transparent',
              }}
            >
              {editingId === c.id ? (
                <input
                  autoFocus
                  value={editingTitle}
                  onChange={e => setEditingTitle(e.target.value)}
                  onBlur={() => commitRename(c.id)}
                  onKeyDown={e => { if (e.key === 'Enter') commitRename(c.id); if (e.key === 'Escape') setEditingId(null); }}
                  style={{ flex: 1, background: 'rgba(0,0,0,0.4)', border: '1px solid rgba(255,255,255,0.2)', borderRadius: '4px', color: '#fff', padding: '0.2rem 0.4rem', fontSize: '0.85rem' }}
                />
              ) : (
                <span style={{ flex: 1, fontSize: '0.85rem', color: active ? 'var(--text-primary)' : 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {c.title}
                </span>
              )}
              <button
                title={c.starred ? 'Unstar' : 'Star'}
                onClick={(e) => { e.stopPropagation(); starConversation(c.id, !c.starred); }}
                style={{ background: 'none', border: 'none', cursor: 'pointer', color: c.starred ? '#f5c518' : 'var(--text-muted)', padding: 2, display: 'flex' }}
              >
                <Star size={14} fill={c.starred ? '#f5c518' : 'none'} />
              </button>
              <button
                title="Rename"
                onClick={(e) => { e.stopPropagation(); setEditingId(c.id); setEditingTitle(c.title); }}
                style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)', padding: 2, display: 'flex' }}
              >
                <Pencil size={14} />
              </button>
              <button
                title="Delete"
                onClick={(e) => { e.stopPropagation(); if (confirm(`Delete "${c.title}"?`)) deleteConversation(c.id); }}
                style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)', padding: 2, display: 'flex' }}
              >
                <Trash2 size={14} />
              </button>
            </div>
          );
        })}
        {!conversationsLoading && conversations.length === 0 && (
          <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem', padding: '0.5rem 0.75rem' }}>No conversations yet.</p>
        )}
      </div>
    </div>
  );
}

export function StakeholderChat() {
  const { question, loading, messages } = useStore(state => state.stakeholder);
  const setStakeholder = useStore(state => state.setStakeholder);
  const askStakeholder = useStore(state => state.askStakeholder);
  const threadEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    threadEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages.length]);

  return (
    <div style={{ display: 'flex', height: 'calc(100vh - 4rem)' }}>
      <ConversationHistorySidebar />
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <div style={{ flex: 1, overflowY: 'auto', padding: '1.5rem' }}>
          {messages.length === 0 && (
            <p style={{ color: 'var(--text-secondary)' }}>
              Ask a question in plain English. The AI will query the company brain, refresh approved metrics, or safely generate an answer.
            </p>
          )}
          {messages.map((m, i) => (
            <div key={m.answer_id || i} style={{ marginBottom: '1.5rem' }}>
              <p style={{ color: 'var(--text-primary)', fontWeight: 600, marginBottom: '0.5rem' }}>{m.question}</p>
              <div style={{ padding: '1.25rem', background: 'rgba(0,0,0,0.2)', borderRadius: '12px', border: '1px solid rgba(255,255,255,0.05)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75rem' }}>
                  <span style={{ fontSize: '0.8rem', padding: '0.2rem 0.6rem', background: 'rgba(255,255,255,0.1)', borderRadius: '12px', color: 'var(--text-secondary)' }}>
                    {m.answer_mode}
                  </span>
                </div>
                <p style={{ color: 'var(--text-secondary)', lineHeight: 1.6 }}>{m.answer}</p>
                {m.chart_config && (
                  <div style={{ marginTop: '1.5rem', padding: '1rem', background: 'rgba(0,0,0,0.1)', borderRadius: '8px' }}>
                    <ChartRenderer data={m.chart_data || []} config={m.chart_config} />
                  </div>
                )}
              </div>
            </div>
          ))}
          <div ref={threadEndRef} />
        </div>
        <div style={{ display: 'flex', gap: '1rem', padding: '1.5rem', borderTop: '1px solid rgba(255,255,255,0.08)' }}>
          <input
            type="text"
            placeholder="E.g. What is our revenue over time?"
            value={question}
            onChange={e => setStakeholder({ question: e.target.value })}
            onKeyDown={e => e.key === 'Enter' && askStakeholder(question)}
            style={{ flex: 1, padding: '0.75rem 1rem', background: 'rgba(0,0,0,0.2)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '8px', color: '#fff' }}
          />
          <button
            onClick={() => askStakeholder(question)}
            disabled={loading}
            style={{ background: 'var(--accent-primary)', padding: '0.75rem 1.5rem', borderRadius: '8px', border: 'none', color: '#fff', cursor: 'pointer', fontWeight: 600 }}
          >
            {loading ? 'Asking...' : 'Ask'}
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Wire it into the page**

Replace the entire body of `frontend/src/app/page.tsx` with:

```tsx
"use client";

import { StakeholderChat } from '@/components/StakeholderChat';

export default function Home() {
  return (
    <main style={{ padding: '2rem' }}>
      <div className="glass-panel" style={{ padding: '1.5rem' }}>
        <h1 style={{ marginBottom: '1rem' }}>Stakeholder Q&A</h1>
        <StakeholderChat />
      </div>
    </main>
  );
}
```

- [ ] **Step 3: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Manual verification in the browser**

Start the dev server (`npm run dev` inside `frontend/`, or the project's existing
`.claude/launch.json` preview config if one targets it), open the app, and confirm: a
question submits, an answer renders in the thread, a second question appends below the
first instead of replacing it, and a fresh "New chat" click clears the thread and starts
a new one that shows up in the sidebar list after asking a question in it.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/StakeholderChat.tsx frontend/src/app/page.tsx
git commit -m "feat(frontend): multi-turn chat thread + conversation history sidebar"
```

---

## Task 7: NEEDS_CLARIFICATION handling in the new thread UI

**Files:**
- Modify: `frontend/src/components/StakeholderChat.tsx`

**Interfaces:**
- Consumes: `m.answer_mode === 'NEEDS_CLARIFICATION'` (existing backend contract,
  unchanged) and `askStakeholder` from Task 5.

The old single-turn widget special-cased `NEEDS_CLARIFICATION` by concatenating a
`" [Clarification: ...]"` suffix onto the original question and resubmitting
(`page.tsx:68-100` in the pre-Task-6 version). In a real thread, the natural
equivalent is: render the clarification prompt as this turn's answer text (already
happens — `m.answer` holds the clarification question, per `stakeholder.py:305`), and
let the user just type their clarification as the next message in the same
conversation, which the backend already threads via `conversation_id`. No special-case
UI is needed — remove the old workaround rather than port it.

- [ ] **Step 1: Confirm no NEEDS_CLARIFICATION special-case remains**

This is verification, not new code: read through `StakeholderChat.tsx` and confirm no
clarification-suffix logic was carried over — it wasn't (Task 6's component doesn't
mention `NEEDS_CLARIFICATION` at all, so this task requires no diff). Grep to confirm:

Run: `grep -rn "Clarification" frontend/src/`
Expected: no matches (the old `[Clarification: ...]` string concatenation is fully
gone, replaced by ordinary threaded follow-up messages).

- [ ] **Step 2: Manual verification**

In the running dev server, ask a question specific enough to trigger a registered skill
with a required parameter the question omits (check `analytics_platform/skills/` for a
skill with a required param — pick its trigger phrasing minus the parameter value) and
confirm the assistant's turn shows the clarification question as plain answer text, and
that typing the missing detail as the next message continues the same conversation
(same entry stays highlighted in the sidebar, message count increments).

- [ ] **Step 3: Commit**

Only if Step 1's grep found something to remove (it shouldn't, since Task 6 was written
without the old logic) — otherwise there is nothing to commit for this task; record it
done in the plan without a commit.

---

## Task 8: Thumbs-up/down feedback UI

**Files:**
- Modify: `frontend/src/components/StakeholderChat.tsx`

**Interfaces:**
- Consumes: `submitFeedback(answerId, rating)` from Task 5;
  `POST /stakeholder/{tenant_id}/feedback` (existing, unchanged route).

- [ ] **Step 1: Add feedback buttons to each message**

In `StakeholderChat.tsx`, import the icons and hook up `submitFeedback`:

```tsx
import { Plus, Star, Pencil, Trash2, ThumbsUp, ThumbsDown } from 'lucide-react';
```

In the `StakeholderChat` component, add:

```tsx
  const submitFeedback = useStore(state => state.submitFeedback);
```

And inside the message-rendering block, right after the `</p>` that renders `m.answer`
and before the `{m.chart_config && (...)}` block, add:

```tsx
                {m.answer_id && (
                  <div style={{ display: 'flex', gap: '0.5rem', marginTop: '1rem' }}>
                    <button
                      title="Good answer"
                      onClick={() => submitFeedback(m.answer_id, 'up')}
                      style={{ background: m.feedback === 'up' ? 'rgba(34,197,94,0.15)' : 'none', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', padding: '0.3rem 0.5rem', cursor: 'pointer', color: m.feedback === 'up' ? 'var(--success)' : 'var(--text-muted)', display: 'flex' }}
                    >
                      <ThumbsUp size={14} />
                    </button>
                    <button
                      title="Bad answer"
                      onClick={() => submitFeedback(m.answer_id, 'down')}
                      style={{ background: m.feedback === 'down' ? 'rgba(239,68,68,0.15)' : 'none', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', padding: '0.3rem 0.5rem', cursor: 'pointer', color: m.feedback === 'down' ? 'var(--error)' : 'var(--text-muted)', display: 'flex' }}
                    >
                      <ThumbsDown size={14} />
                    </button>
                  </div>
                )}
```

- [ ] **Step 2: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Manual verification**

Ask a question, click thumbs-up on the answer, confirm the button highlights green and
stays highlighted (state persists in `messages`); reload the conversation via the
sidebar (click away to another/new chat, then click back) and confirm — this is a real
gap to note, not silently pass: `get_conversation`'s `messages` (Task 2) does not
currently include per-answer feedback rating, so a reloaded conversation will NOT show
prior feedback highlighting even though it was recorded. This is acceptable for this
task (the feedback write path is verifiably correct — `record_feedback` is unchanged and
already tested) but must be logged as a known follow-up rather than silently accepted;
add it to the ledger described below.

- [ ] **Step 4: Note the reload gap for follow-up (no code change in this task)**

This finding — reloading a conversation loses the feedback highlight because
`get_conversation()` doesn't join `stakeholder_feedback` — is real but out of scope for
this plan's UI-shell goal. Leave a one-line TODO comment directly above `get_conversation`
in `analytics_platform/stakeholder.py` (added in Task 2) rather than silently leaving no
trace:

```python
    def get_conversation(self, tenant_id: str, conversation_id: str) -> Optional[Dict[str, Any]]:
        # NOTE: messages don't carry prior feedback ratings (no join against
        # stakeholder_feedback) -- reloading a conversation loses the thumbs-up/down
        # highlight even though the rating itself is correctly persisted. Follow-up.
        store = self.stores.for_tenant(tenant_id)
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/StakeholderChat.tsx analytics_platform/stakeholder.py
git commit -m "feat(frontend): wire thumbs-up/down feedback into the chat thread"
```

---

## Task 9: Collapsible SQL code blocks

**Files:**
- Modify: `frontend/src/components/StakeholderChat.tsx`

**Interfaces:**
- Consumes: `m.queries_run: string[]` (existing field, already returned by every
  `answer()` path).

- [ ] **Step 1: Add a `CollapsibleCode` subcomponent**

In `StakeholderChat.tsx`, add above the `ConversationHistorySidebar` function:

```tsx
function CollapsibleCode({ label, code }: { label: string; code: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ marginTop: '1rem' }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)', fontSize: '0.8rem', textTransform: 'uppercase', letterSpacing: '0.05em', padding: 0 }}
      >
        <span style={{ display: 'inline-block', transition: 'transform 0.15s', transform: open ? 'rotate(90deg)' : 'none' }}>▶</span>
        {label}
      </button>
      {open && (
        <pre style={{ background: '#0a0a0c', padding: '1rem', borderRadius: '8px', overflowX: 'auto', fontSize: '0.85rem', color: '#a0a0a0', border: '1px solid rgba(255,255,255,0.05)', marginTop: '0.5rem' }}>
          <code>{code}</code>
        </pre>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Replace the plain `<pre>` SQL block with it**

In the message-rendering block (after the feedback buttons added in Task 8), replace
whatever renders `m.queries_run` (nothing does yet in Task 6's version — this is new)
with:

```tsx
                {m.queries_run && m.queries_run.length > 0 && (
                  m.queries_run.map((q, qi) => (
                    <CollapsibleCode key={qi} label={`SQL executed${m.queries_run.length > 1 ? ` (${qi + 1}/${m.queries_run.length})` : ''}`} code={q} />
                  ))
                )}
```

Place it inside the message card, after the `{m.chart_config && (...)}` block.

- [ ] **Step 3: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Manual verification**

Ask a question that triggers SQL execution (e.g. "how many retail orders per month" on
a tenant seeded with the golden query fixture — matches `WEEKLY_ORDER_SQL`). Confirm the
"SQL EXECUTED" section renders collapsed by default and expands/collapses on click,
without affecting scroll position of the thread.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/StakeholderChat.tsx
git commit -m "feat(frontend): collapsible SQL code blocks in the chat thread"
```

---

## Self-Review Notes

- **Spec coverage** against the original "2. Stakeholder Q&A Feature Requirements": ✅
  expanded scrollable chat window (Task 6), ✅ chat history sidebar with
  rename/delete/star (Task 6), ✅ thumbs-up/down feedback (Task 8), ✅ collapsible code
  sections for generated SQL (Task 9). Collapsible sections for generated **Python**
  are explicitly deferred — there is no Python execution path yet (see plan 2 in the
  INDEX); `CollapsibleCode` in Task 9 is written generically (`label`/`code` props) so
  plan 2 can reuse it for Python cells without modification.
  Multi-turn analyst-mimicking session choosing Python-vs-SQL, the repair loop, export,
  and the 50,000-token warning are explicitly out of scope for this plan — see the
  INDEX for why they're separate plans.
- **Known gap logged, not silently dropped:** Task 8 Step 4 — reloading a conversation
  doesn't restore prior feedback highlighting. Flagged in-code (Task 8 Step 4) rather
  than fixed here, since fixing it means widening `get_conversation`'s query with a
  `stakeholder_feedback` join and deciding a rating-precedence rule if an answer somehow
  has multiple feedback rows — a small but real design decision, not a one-line fix, and
  not blocking for a first shippable version of the thread UI.
