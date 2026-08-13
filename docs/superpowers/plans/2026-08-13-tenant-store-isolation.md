# Tenant Store Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every tenant its own physical SQLite database, and make that an invariant the code enforces rather than a launcher convention an environment variable can defeat.

**Architecture:** The schema splits in two. A **control-plane** database holds the handful of genuinely cross-tenant tables (`tenants`, `scheduler_state`, `api_logs`, `auth_principals`). Every other table lives in a **per-tenant** database at `tenants/<tenant_id>/tenant.db`. A `TenantStoreProvider` maps `tenant_id → Store`, caching connections and refusing to open a database owned by a different tenant. Services stop holding a single `Store` and hold the provider instead, resolving the right store per call. The same code then serves one-process-for-all-companies and one-process-per-company without change.

**Tech Stack:** Python 3.14, stdlib `sqlite3`, `unittest` + `pytest`.

## Global Constraints

- **No new dependencies.**
- **Each tenant is a different company.** The isolation boundary is the database file, never a `WHERE tenant_id = ?` clause. A shared file scoped by column is not acceptable isolation, regardless of how careful the queries are.
- **Isolation failures are loud.** Opening a tenant database whose recorded owner differs from the requested tenant raises `TenantIsolationError`. It is never a warning, never a silent fallback.
- **Tenant ids are identifiers, not paths.** Any id that would escape the tenants root is refused before touching the filesystem.
- **Existing `tenant_id` columns and filters stay.** They become defence-in-depth behind the file boundary. Removing them is out of scope and would widen the diff for no isolation gain.
- **Core, not tenant.** All code lands in `analytics_platform/`. Per `AGENTS.md` Part 1 §2.
- **No silent failures.** Every `except` this plan touches logs at WARNING or higher via `logging.getLogger(__name__)`.
- **`data/platform.db` is disposable dev scratch** (Smoke, CP11 Check, Hardcoded UI Tenant, and a 3-node Acme). No split migration is written for it. `tenants/DTDL/platform.db` is real data with 1245 nodes and one tenant — Task 6 adopts it into the new layout.
- Run all commands from the repo root with `.venv/bin/python`.

---

## Why this plan exists

`run_dashboard.command tenants/DTDL` exports `ANALYTICS_DATA_DIR`, which makes `Settings.resolve_db_path()` return `tenants/DTDL/platform.db`. That already gives DTDL a private database — the intent was always one company per file. But nothing enforces it. The default path, `data/platform.db`, currently holds four tenants in one file. The difference between isolated and co-mingled is one environment variable, set correctly by convention.

This plan moves that guarantee from the launcher into the code.

---

## File Structure

**Created:**
- `analytics_platform/stores.py` — `TenantStoreProvider`, `TenantIsolationError`
- `tests/test_tenant_stores.py`, `tests/test_store_wiring.py`

**Modified:**
- `analytics_platform/database.py` — `SCHEMA` splits into `CONTROL_SCHEMA` + `TENANT_SCHEMA`; `Store` takes a schema
- `analytics_platform/config.py` — `resolve_control_db_path()`, `resolve_tenants_root()`
- `analytics_platform/tenancy.py` — `TenantService` uses the control store
- `analytics_platform/api.py` — `make_context` builds the provider
- `analytics_platform/{pipeline,stakeholder,junior,junior_worker,senior,onboarding,research,triage,anomaly,scheduler}.py` — hold the provider, resolve per call
- `analytics_platform/cli.py` — `adopt-db` command
- `tests/helpers.py` — `Ctx` exposes a provider

---

### Task 1: Split the schema into control plane and tenant plane

**Why:** Before a store can be per-tenant, it has to be clear which tables belong to a tenant at all. Nineteen tables exist; seventeen carry `tenant_id`. `tenants` is the registry that tells you which tenants exist, so it cannot live inside a tenant's own database — you would need to know the tenant to find the tenant. `scheduler_state` is process-wide. `api_logs` and `auth_principals` are written by the API edge before a tenant is necessarily resolved, so they stay central too.

**Files:**
- Modify: `analytics_platform/database.py:19-100` (`SCHEMA`), `analytics_platform/database.py:182-190` (`Store.__init__`), `analytics_platform/database.py:127-145` (`init_db`)
- Test: `tests/test_tenant_stores.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `CONTROL_SCHEMA: str` — `tenants`, `scheduler_state`, `api_logs`, `auth_principals`, plus their indexes
  - `TENANT_SCHEMA: str` — the remaining fifteen tables plus a new `db_owner` table, plus their indexes
  - `Store(db_path: str, schema: str = None)` — `schema=None` still means "the full combined schema," exactly today's behaviour. `Store` itself does not default to `TENANT_SCHEMA`: every existing bare `Store(path)` call site in the codebase (nine-plus services, `tests/helpers.py`) still expects the `tenants` table to exist, and none of them are migrated until Task 5. Only `TenantStoreProvider` (Task 2) ever passes `schema=CONTROL_SCHEMA` or `schema=TENANT_SCHEMA` explicitly. This keeps Task 1 a self-contained, fully-green deliverable rather than a change that only becomes correct once Task 5 lands.
  - `SCHEMA` remains as `CONTROL_SCHEMA + TENANT_SCHEMA`, and is *not* just a transitional compatibility shim — it is the real default every unmigrated caller still runs on until Task 6.

  Tasks 2-6 consume `CONTROL_SCHEMA`, `TENANT_SCHEMA` and the new `Store` signature.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tenant_stores.py`:

```python
"""Per-tenant databases: schema split, provider routing, ownership enforcement."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from analytics_platform.database import CONTROL_SCHEMA, TENANT_SCHEMA, Store

CONTROL_TABLES = {"tenants", "scheduler_state", "api_logs", "auth_principals"}
TENANT_TABLES = {
    "company_profiles", "data_sources", "knowledge_nodes", "questions",
    "analysis_runs", "telemetry", "stakeholder_answers", "stakeholder_feedback",
    "research_sources", "research_docs", "audit_log", "company_profile_history",
    "analyst_configs", "analyst_config_history", "kpis", "db_owner",
}


class SchemaSplitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _tables(self, store: Store) -> set:
        rows = store.query_all(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")
        return {r["name"] for r in rows}

    def test_control_store_has_the_control_tables(self):
        store = Store(os.path.join(self._tmp.name, "control.db"), schema=CONTROL_SCHEMA)
        self.assertTrue(CONTROL_TABLES.issubset(self._tables(store)))
        store.close()

    def test_control_store_has_no_tenant_tables(self):
        store = Store(os.path.join(self._tmp.name, "control.db"), schema=CONTROL_SCHEMA)
        self.assertEqual(self._tables(store) & {"knowledge_nodes", "analysis_runs"}, set())
        store.close()

    def test_tenant_store_has_the_tenant_tables(self):
        store = Store(os.path.join(self._tmp.name, "t.db"), schema=TENANT_SCHEMA)
        self.assertTrue(TENANT_TABLES.issubset(self._tables(store)))
        store.close()

    def test_tenant_store_has_no_tenants_registry(self):
        """A tenant database must not be able to see the list of other companies."""
        store = Store(os.path.join(self._tmp.name, "t.db"), schema=TENANT_SCHEMA)
        self.assertNotIn("tenants", self._tables(store))
        store.close()

    def test_the_two_schemas_do_not_overlap(self):
        self.assertEqual(CONTROL_TABLES & TENANT_TABLES, set())

    def test_bare_store_still_gets_the_combined_schema(self):
        """Every unmigrated caller in the codebase relies on this until Task 5."""
        store = Store(os.path.join(self._tmp.name, "d.db"))
        tables = self._tables(store)
        self.assertIn("knowledge_nodes", tables)  # tenant-plane table
        self.assertIn("tenants", tables)          # control-plane table
        store.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tenant_stores.py -v`
Expected: FAIL with `ImportError: cannot import name 'CONTROL_SCHEMA'`.

- [ ] **Step 3: Split the schema string**

In `analytics_platform/database.py`, replace the single `SCHEMA = """..."""` with two strings. Move each `CREATE TABLE` verbatim — do not retype the column lists, cut and paste them so no column is lost.

`CONTROL_SCHEMA` gets: `tenants`, `scheduler_state`, `api_logs`, `auth_principals`, and the indexes referencing only those tables (`idx_api_logs_ts`, `idx_api_logs_tenant`).

`TENANT_SCHEMA` gets every other `CREATE TABLE`, their indexes, and one new table at the top:

```sql
CREATE TABLE IF NOT EXISTS db_owner (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    tenant_id TEXT NOT NULL,
    bound_at TEXT NOT NULL
);
```

The `CHECK (singleton = 1)` makes it structurally impossible for one database to record two owners.

Keep a compatibility alias below both, so nothing breaks mid-refactor:

```python
# Retained so call sites still on the single-database model keep working until
# Task 6 removes the last of them.
SCHEMA = CONTROL_SCHEMA + TENANT_SCHEMA
```

- [ ] **Step 4: Let Store take a schema**

Replace `init_db` and `Store.__init__` in `analytics_platform/database.py`:

```python
def init_db(conn: sqlite3.Connection, schema: str = None) -> None:
    with _LOCK:
        conn.executescript(schema if schema is not None else SCHEMA)
        conn.commit()
        _migrate(conn)
```

```python
class Store:
    """Thin wrapper: safe single write + serialization helpers.

    A Store is one SQLite file. `schema=None` is the full combined schema — every
    existing caller in the codebase still relies on this until Task 5 migrates it
    onto TenantStoreProvider. Only the provider ever passes an explicit
    CONTROL_SCHEMA or TENANT_SCHEMA.
    """

    def __init__(self, db_path: str, schema: str = None):
        self.db_path = db_path
        self.schema = schema if schema is not None else SCHEMA
        self.conn = get_conn(db_path)
        init_db(self.conn, self.schema)
```

`_migrate` runs `PRAGMA table_info` against tables that may not exist in a control store. Guard each block — wrap the `analysis_runs` and `stakeholder_answers` migrations in a check:

```python
    def _has(table: str) -> bool:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone())
```

and skip each migration block when its table is absent.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_tenant_stores.py -v`
Expected: 6 passed.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: unchanged from baseline — a bare `Store(path)` still gets the full combined schema (control + tenant tables together, exactly as before this task), so every existing caller keeps working untouched. Nothing in this task changes what any existing caller sees; it only adds two new constants and two new tables that nothing reads from yet. If the full suite shows new failures, the schema split leaked into `Store`'s default — re-check Step 4 against the corrected default above before touching anything else. Record the baseline first if you have not:

```bash
git stash -u && .venv/bin/python -m pytest tests/ -q 2>&1 | tail -5; git stash pop
```

- [ ] **Step 7: Commit**

```bash
git add analytics_platform/database.py tests/test_tenant_stores.py
git commit -m "feat(db): split the schema into control-plane and tenant-plane"
```

---

### Task 2: The store provider

**Why:** This is the object that makes isolation structural. It owns the mapping from tenant id to database file, caches connections so a request does not reopen SQLite each time, refuses ids that would escape the tenants root, and — the important part — records which tenant owns each database and refuses to hand back a store whose recorded owner disagrees. That last check is what turns a mis-set path from silent co-mingling into an exception.

**Files:**
- Create: `analytics_platform/stores.py`
- Test: `tests/test_tenant_stores.py` (append)

**Interfaces:**
- Consumes: `Store`, `CONTROL_SCHEMA`, `TENANT_SCHEMA` (Task 1).
- Produces:
  - `class TenantIsolationError(Exception)`
  - `class TenantStoreProvider(control_db_path: str, tenants_root: str)`
    - `.control -> Store` (property, lazily opened)
    - `.for_tenant(tenant_id: str) -> Store`
    - `.tenant_db_path(tenant_id: str) -> str`
    - `.known_tenants() -> List[str]` — ids with a database on disk
    - `.close_all() -> None`

  Tasks 3-6 consume all of these.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tenant_stores.py`:

```python
from analytics_platform.stores import TenantIsolationError, TenantStoreProvider


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.provider = TenantStoreProvider(
            control_db_path=os.path.join(self.root, "control.db"),
            tenants_root=os.path.join(self.root, "tenants"))

    def tearDown(self):
        self.provider.close_all()
        self._tmp.cleanup()

    def test_each_tenant_gets_its_own_file(self):
        a = self.provider.for_tenant("acme")
        b = self.provider.for_tenant("globex")
        self.assertNotEqual(a.db_path, b.db_path)

    def test_both_files_exist_on_disk(self):
        self.provider.for_tenant("acme")
        self.provider.for_tenant("globex")
        self.assertTrue(os.path.exists(self.provider.tenant_db_path("acme")))
        self.assertTrue(os.path.exists(self.provider.tenant_db_path("globex")))

    def test_the_store_is_cached_per_tenant(self):
        self.assertIs(self.provider.for_tenant("acme"),
                      self.provider.for_tenant("acme"))

    def test_writes_do_not_leak_between_tenants(self):
        """The isolation guarantee, stated as a test."""
        a = self.provider.for_tenant("acme")
        a.execute(
            "INSERT INTO knowledge_nodes (id,tenant_id,kind,status,version,title,"
            "summary,payload,confidence,evidence_ref,source_ref,created_at,"
            "updated_at,created_by,reviewed_by,review_notes,supersedes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("kn_1", "acme", "METRIC", "APPROVED", 1, "Secret metric", "",
             "{}", "{}", "", "", "2026-01-01", "2026-01-01", "s", "", "", ""))
        b = self.provider.for_tenant("globex")
        self.assertEqual(b.query_all("SELECT id FROM knowledge_nodes"), [])

    def test_a_tenant_database_cannot_be_opened_by_another_tenant(self):
        """The real-world mistake this guards: a bad restore, or a mis-set
        ANALYTICS_DATA_DIR, that puts one company's file where another's belongs."""
        self.provider.for_tenant("acme")
        self.provider.close_all()
        shutil.copy(self.provider.tenant_db_path("acme"),
                    self.provider.tenant_db_path("globex"))
        rogue = TenantStoreProvider(
            control_db_path=os.path.join(self.root, "control.db"),
            tenants_root=os.path.join(self.root, "tenants"))
        with self.assertRaises(TenantIsolationError):
            rogue.for_tenant("globex")
        rogue.close_all()

    def test_ownership_is_recorded_on_first_open(self):
        store = self.provider.for_tenant("acme")
        row = store.query_one("SELECT tenant_id FROM db_owner WHERE singleton = 1")
        self.assertEqual(row["tenant_id"], "acme")

    def test_reopening_the_same_tenant_is_fine(self):
        self.provider.for_tenant("acme")
        self.provider.close_all()
        again = TenantStoreProvider(
            control_db_path=os.path.join(self.root, "control.db"),
            tenants_root=os.path.join(self.root, "tenants"))
        self.assertIsNotNone(again.for_tenant("acme"))
        again.close_all()

    def test_the_control_store_holds_the_registry(self):
        self.assertIsNotNone(
            self.provider.control.query_all("SELECT id FROM tenants"))

    def test_the_control_store_has_no_knowledge_nodes(self):
        with self.assertRaises(Exception):
            self.provider.control.query_all("SELECT id FROM knowledge_nodes")

    def test_known_tenants_lists_databases_on_disk(self):
        self.provider.for_tenant("acme")
        self.provider.for_tenant("globex")
        self.assertEqual(sorted(self.provider.known_tenants()), ["acme", "globex"])


class TenantIdSafetyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.provider = TenantStoreProvider(
            control_db_path=os.path.join(self._tmp.name, "control.db"),
            tenants_root=os.path.join(self._tmp.name, "tenants"))

    def tearDown(self):
        self.provider.close_all()
        self._tmp.cleanup()

    def test_path_traversal_is_refused(self):
        with self.assertRaises(ValueError):
            self.provider.for_tenant("../../etc")

    def test_a_separator_is_refused(self):
        with self.assertRaises(ValueError):
            self.provider.for_tenant("acme/globex")

    def test_an_empty_id_is_refused(self):
        with self.assertRaises(ValueError):
            self.provider.for_tenant("")

    def test_a_dot_id_is_refused(self):
        with self.assertRaises(ValueError):
            self.provider.for_tenant(".")

    def test_a_normal_id_is_accepted(self):
        self.assertIsNotNone(self.provider.for_tenant("tnt_d23cd823d4c6"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tenant_stores.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'analytics_platform.stores'`.

- [ ] **Step 3: Write the implementation**

Create `analytics_platform/stores.py`:

```python
"""Per-tenant database routing.

Every tenant is a different company, so the isolation boundary is the database
file — not a `WHERE tenant_id = ?` clause. This module owns the mapping from a
tenant id to that company's own SQLite file, and refuses to hand back a database
whose recorded owner disagrees with the tenant being asked for.

Two planes:

* control — `tenants`, `scheduler_state`, `api_logs`, `auth_principals`. One file.
  The registry cannot live inside a tenant's database, because you would need to
  know the tenant in order to find the tenant.
* tenant — everything else, one file per company at `<root>/<tenant_id>/tenant.db`.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Dict, List, Optional

from .database import CONTROL_SCHEMA, TENANT_SCHEMA, Store
from .domain import now_iso

logger = logging.getLogger(__name__)

TENANT_DB_FILENAME = "tenant.db"

# A tenant id names a directory, so it is restricted to characters that cannot
# traverse or escape. Real ids look like `tnt_d23cd823d4c6` or `DTDL`. `\Z` (not
# `$`) anchors the end so a trailing newline can't sneak an id past this check —
# `$` matches before a final "\n" in Python, `\Z` does not.
_SAFE_TENANT_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class TenantIsolationError(Exception):
    """A tenant database's recorded owner does not match the tenant requested."""


def validate_tenant_id(tenant_id: str) -> str:
    if not tenant_id or not _SAFE_TENANT_ID.match(tenant_id) or tenant_id in (".", ".."):
        raise ValueError(f"unsafe tenant id {tenant_id!r}: must match "
                         f"{_SAFE_TENANT_ID.pattern}")
    return tenant_id


class TenantStoreProvider:
    def __init__(self, control_db_path: str, tenants_root: str):
        self.control_db_path = control_db_path
        self.tenants_root = os.path.abspath(tenants_root)
        self._control: Optional[Store] = None
        self._tenants: Dict[str, Store] = {}

    # -- control plane -------------------------------------------------------
    @property
    def control(self) -> Store:
        if self._control is None:
            self._control = Store(self.control_db_path, schema=CONTROL_SCHEMA)
            logger.info("control store opened at %s", self.control_db_path)
        return self._control

    # -- tenant plane --------------------------------------------------------
    def tenant_db_path(self, tenant_id: str) -> str:
        validate_tenant_id(tenant_id)
        path = os.path.abspath(
            os.path.join(self.tenants_root, tenant_id, TENANT_DB_FILENAME))
        if not path.startswith(self.tenants_root + os.sep):
            raise ValueError(f"tenant id {tenant_id!r} escapes {self.tenants_root}")
        return path

    def for_tenant(self, tenant_id: str) -> Store:
        """This company's database. Opens and binds it on first use."""
        cached = self._tenants.get(tenant_id)
        if cached is not None:
            return cached

        path = self.tenant_db_path(tenant_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        store = Store(path, schema=TENANT_SCHEMA)
        try:
            self._bind_owner(store, tenant_id, path)
        except Exception:
            # Any failure here — the isolation mismatch below, or anything else
            # (a corrupt file, a race) — must not leave an open, uncached handle.
            store.close()
            raise
        self._tenants[tenant_id] = store
        logger.debug("tenant store for %s opened at %s", tenant_id, path)
        return store

    @staticmethod
    def _bind_owner(store: Store, tenant_id: str, path: str) -> None:
        """Record the owner, or refuse if this file belongs to someone else.

        `INSERT OR IGNORE` then re-read makes the bind race-safe: if two threads
        open the same fresh file concurrently, exactly one INSERT wins (the
        `db_owner` PRIMARY KEY enforces that), and both threads then read back
        whichever tenant_id actually landed — so a losing thread sees a real
        mismatch and gets TenantIsolationError, never a raw IntegrityError.
        """
        store.execute(
            "INSERT OR IGNORE INTO db_owner (singleton, tenant_id, bound_at) "
            "VALUES (1,?,?)", (tenant_id, now_iso()))
        row = store.query_one("SELECT tenant_id FROM db_owner WHERE singleton = 1")
        owner = row["tenant_id"]
        if owner != tenant_id:
            raise TenantIsolationError(
                f"{path} belongs to tenant {owner!r}, refusing to open it as "
                f"{tenant_id!r}. Each tenant is a separate company and must have "
                f"its own database.")

    def known_tenants(self) -> List[str]:
        """Tenant ids that have a database on disk."""
        if not os.path.isdir(self.tenants_root):
            return []
        out: List[str] = []
        for entry in sorted(os.listdir(self.tenants_root)):
            candidate = os.path.join(self.tenants_root, entry, TENANT_DB_FILENAME)
            if os.path.exists(candidate):
                out.append(entry)
        return out

    def close_all(self) -> None:
        for tenant_id, store in list(self._tenants.items()):
            try:
                store.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("closing store for %s failed: %s", tenant_id, exc)
        self._tenants.clear()
        if self._control is not None:
            try:
                self._control.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("closing control store failed: %s", exc)
            self._control = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_tenant_stores.py -v`
Expected: 21 passed.

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/stores.py tests/test_tenant_stores.py
git commit -m "feat(db): TenantStoreProvider with enforced per-tenant ownership"
```

---

### Task 3: Settings resolve the two paths

**Why:** `resolve_db_path()` returns one file and is the seam every service currently uses. It has to become two resolvers so the provider can be built from settings alone, and so `ANALYTICS_DATA_DIR` keeps working for anyone running one process per company.

**Files:**
- Modify: `analytics_platform/config.py:57-65`
- Test: `tests/test_tenant_stores.py` (append)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Settings.resolve_control_db_path() -> str` — `<data_dir>/control.db`, else `data/control.db`
  - `Settings.resolve_tenants_root() -> str` — `<data_dir>/tenants`, else `tenants`
  - `resolve_db_path()` is kept, marked deprecated, and used only by `adopt-db` in Task 6.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tenant_stores.py`:

```python
class SettingsPathTest(unittest.TestCase):
    def test_control_path_defaults_under_data(self):
        from analytics_platform.config import Settings
        self.assertEqual(Settings().resolve_control_db_path(), "data/control.db")

    def test_tenants_root_defaults_to_tenants(self):
        from analytics_platform.config import Settings
        self.assertEqual(Settings().resolve_tenants_root(), "tenants")

    def test_data_dir_moves_both(self):
        from analytics_platform.config import Settings
        s = Settings(data_dir="/srv/acme")
        self.assertEqual(s.resolve_control_db_path(), "/srv/acme/control.db")
        self.assertEqual(s.resolve_tenants_root(), "/srv/acme/tenants")

    def test_the_two_paths_are_distinct(self):
        from analytics_platform.config import Settings
        s = Settings(data_dir="/srv/acme")
        self.assertNotIn(s.resolve_control_db_path(), s.resolve_tenants_root())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tenant_stores.py::SettingsPathTest -v`
Expected: FAIL — `'Settings' object has no attribute 'resolve_control_db_path'`.

- [ ] **Step 3: Write the implementation**

In `analytics_platform/config.py`, add beside `resolve_db_path`:

```python
    def resolve_control_db_path(self) -> str:
        """The cross-tenant registry database (tenants, scheduler, api logs)."""
        if self.data_dir:
            return os.path.join(self.data_dir, "control.db")
        return "data/control.db"

    def resolve_tenants_root(self) -> str:
        """Directory holding one database per company: <root>/<tenant_id>/tenant.db."""
        if self.data_dir:
            return os.path.join(self.data_dir, "tenants")
        return "tenants"
```

Mark the old one deprecated without deleting it — `adopt-db` still needs it:

```python
    def resolve_db_path(self) -> str:
        """DEPRECATED: the single shared database. Use resolve_control_db_path()
        and resolve_tenants_root(). Retained so `adopt-db` can find a legacy file."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_tenant_stores.py -v`
Expected: 25 passed.

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/config.py tests/test_tenant_stores.py
git commit -m "feat(config): resolve control-plane and tenants-root paths separately"
```

---

### Task 4: Move the control plane onto the control store

**Why:** `TenantService` reads and writes the `tenants` table, which now lives only in the control database. Until it points there, creating a tenant writes the registry into whichever file happens to be open — which is the co-mingling this plan removes.

**Files:**
- Modify: `analytics_platform/tenancy.py`, `analytics_platform/observability.py`, `analytics_platform/scheduler.py`
- Test: `tests/test_store_wiring.py`

**Interfaces:**
- Consumes: `TenantStoreProvider` (Task 2).
- Produces: `TenantService(stores: TenantStoreProvider)` — reads/writes `tenants` via `stores.control`, and per-tenant config via `stores.for_tenant(tenant_id)`. `TenantService.store` is removed; callers use `self.stores`. This task's own test (below) calls `create(tenant_id, name=...)` and `list()`, which do not exist on `TenantService` yet — add them as new methods alongside the existing `create_tenant(name, ...)` (auto-generated id) and `list_tenants()` (returns `List[Dict]`), sharing an `_insert_tenant`/`_row_to_tenant` helper so the two pairs are not independent implementations. `create` takes a caller-assigned id (useful for tests and any future caller that needs a deterministic id); `list` returns typed `Tenant` objects rather than dicts. Every existing caller of `create_tenant`/`list_tenants` (`onboarding.py`, `api.py`) is untouched — do not rename or remove them.

- [ ] **Step 1: Write the failing test**

Create `tests/test_store_wiring.py`:

```python
"""Control-plane and tenant-plane data land in the right database."""
from __future__ import annotations

import os
import tempfile
import unittest

from analytics_platform.stores import TenantStoreProvider
from analytics_platform.tenancy import TenantService


class TenantServiceWiringTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.stores = TenantStoreProvider(
            control_db_path=os.path.join(self._tmp.name, "control.db"),
            tenants_root=os.path.join(self._tmp.name, "tenants"))
        self.tenants = TenantService(self.stores)

    def tearDown(self):
        self.stores.close_all()
        self._tmp.cleanup()

    def test_a_created_tenant_lands_in_the_control_database(self):
        self.tenants.create("acme", name="Acme")
        rows = self.stores.control.query_all("SELECT id FROM tenants")
        self.assertEqual([r["id"] for r in rows], ["acme"])

    def test_listing_tenants_reads_the_control_database(self):
        self.tenants.create("acme", name="Acme")
        self.tenants.create("globex", name="Globex")
        self.assertEqual(sorted(t.id for t in self.tenants.list()), ["acme", "globex"])

    def test_creating_a_tenant_creates_its_database(self):
        self.tenants.create("acme", name="Acme")
        self.assertTrue(os.path.exists(self.stores.tenant_db_path("acme")))

    def test_analyst_config_lands_in_the_tenant_database(self):
        self.tenants.create("acme", name="Acme")
        self.tenants.get_analyst_config("acme")
        rows = self.stores.for_tenant("acme").query_all(
            "SELECT tenant_id FROM analyst_configs")
        self.assertTrue(all(r["tenant_id"] == "acme" for r in rows))

    def test_one_tenants_config_is_invisible_to_another(self):
        self.tenants.create("acme", name="Acme")
        self.tenants.create("globex", name="Globex")
        self.tenants.get_analyst_config("acme")
        rows = self.stores.for_tenant("globex").query_all(
            "SELECT tenant_id FROM analyst_configs")
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_store_wiring.py -v`
Expected: FAIL — `TenantService.__init__` takes a `Store`, and `Store` has no attribute matching the provider.

- [ ] **Step 3: Rewire TenantService**

Read `analytics_platform/tenancy.py` in full first. Then:

- Change `__init__(self, store: Store)` to `__init__(self, stores: TenantStoreProvider)`, setting `self.stores = stores`.
- Every method touching the `tenants` table uses `self.stores.control`.
- Every method touching `analyst_configs`, `analyst_config_history`, `company_profiles`, `company_profile_history`, `data_sources` uses `self.stores.for_tenant(tenant_id)`.
- Add `create(tenant_id, name, ...)` and `list()` per the Interfaces note above; keep `create_tenant`/`list_tenants` untouched for existing callers.

Apply the same split to `Observability` (`telemetry` is tenant-scoped, `api_logs` is control) and to `Scheduler` (`scheduler_state` is control).

`Observability.event()` and any other method that can be called with `tenant_id=""` (a platform-level event with no owning company — e.g. the scheduler's own maintenance/log-purge bookkeeping) has nowhere to route a tenant-scoped `telemetry` write: `stores.for_tenant("")` raises `ValueError` before touching disk. Do not let that vanish into the method's existing broad exception handler silently — catch it specifically and `logger.warning(...)` that a platform-level event was dropped, naming the stage/actor, before falling through to whatever the existing swallow-everything behavior was. This is a known, accepted gap for this plan (no control-plane telemetry table exists yet) — the point of this step is only that the drop is loud, not that it stops happening.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_store_wiring.py -v`
Expected: 5 passed.

- [ ] **Step 4b: Run the full suite and confirm the expected, bounded set of failures**

Run: `.venv/bin/python -m pytest tests/ -q`

This task deliberately does not migrate every consumer — that is Task 5's job, which is why it exists as a separate task. Expect the suite to regress from 224 passed/1 skipped to **223 passed, 6 failed, 1 skipped** (224 + 5 new tests from this task, minus 6 newly-failing ones), and expect every one of the 6 failures to trace to a file Task 5 is responsible for (`api.py`, `billing.py`, or `retention.py`) still constructing `TenantService`/`Observability`/`Scheduler` with a bare `Store`, or reading `tenants`/`telemetry` off its own flat store independently of those classes. If the failure count differs, or any failure traces to a file *not* in that list, stop — that is a real regression, not the expected midpoint state, and must be fixed before this task is done.

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/ tests/test_store_wiring.py
git commit -m "feat(db): route the tenant registry to the control store"
```

---

### Task 5: Thread the provider through the tenant-scoped services

**Why:** Eleven services hold `self.store` and take `tenant_id` per method. Each needs to hold the provider and resolve the right database per call. This is the widest part of the plan, but it is mechanical: the pattern is identical in every file, and `CompanyBrain` itself needs no change because it already takes `(store, tenant_id)` — it simply receives the tenant's own store now.

This task also closes the 6 failures Task 4 deliberately left open: `api.py` (`make_context`/`_make_junior_worker` still construct `TenantService`/`Observability`/`Scheduler` with a bare `Store`), `billing.py` (`BillingService` reads `tenants`/`telemetry` off its own flat `self.store`, independent of `Observability` — which now writes `telemetry` per tenant), and `retention.py` (`RetentionService.delete_tenant` deletes from `tenants` and tenant-scoped tables off its own flat `self.store`, never reaching the control database `TenantService` now reads from). `billing.py` and `retention.py` were missed when this task was first scoped — found only once Task 4 actually ran and traced its 6 failures to source; both belong here alongside the other nine.

**Files:**
- Modify: `analytics_platform/{pipeline,stakeholder,junior,junior_worker,senior,onboarding,research,triage,anomaly,billing,retention}.py`, `analytics_platform/api.py:270-320`, `tests/helpers.py`
- Test: `tests/test_store_wiring.py` (append)

**Interfaces:**
- Consumes: `TenantStoreProvider` (Task 2), `TenantService(stores)` (Task 4).
- Produces: each service takes `stores: TenantStoreProvider` as its first argument in place of `store: Store`, and resolves `store = self.stores.for_tenant(tenant_id)` at the top of every tenant-scoped method. `make_context` builds one provider and passes it to all of them. `Ctx` in `tests/helpers.py` exposes `.stores` and keeps `.store` as `stores.for_tenant(DEFAULT_TEST_TENANT)` so existing tests keep working.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_store_wiring.py`:

```python
class EndToEndIsolationTest(unittest.TestCase):
    """The guarantee, end to end: two companies through one process."""

    def setUp(self):
        from analytics_platform.api import make_context
        from analytics_platform.config import Settings
        self._tmp = tempfile.TemporaryDirectory()
        self.ctx = make_context(Settings(data_dir=self._tmp.name,
                                         embedding_enabled=False))
        self.ctx.tenants.create("acme", name="Acme")
        self.ctx.tenants.create("globex", name="Globex")

    def tearDown(self):
        self.ctx.stores.close_all()
        self._tmp.cleanup()

    def _add_node(self, tenant_id: str, title: str) -> str:
        from analytics_platform.domain import NodeKind
        brain = self.ctx.pipeline.brain(tenant_id)
        return brain.create(NodeKind.METRIC, title, summary="x").id

    def test_each_tenant_sees_only_its_own_knowledge(self):
        from analytics_platform.domain import NodeKind
        self._add_node("acme", "Acme margin")
        self._add_node("globex", "Globex margin")
        acme = [n.title for n in self.ctx.pipeline.brain("acme").all(kind=NodeKind.METRIC)]
        self.assertEqual(acme, ["Acme margin"])

    def test_the_two_tenants_use_different_files(self):
        self.assertNotEqual(self.ctx.stores.for_tenant("acme").db_path,
                            self.ctx.stores.for_tenant("globex").db_path)

    def test_a_node_id_from_one_tenant_is_not_readable_by_the_other(self):
        node_id = self._add_node("acme", "Acme margin")
        self.assertIsNone(self.ctx.pipeline.brain("globex").get(node_id))

    def test_stakeholder_resolves_the_right_store(self):
        self._add_node("acme", "Acme margin")
        self.assertEqual(self.ctx.stakeholder.brain("acme").stats()["total_nodes"], 1)
        self.assertEqual(self.ctx.stakeholder.brain("globex").stats()["total_nodes"], 0)

    def test_deleting_a_tenants_file_removes_all_of_its_data(self):
        """Per-company export and deletion become one filesystem operation."""
        self._add_node("acme", "Acme margin")
        path = self.ctx.stores.tenant_db_path("acme")
        self.assertTrue(os.path.exists(path))
        self.assertTrue(os.path.exists(self.ctx.stores.tenant_db_path("globex")))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_store_wiring.py::EndToEndIsolationTest -v`
Expected: FAIL — `AppContext` has no `stores`, and the services still take a single `Store`.

- [ ] **Step 3: Convert each service**

For each of `pipeline.py`, `stakeholder.py`, `junior.py`, `junior_worker.py`, `senior.py`, `onboarding.py`, `research.py`, `triage.py`, `anomaly.py`, apply this pattern:

```python
    def __init__(self, stores: TenantStoreProvider, ...):
        self.stores = stores
```

and at the top of every method that takes a `tenant_id`:

```python
        store = self.stores.for_tenant(tenant_id)
```

replacing `self.store` with `store` in that method's body. Where a method builds a brain, it becomes:

```python
    def brain(self, tenant_id: str) -> CompanyBrain:
        return CompanyBrain(self.stores.for_tenant(tenant_id), tenant_id)
```

`JuniorWorker` is constructed per tenant and holds `self.tenant_id`; give it `self.store = stores.for_tenant(tenant_id)` once in `__init__` rather than resolving per method.

`billing.py` (`BillingService`) and `retention.py` (`RetentionService`) take the same `stores: TenantStoreProvider` constructor change, but their bodies need more than a mechanical `self.store` → `store` swap, because they each currently do one query that spans both planes:

- `BillingService.platform_report` (or equivalent — read the method that lists `SELECT id, name FROM tenants` alongside per-tenant usage) needs `self.stores.control` for the tenant list and `self.stores.for_tenant(tenant_id)` per tenant for `telemetry`/`stakeholder_answers`/`analysis_runs` — it becomes a loop over `self.stores.control`'s tenant list, aggregating a per-tenant query against each one's own store, not a single query against one flat file.
- `RetentionService.delete_tenant` currently deletes rows from `tenants` plus every tenant-scoped table one at a time against a single flat store. With one file per tenant this simplifies: delete the registry row via `self.stores.control.execute("DELETE FROM tenants WHERE id=?", (tenant_id,))`, then delete the tenant's entire database file (close the cached `Store` first — add `TenantStoreProvider.evict(tenant_id)` to `analytics_platform/stores.py` for this: closes and forgets one cached tenant `Store`, the single-tenant sibling of the close-loop already inside `close_all()` — then `os.remove` on `self.stores.tenant_db_path(tenant_id)` and its `-wal`/`-shm` sidecars) instead of issuing a `DELETE FROM {table}` per table. Confirm `tests/test_governance_retention.py::TestRetention::test_delete_tenant_wipes_all_and_audits` still passes — it is the test that first caught this in Task 4's report; read what it actually asserts before changing the method's return shape.

  **The deletion's own audit record cannot live in the file being deleted.** `audit_log` (`TENANT_SCHEMA`, Task 1) is where `delete_tenant` used to write its "tenant deleted" record — but that table is inside the very file this method now removes, so the record would vanish with the tenant, contradicting this module's own "full tenant deletion leaves an append-only audit record" contract. Add a **separate, distinctly-named** table to `CONTROL_SCHEMA` for this — `tenant_lifecycle_log` (same shape as `audit_log`: `id INTEGER PRIMARY KEY AUTOINCREMENT, ts, tenant_id, actor, role, action, resource, outcome, detail`, plus an index on `tenant_id`). Do not reuse the name `audit_log` for it — a control-plane and a tenant-plane table with the identical name is exactly the kind of ambiguity ("which `audit_log` does this query mean?") a schema split is supposed to remove, and it undoes the file-boundary clarity Task 1 established for a second time in the same plan. `delete_tenant` writes its deletion record into `tenant_lifecycle_log` via `self.stores.control`; the tenant-plane `audit_log` (regular per-tenant activity — approvals, reviews, etc.) is untouched by this change.

  `auth_principals` is control-plane (`CONTROL_SCHEMA`, Task 1), not tenant-scoped — the pre-split version of `delete_tenant` deleted it as if it were tenant-scoped, which stopped being correct the moment Task 1 landed. Keep deleting it (a tenant's auth principals should not survive the tenant), but route that delete through `self.stores.control` explicitly, counted separately from the tenant-file table counts.

  `Observability.event(tenant_id=tenant_id, ...)` must NOT be called with the just-deleted tenant's id after the file is gone — `Observability.event()` routes through `self.stores.for_tenant(tenant_id)`, which creates the file on demand if it's missing, so calling it post-deletion silently resurrects an empty database for the tenant you just removed. Call it with `tenant_id=""` (a platform-level event, matching the pattern `Scheduler`'s own bookkeeping already uses) and carry the deleted tenant's id in `resource=` instead.

Do the files one at a time and run `.venv/bin/python -m pytest tests/ -q` after each, so a break is attributable to one file.

- [ ] **Step 4: Build the provider in make_context**

In `analytics_platform/api.py`, replace:

```python
    store = Store(settings.resolve_db_path())
    tenants = TenantService(store)
```

with:

```python
    from .stores import TenantStoreProvider
    stores = TenantStoreProvider(
        control_db_path=settings.resolve_control_db_path(),
        tenants_root=settings.resolve_tenants_root())
    tenants = TenantService(stores)
```

Pass `stores` to every service constructor in place of `store`. Add `stores` to `AppContext` and keep `store` as a property returning `stores.control`, so the handful of control-plane call sites in `api.py` keep working:

```python
    @property
    def store(self) -> Store:
        """DEPRECATED: the control store. Tenant work must go through `stores`."""
        return self.stores.control
```

- [ ] **Step 5: Update the test helper**

In `tests/helpers.py`, replace the `Ctx` body:

```python
DEFAULT_TEST_TENANT = "t1"


class Ctx:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=self._tmp.name, source_dialect="duckdb",
                                 embedding_enabled=False)
        self.stores = TenantStoreProvider(
            control_db_path=self.settings.resolve_control_db_path(),
            tenants_root=self.settings.resolve_tenants_root())
        self.tenants = TenantService(self.stores)
        self.obs = Observability(self.stores)
        self.executor = SamplerExecutor()
        self.pipeline = Pipeline(self.stores, settings=self.settings,
                                 tenant_service=self.tenants, executor=self.executor,
                                 observability=self.obs)

    @property
    def store(self):
        """Most tests use a single tenant; this is that tenant's database."""
        return self.stores.for_tenant(DEFAULT_TEST_TENANT)

    def close(self):
        self.stores.close_all()
        try:
            self._tmp.cleanup()
        except Exception:
            pass
```

`self.db_path` is referenced by some tests — keep it as `self.settings.resolve_control_db_path()`.

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_store_wiring.py -v`
Expected: 10 passed.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`

Tests that constructed a service with a bare `Store` now need a provider. Update each to use `ctx.stores`. Tests that asserted two tenants share a database were asserting the defect — update them and comment the change with a pointer to this plan.

- [ ] **Step 8: Commit**

```bash
git add analytics_platform/ tests/
git commit -m "refactor(db): services resolve a per-tenant store from the provider

Each tenant is a separate company, so each gets its own SQLite file. Isolation is
now a property of the filesystem rather than of every WHERE clause."
```

---

### Task 6: Adopt the existing DTDL database and retire the shared file

**Why:** `tenants/DTDL/platform.db` holds 1245 real knowledge nodes for a single tenant (`tnt_d23cd823d4c6`). It is already single-tenant, so adopting it is a move plus an ownership binding, not a data migration. `data/platform.db` holds four co-mingled dev tenants and is disposable. Retiring the `SCHEMA` alias afterwards removes the last way to accidentally create a shared file.

**Files:**
- Modify: `analytics_platform/cli.py`, `analytics_platform/database.py` (remove the `SCHEMA` alias)
- Test: `tests/test_tenant_stores.py` (append)

**Interfaces:**
- Consumes: `TenantStoreProvider` (Task 2).
- Produces: `adopt_db(source_path: str, tenant_id: str, stores: TenantStoreProvider) -> int` in `cli.py`, and an `adopt-db` subcommand. Returns the number of knowledge nodes carried over.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tenant_stores.py`:

```python
class AdoptDbTest(unittest.TestCase):
    def setUp(self):
        from analytics_platform.database import SCHEMA_LEGACY_ALL, Store
        self._tmp = tempfile.TemporaryDirectory()
        self.legacy = os.path.join(self._tmp.name, "platform.db")
        legacy = Store(self.legacy, schema=SCHEMA_LEGACY_ALL)
        legacy.execute("INSERT INTO tenants (id,name) VALUES (?,?)", ("acme", "Acme"))
        legacy.execute(
            "INSERT INTO knowledge_nodes (id,tenant_id,kind,status,version,title,"
            "summary,payload,confidence,evidence_ref,source_ref,created_at,"
            "updated_at,created_by,reviewed_by,review_notes,supersedes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("kn_1", "acme", "METRIC", "APPROVED", 1, "Margin", "", "{}", "{}",
             "", "", "2026-01-01", "2026-01-01", "s", "", "", ""))
        legacy.close()
        self.stores = TenantStoreProvider(
            control_db_path=os.path.join(self._tmp.name, "control.db"),
            tenants_root=os.path.join(self._tmp.name, "tenants"))

    def tearDown(self):
        self.stores.close_all()
        self._tmp.cleanup()

    def test_adoption_reports_the_node_count(self):
        from analytics_platform.cli import adopt_db
        self.assertEqual(adopt_db(self.legacy, "acme", self.stores), 1)

    def test_the_nodes_land_in_the_tenant_database(self):
        from analytics_platform.cli import adopt_db
        adopt_db(self.legacy, "acme", self.stores)
        rows = self.stores.for_tenant("acme").query_all(
            "SELECT title FROM knowledge_nodes")
        self.assertEqual([r["title"] for r in rows], ["Margin"])

    def test_the_registry_row_lands_in_the_control_database(self):
        from analytics_platform.cli import adopt_db
        adopt_db(self.legacy, "acme", self.stores)
        rows = self.stores.control.query_all("SELECT id FROM tenants")
        self.assertEqual([r["id"] for r in rows], ["acme"])

    def test_adopting_a_multi_tenant_database_is_refused(self):
        from analytics_platform.cli import adopt_db
        from analytics_platform.database import SCHEMA_LEGACY_ALL, Store
        legacy = Store(self.legacy, schema=SCHEMA_LEGACY_ALL)
        legacy.execute(
            "INSERT INTO knowledge_nodes (id,tenant_id,kind,status,version,title,"
            "summary,payload,confidence,evidence_ref,source_ref,created_at,"
            "updated_at,created_by,reviewed_by,review_notes,supersedes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("kn_2", "globex", "METRIC", "APPROVED", 1, "Other", "", "{}", "{}",
             "", "", "2026-01-01", "2026-01-01", "s", "", "", ""))
        legacy.close()
        with self.assertRaises(ValueError):
            adopt_db(self.legacy, "acme", self.stores)

    def test_adoption_is_idempotent(self):
        from analytics_platform.cli import adopt_db
        adopt_db(self.legacy, "acme", self.stores)
        adopt_db(self.legacy, "acme", self.stores)
        rows = self.stores.for_tenant("acme").query_all("SELECT id FROM knowledge_nodes")
        self.assertEqual(len(rows), 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tenant_stores.py::AdoptDbTest -v`
Expected: FAIL with `ImportError: cannot import name 'SCHEMA_LEGACY_ALL'`.

- [ ] **Step 3: Rename the compatibility alias**

In `analytics_platform/database.py`, replace:

```python
SCHEMA = CONTROL_SCHEMA + TENANT_SCHEMA
```

with:

```python
# The pre-split single-file schema. Retained ONLY so `adopt-db` can open a legacy
# database. Never pass this to a Store that will be written to — a file with both
# planes is exactly the co-mingling this design removes.
SCHEMA_LEGACY_ALL = CONTROL_SCHEMA + TENANT_SCHEMA
```

Then find every remaining reference to the old name and repoint it:

```bash
grep -rn "\bSCHEMA\b" --include="*.py" analytics_platform/ tests/ | grep -v "CONTROL_SCHEMA\|TENANT_SCHEMA\|SCHEMA_LEGACY_ALL"
```

- [ ] **Step 4: Write adopt_db**

Add to `analytics_platform/cli.py`:

```python
TENANT_TABLES = (
    "company_profiles", "data_sources", "knowledge_nodes", "questions",
    "analysis_runs", "telemetry", "stakeholder_answers", "stakeholder_feedback",
    "research_sources", "research_docs", "audit_log", "company_profile_history",
    "analyst_configs", "analyst_config_history", "kpis",
)


def adopt_db(source_path: str, tenant_id: str, stores) -> int:
    """Move a legacy single-file database into the per-tenant layout.

    Refuses a source holding more than one tenant: splitting co-mingled companies
    is a data-ownership decision, not something a CLI should guess at.
    """
    from .database import SCHEMA_LEGACY_ALL, Store

    legacy = Store(source_path, schema=SCHEMA_LEGACY_ALL)
    try:
        found = set()
        for table in TENANT_TABLES:
            try:
                rows = legacy.query_all(f"SELECT DISTINCT tenant_id FROM {table}")
            except Exception:
                continue
            found.update(r["tenant_id"] for r in rows if r["tenant_id"])
        extra = found - {tenant_id}
        if extra:
            raise ValueError(
                f"{source_path} holds data for {sorted(found)}, not just "
                f"{tenant_id!r}. Each tenant is a separate company; split this "
                f"file by hand before adopting it.")

        target = stores.for_tenant(tenant_id)
        moved = 0
        for table in TENANT_TABLES:
            try:
                rows = legacy.query_all(f"SELECT * FROM {table} WHERE tenant_id = ?",
                                        (tenant_id,))
            except Exception:
                continue
            for row in rows:
                data = dict(row)
                cols = ",".join(data)
                marks = ",".join("?" for _ in data)
                target.execute(
                    f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})",
                    tuple(data.values()))
            if table == "knowledge_nodes":
                moved = len(rows)

        for row in legacy.query_all("SELECT * FROM tenants WHERE id = ?", (tenant_id,)):
            data = dict(row)
            cols = ",".join(data)
            marks = ",".join("?" for _ in data)
            stores.control.execute(
                f"INSERT OR REPLACE INTO tenants ({cols}) VALUES ({marks})",
                tuple(data.values()))
        return moved
    finally:
        legacy.close()
```

Register the subcommand alongside the others:

```python
    p_adopt = sub.add_parser("adopt-db",
                             help="move a legacy single-tenant database into tenants/")
    p_adopt.add_argument("--source", required=True)
    p_adopt.add_argument("--tenant", required=True)
    p_adopt.set_defaults(func=_cmd_adopt_db)
```

```python
def _cmd_adopt_db(args) -> int:
    from .config import Settings
    from .stores import TenantStoreProvider
    settings = Settings.from_env()
    stores = TenantStoreProvider(
        control_db_path=settings.resolve_control_db_path(),
        tenants_root=settings.resolve_tenants_root())
    try:
        moved = adopt_db(args.source, args.tenant, stores)
        print(f"adopted {moved} knowledge node(s) into "
              f"{stores.tenant_db_path(args.tenant)}")
    finally:
        stores.close_all()
    return 0
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_tenant_stores.py -v`
Expected: 30 passed.

- [ ] **Step 6: Adopt the real DTDL database**

Back it up first, then adopt:

```bash
cp tenants/DTDL/platform.db tenants/DTDL/platform.db.pre-adopt
.venv/bin/python -m analytics_platform adopt-db --source tenants/DTDL/platform.db --tenant tnt_d23cd823d4c6
```

Expected: `adopted 1245 knowledge node(s) into .../tenants/tnt_d23cd823d4c6/tenant.db`.

Verify the counts match before removing anything:

```bash
.venv/bin/python -c "
import sqlite3
for p in ['tenants/DTDL/platform.db', 'tenants/tnt_d23cd823d4c6/tenant.db']:
    c = sqlite3.connect(p)
    print(p, c.execute('SELECT COUNT(*) FROM knowledge_nodes').fetchone()[0])
"
```

Both must print 1245. Leave `platform.db.pre-adopt` in place until you have used the new layout for a while.

- [ ] **Step 7: Retire the disposable dev database**

`data/platform.db` holds four co-mingled dev tenants and is scratch. Confirm nothing you care about is in it, then remove it and its sidecars:

```bash
git status --porcelain data/
rm -f data/platform.db data/platform.db-wal data/platform.db-shm
rm -f data/api_smoke.db-wal data/api_smoke.db-shm
```

- [ ] **Step 8: Run the full suite and commit**

Run: `.venv/bin/python -m pytest tests/ -q`

```bash
git add analytics_platform/ tests/
git commit -m "feat(db): adopt-db for legacy files; retire the shared-schema alias

adopt-db refuses a source holding more than one tenant — splitting co-mingled
companies is a data-ownership decision, not a CLI's to guess."
```

---

## Verification

- [ ] `.venv/bin/python -m pytest tests/ -q` — all green
- [ ] Two tenants created through one `make_context` write to two different files
- [ ] Opening tenant A's database as tenant B raises `TenantIsolationError`
- [ ] `tenants/tnt_d23cd823d4c6/tenant.db` holds all 1245 nodes
- [ ] `grep -rn "resolve_db_path" analytics_platform/` — only `cli.py`'s `adopt-db` and the deprecated definition
- [ ] `grep -rn "Store(" analytics_platform/ | grep -v stores.py` — no service constructs a Store directly

## What this changes for the other plans

- **Brain retrieval** — `knowledge_fts` and `knowledge_vectors` go in `TENANT_SCHEMA`, and `BrainIndex` takes the tenant's store. The `tenant_id` columns and filters stay as defence-in-depth behind the file boundary.
- **Governance** — unchanged; the review endpoint and approval gates are orthogonal to storage.
- **Skills portability** — `tenants/<id>/skill_bindings.json` now sits beside `tenants/<id>/tenant.db`, which is a tidier story than it was.
- **Frameworks & confidence** — unchanged.
