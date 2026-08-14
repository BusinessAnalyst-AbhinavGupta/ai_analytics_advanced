# Brain Retrieval Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken ChromaDB retrieval path with a SQLite-native hybrid search (BM25 + dense vectors, fused by Reciprocal Rank Fusion) so the Company Brain actually returns relevant nodes for natural-language questions.

**Architecture:** SQLite remains the single source of truth, and **each tenant has its own database file** (see the prerequisite below). Two new tables live beside `knowledge_nodes` in that same tenant database and the same transaction: an FTS5 virtual table for lexical recall, and a `knowledge_vectors` BLOB table for dense recall. Retrieval runs a hard SQL pre-filter (status + kind, inside the tenant's own file) to produce a candidate id set, runs both recall legs restricted to those ids, fuses the two rankings with RRF, and re-ranks by stored confidence. ChromaDB is removed entirely.

**Tech Stack:** Python 3.14, stdlib `sqlite3` (FTS5 already compiled in, verified), `numpy` 2.5.1, `sentence-transformers` 5.7.0, `unittest` + `pytest` runner.

## Prerequisite

**This plan depends on [Tenant Store Isolation](2026-08-13-tenant-store-isolation.md) — landed 2026-08-13 (PR #6, merged).** Every tenant now has its own SQLite file and a `TenantStoreProvider` resolves it. This plan's two tables live in `TENANT_SCHEMA`, and its index objects are constructed against a tenant's own store.

**Also blocking Task 1 of this plan: [PR #3](https://github.com/BusinessAnalyst-AbhinavGupta/ai_analytics_advanced/pull/3) is open and conflicts with the merged tenant-isolation work — do not merge it as-is.** See "PR #3" under Follow-on plans below for what to salvage from it before starting Task 1.

## Global Constraints

- **No new dependencies.** Every library this plan uses is already installed. `chromadb` is *removed* from `requirements-advanced.txt`.
- **Core, not tenant.** All code lands in `analytics_platform/` — nothing tenant-specific. Per `AGENTS.md` Part 1 §2.
- **Tenant isolation is the database file.** `knowledge_fts` and `knowledge_vectors` live in the tenant's own database, so a query physically cannot reach another company's rows. The `tenant_id` columns and filters this plan keeps are **defence-in-depth behind that boundary** — a second check, never the primary one. No isolation may ever depend on a secondary index's metadata filter.
- **No silent failures.** Every `except` in code this plan touches must log at WARNING or higher via `logging.getLogger(__name__)`. A bare `except: pass` is a review rejection. This is the defect class that hid every bug in the current implementation.
- **Embedding model is configuration, not a literal.** Per `AGENTS.md` Part 1 §1 ("LLM Configurability").
- **Degrade loudly, never silently.** If embeddings are unavailable, retrieval falls back to lexical-only *and says so in the logs*.
- **Existing tests must keep passing.** Run `.venv/bin/python -m pytest tests/ -q` before every commit.
- Run all commands from the repo root with `.venv/bin/python`.

---

## File Structure

**Created:**
- `analytics_platform/brain/text.py` — FTS5 query sanitisation. Pure functions, no I/O.
- `analytics_platform/brain/embedding.py` — embedding provider protocol, real + null implementations, cached factory.
- `analytics_platform/brain/fusion.py` — RRF fusion and confidence re-ranking. Pure functions, no I/O.
- `analytics_platform/brain/index.py` — `BrainIndex`: owns both recall legs and the write path into them.
- `tests/test_brain_text.py`, `tests/test_brain_embedding.py`, `tests/test_brain_fusion.py`, `tests/test_brain_index.py`, `tests/test_brain_retrieval.py`

**Modified:**
- `analytics_platform/database.py` — two new tables in `SCHEMA`, backfill in `_migrate`.
- `analytics_platform/config.py` — embedding settings + `resolve_vector_path` removal.
- `analytics_platform/brain/store.py` — `_sync_vector` → `_sync_index`; `search()` rewritten.
- `analytics_platform/stakeholder.py:60` — accept and pass an index.
- `analytics_platform/api.py:280-297` — build one `BrainIndex`, inject everywhere.
- `analytics_platform/cli.py` — `reindex` command.
- `requirements-advanced.txt` — drop `chromadb`.

**Deleted:**
- `analytics_platform/brain/vector_store.py`
- `tests/test_vector_search.py` (replaced by `tests/test_brain_index.py`)

---

### Task 1: Schema for the two recall legs

**Why:** Both recall legs need somewhere to live inside the source-of-truth database. Putting them there — rather than in a separate store — is what removes the dual-write drift problem: an index write and a node write happen against the same connection, so they cannot diverge unnoticed. They belong to `TENANT_SCHEMA` because a company's search index is that company's data. Existing databases must pick the tables up without being recreated, which is what `_migrate` is for.

**Files:**
- Modify: `analytics_platform/database.py` (add to `TENANT_SCHEMA`, beside `knowledge_nodes`), `analytics_platform/database.py` `_migrate`
- Test: `tests/test_brain_index.py`

**Interfaces:**
- Consumes: nothing.
- Produces: tables `knowledge_fts` (columns `node_id`, `tenant_id`, `title`, `summary`) and `knowledge_vectors` (columns `node_id TEXT PRIMARY KEY`, `tenant_id TEXT`, `model TEXT`, `dim INTEGER`, `vector BLOB`, `updated_at TEXT`). Task 4 and Task 5 write to these.

- [ ] **Step 1: Write the failing test**

Create `tests/test_brain_index.py`:

```python
"""Hybrid retrieval index: schema, lexical leg, vector leg, tenant isolation."""
from __future__ import annotations

import unittest

from tests.helpers import make_ctx


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()

    def tearDown(self):
        self.ctx.close()

    def _tables(self):
        rows = self.ctx.store.query_all(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")
        return {r["name"] for r in rows}

    def test_fts_table_exists(self):
        self.assertIn("knowledge_fts", self._tables())

    def test_vectors_table_exists(self):
        self.assertIn("knowledge_vectors", self._tables())

    def test_vectors_table_columns(self):
        rows = self.ctx.store.query_all("PRAGMA table_info(knowledge_vectors)")
        self.assertEqual(
            {r["name"] for r in rows},
            {"node_id", "tenant_id", "model", "dim", "vector", "updated_at"})

    def test_fts_accepts_match_query(self):
        self.ctx.store.execute(
            "INSERT INTO knowledge_fts (node_id, tenant_id, title, summary) VALUES (?,?,?,?)",
            ("kn_1", "t1", "Checkout conversion", "Share of sessions reaching payment"))
        rows = self.ctx.store.query_all(
            "SELECT node_id FROM knowledge_fts WHERE knowledge_fts MATCH ? AND tenant_id = ?",
            ('"conversion"', "t1"))
        self.assertEqual([r["node_id"] for r in rows], ["kn_1"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_brain_index.py -v`
Expected: FAIL — `test_fts_table_exists` and `test_vectors_table_exists` assert missing names; `test_fts_accepts_match_query` raises `sqlite3.OperationalError: no such table: knowledge_fts`.

- [ ] **Step 3: Add the tables to SCHEMA**

In `analytics_platform/database.py`, immediately after the `knowledge_nodes` table definition inside the **`TENANT_SCHEMA`** string, add:

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    node_id UNINDEXED,
    tenant_id UNINDEXED,
    title,
    summary,
    tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS knowledge_vectors (
    node_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    updated_at TEXT
);
```

And in the index block at the end of `TENANT_SCHEMA`, add:

```sql
CREATE INDEX IF NOT EXISTS idx_kv_tenant ON knowledge_vectors(tenant_id);
```

Both tables carry `tenant_id` even though they live in a single-tenant database. That is deliberate defence-in-depth: the file boundary is the isolation guarantee, and the column is a second check that costs one indexed comparison.

- [ ] **Step 4: Make the migration non-destructive for existing databases**

`CREATE TABLE IF NOT EXISTS` in `TENANT_SCHEMA` already covers existing databases, because `init_db` runs `executescript(schema)` on every open. Add an explicit guard to `_migrate` in `analytics_platform/database.py` so a tenant database created before FTS5 was available fails loudly rather than silently lacking search. Insert this inside `_migrate`, before the final `conn.commit()`:

```python
        # Brain retrieval: both recall legs must exist or search silently degrades.
        # Only tenant databases carry them — a control store legitimately has neither,
        # so key the check off knowledge_nodes rather than asserting unconditionally.
        is_tenant_db = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='knowledge_nodes'").fetchone())
        if is_tenant_db:
            have = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('knowledge_fts','knowledge_vectors')").fetchall()}
            missing = {"knowledge_fts", "knowledge_vectors"} - have
            if missing:
                raise RuntimeError(
                    f"Brain retrieval tables missing after schema init: "
                    f"{sorted(missing)}. SQLite may lack FTS5 support.")
```

Note the existing `except Exception: pass` at the end of `_migrate` would swallow this. Change that handler to re-raise `RuntimeError` and log everything else:

```python
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - column migrations must not block startup
        logger.warning("schema migration step failed: %s", exc, exc_info=True)
```

Add at the top of `analytics_platform/database.py`, after the imports:

```python
import logging

logger = logging.getLogger(__name__)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_brain_index.py -v`
Expected: 4 passed.

- [ ] **Step 6: Run the full suite for regressions**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: no new failures versus the pre-change baseline. Record the baseline first with `git stash && .venv/bin/python -m pytest tests/ -q; git stash pop` if you did not capture it.

- [ ] **Step 7: Commit**

```bash
git add analytics_platform/database.py tests/test_brain_index.py
git commit -m "feat(brain): add FTS5 and vector tables to the knowledge schema"
```

---

### Task 2: FTS5 query sanitisation

**Why:** This is the direct fix for the defect that makes the Brain look empty. Today the raw user question is interpolated into `title LIKE '%...%'`, which matches only if a node title literally contains the whole sentence. FTS5 cannot take a raw question either — characters like `?`, `-`, `:` and the bare words `AND`/`OR`/`NOT` are query syntax and raise `sqlite3.OperationalError`. Every question must be reduced to a quoted OR-set of content tokens before it reaches the index.

**Files:**
- Create: `analytics_platform/brain/text.py`
- Test: `tests/test_brain_text.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `to_fts_query(text: str) -> str` — returns an FTS5 MATCH expression, or `""` when the input has no usable tokens. Task 4 calls this.

- [ ] **Step 1: Write the failing test**

Create `tests/test_brain_text.py`:

```python
"""FTS5 query sanitisation: user questions become safe MATCH expressions."""
from __future__ import annotations

import unittest

from analytics_platform.brain.text import to_fts_query


class ToFtsQueryTest(unittest.TestCase):
    def test_tokens_are_quoted_and_ored(self):
        self.assertEqual(to_fts_query("checkout conversion"),
                         '"checkout" OR "conversion"')

    def test_stopwords_are_dropped(self):
        self.assertEqual(to_fts_query("what is the conversion"), '"conversion"')

    def test_single_character_tokens_are_dropped(self):
        self.assertEqual(to_fts_query("a b conversion"), '"conversion"')

    def test_punctuation_is_stripped(self):
        self.assertEqual(to_fts_query("why did conversion drop?"),
                         '"conversion" OR "drop"')

    def test_fts_operators_are_neutralised(self):
        # Bare AND/OR/NOT would be parsed as syntax; quoting makes them literals,
        # and they are stopwords so they drop out entirely.
        self.assertEqual(to_fts_query("revenue AND cost"), '"revenue" OR "cost"')

    def test_quotes_and_hyphens_do_not_leak(self):
        out = to_fts_query('the "user-churn" rate')
        self.assertEqual(out, '"user" OR "churn" OR "rate"')

    def test_empty_input_returns_empty(self):
        self.assertEqual(to_fts_query(""), "")

    def test_only_stopwords_returns_empty(self):
        self.assertEqual(to_fts_query("what is the"), "")

    def test_numbers_survive(self):
        self.assertEqual(to_fts_query("q3 2026 revenue"),
                         '"q3" OR "2026" OR "revenue"')

    def test_duplicate_tokens_collapse(self):
        self.assertEqual(to_fts_query("revenue revenue"), '"revenue"')


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_brain_text.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'analytics_platform.brain.text'`.

- [ ] **Step 3: Write the implementation**

Create `analytics_platform/brain/text.py`:

```python
"""Text preparation for the Brain's lexical recall leg.

FTS5 MATCH takes a query *expression*, not free text: `?`, `-`, `:`, `"` and the
bare words AND/OR/NOT are syntax and raise OperationalError. A user question is
therefore reduced to its content tokens, each quoted so it is treated as a
literal, joined with OR so any single overlap is a hit. Ranking is bm25()'s job,
not the query's.
"""
from __future__ import annotations

import re
from typing import List

# Deliberately small. An aggressive stoplist hurts a knowledge base full of short
# titles more than it helps; these are only the words that appear in nearly every
# analytics question and so carry no discriminating signal.
STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "in", "on", "at", "to", "for", "with", "by", "from", "as",
    "and", "or", "not", "but", "if", "then", "than", "that", "this", "these",
    "those", "it", "its", "we", "our", "you", "your", "they", "their",
    "what", "which", "who", "whom", "when", "where", "why", "how",
    "do", "does", "did", "doing", "done", "can", "could", "should", "would",
    "have", "has", "had", "will", "shall", "may", "might", "must",
    "me", "my", "us", "i", "s", "t",
})

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> List[str]:
    """Content tokens, lowercased, order-preserving, de-duplicated."""
    seen = set()
    out: List[str] = []
    for raw in _TOKEN_RE.findall(text or ""):
        tok = raw.lower()
        if len(tok) < 2 or tok in STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def to_fts_query(text: str) -> str:
    """An FTS5 MATCH expression for `text`, or "" when nothing usable remains.

    Callers MUST treat "" as "skip the lexical leg" — passing it to MATCH is an
    OperationalError, not an empty result set.
    """
    tokens = tokenize(text)
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_brain_text.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/brain/text.py tests/test_brain_text.py
git commit -m "feat(brain): sanitise user questions into FTS5 match expressions"
```

---

### Task 3: Configurable embedding provider with a loud null fallback

**Why:** The current model is a 1.3 GB literal inside `vector_store.py` with no config knob, which violates the configurability rule and makes cold start painful for every tenant. It also has no fallback: if the model cannot load, the whole vector leg disappears without a word. This task makes the model a setting, defaults it to the small variant (ample for a few thousand curated nodes), applies the query-side instruction prefix BGE retrieval models expect, and makes unavailability an explicit, logged, queryable state.

**Files:**
- Create: `analytics_platform/brain/embedding.py`
- Modify: `analytics_platform/config.py:24-60` (add settings), `analytics_platform/config.py:62-65` (delete `resolve_vector_path`)
- Test: `tests/test_brain_embedding.py`

**Interfaces:**
- Consumes: `Settings` from `analytics_platform.config`.
- Produces:
  - `class Embedder(Protocol)` with `available: bool`, `model_name: str`, `dim: int`, `encode_documents(texts: List[str]) -> Optional[np.ndarray]`, `encode_query(text: str) -> Optional[np.ndarray]`
  - `class NullEmbedder` — `available == False`, both encode methods return `None`
  - `class SentenceTransformerEmbedder(model_name: str, query_prefix: str)`
  - `get_embedder(settings: Settings) -> Embedder` — cached per `(model_name, enabled)`
  - New `Settings` fields: `embedding_enabled: bool = True`, `embedding_model: str = "BAAI/bge-small-en-v1.5"`, `embedding_query_prefix: str = "Represent this sentence for searching relevant passages: "`

  Tasks 5 and 9 consume these. Vectors returned are **L2-normalised float32**, so cosine similarity is a plain dot product.

- [ ] **Step 1: Write the failing test**

Create `tests/test_brain_embedding.py`:

```python
"""Embedding provider: configurable model, normalised output, loud null fallback."""
from __future__ import annotations

import logging
import unittest

import numpy as np

from analytics_platform.brain.embedding import (NullEmbedder,
                                                SentenceTransformerEmbedder,
                                                get_embedder)
from analytics_platform.config import Settings


class NullEmbedderTest(unittest.TestCase):
    def test_reports_unavailable(self):
        self.assertFalse(NullEmbedder("disabled by config").available)

    def test_encode_returns_none(self):
        emb = NullEmbedder("disabled by config")
        self.assertIsNone(emb.encode_documents(["anything"]))
        self.assertIsNone(emb.encode_query("anything"))

    def test_dim_is_zero(self):
        self.assertEqual(NullEmbedder("disabled by config").dim, 0)


class FactoryTest(unittest.TestCase):
    def test_disabled_setting_yields_null_embedder(self):
        emb = get_embedder(Settings(embedding_enabled=False))
        self.assertIsInstance(emb, NullEmbedder)
        self.assertFalse(emb.available)

    def test_unloadable_model_degrades_to_null_and_logs(self):
        settings = Settings(embedding_model="definitely/not-a-real-model-xyz")
        with self.assertLogs("analytics_platform.brain.embedding", level=logging.WARNING) as cap:
            emb = get_embedder(settings)
        self.assertFalse(emb.available)
        self.assertTrue(any("definitely/not-a-real-model-xyz" in m for m in cap.output))

    def test_repeated_calls_are_cached(self):
        s = Settings(embedding_enabled=False)
        self.assertIs(get_embedder(s), get_embedder(s))


class SentenceTransformerEmbedderTest(unittest.TestCase):
    """Loads a real model. Skipped when the model is not cached locally."""

    @classmethod
    def setUpClass(cls):
        cls.emb = SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5")
        if not cls.emb.available:
            raise unittest.SkipTest("bge-small-en-v1.5 not available offline")

    def test_document_vectors_are_normalised_float32(self):
        vecs = self.emb.encode_documents(["checkout conversion rate"])
        self.assertEqual(vecs.dtype, np.float32)
        self.assertAlmostEqual(float(np.linalg.norm(vecs[0])), 1.0, places=4)

    def test_query_vector_shape_matches_dim(self):
        vec = self.emb.encode_query("how many people converted")
        self.assertEqual(vec.shape, (self.emb.dim,))

    def test_semantics_beat_keywords(self):
        # The unrelated doc must be topically unambiguous. An earlier draft used a
        # "server latency" doc, which scored within 0.0015 of the correct answer —
        # "regression" apparently reads close to "churn regression model" to this
        # model, a near coin-flip margin, not a robust semantic-match assertion.
        docs = self.emb.encode_documents([
            "High user churn observed in Q3 for the European market.",
            "The design team shipped a refreshed color palette for the mobile app icon.",
        ])
        q = self.emb.encode_query("customer attrition")
        sims = docs @ q
        self.assertGreater(float(sims[0]), float(sims[1]))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_brain_embedding.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'analytics_platform.brain.embedding'`.

- [ ] **Step 3: Add the settings**

In `analytics_platform/config.py`, inside the `Settings` dataclass, add after the `source_dialect` line:

```python
    # Brain retrieval -------------------------------------------------------
    embedding_enabled: bool = True      # False -> lexical-only retrieval (logged)
    embedding_model: str = "BAAI/bge-small-en-v1.5"   # ~130MB; large variant is 1.3GB
    embedding_query_prefix: str = (      # BGE retrieval models expect this on queries only
        "Represent this sentence for searching relevant passages: ")
```

Delete `resolve_vector_path` (lines 62-65) — nothing will reference it after Task 9:

```python
    def resolve_vector_path(self) -> str:
        if self.data_dir:
            return os.path.join(self.data_dir, ".chroma_db")
        return ".chroma_db"
```

- [ ] **Step 4: Write the implementation**

Create `analytics_platform/brain/embedding.py`:

```python
"""Embedding providers for the Brain's dense recall leg.

Two rules shape this module:

* The model is configuration, never a literal — every tenant deployment can pick
  its own, and the default is the small BGE variant because a curated Brain is
  thousands of nodes, not millions.
* Unavailability is an explicit, logged state. Retrieval must be able to ask
  `embedder.available` and fall back to lexical-only *knowing* that it did, rather
  than silently returning nothing.

All vectors are L2-normalised float32, so cosine similarity is a dot product.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Protocol, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    available: bool
    model_name: str
    dim: int

    def encode_documents(self, texts: List[str]) -> Optional[np.ndarray]: ...
    def encode_query(self, text: str) -> Optional[np.ndarray]: ...


class NullEmbedder:
    """Stands in when embeddings are off or the model will not load."""

    available = False
    dim = 0

    def __init__(self, reason: str):
        self.reason = reason
        self.model_name = ""

    def encode_documents(self, texts: List[str]) -> Optional[np.ndarray]:
        return None

    def encode_query(self, text: str) -> Optional[np.ndarray]:
        return None


class SentenceTransformerEmbedder:
    """sentence-transformers backend. Never raises on load — sets `available`."""

    def __init__(self, model_name: str, query_prefix: str = ""):
        self.model_name = model_name
        self.query_prefix = query_prefix
        self.available = False
        self.dim = 0
        self._model = None
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(model_name)
            self.dim = int(self._model.get_sentence_embedding_dimension())
            self.available = True
        except Exception as exc:  # noqa: BLE001 - load failure must not crash startup
            logger.warning(
                "embedding model %r unavailable, Brain retrieval will be "
                "lexical-only: %s", model_name, exc)

    def _encode(self, texts: List[str]) -> Optional[np.ndarray]:
        if not self.available:
            return None
        try:
            vecs = self._model.encode(texts, normalize_embeddings=True,
                                      convert_to_numpy=True,
                                      show_progress_bar=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("embedding failed for %d text(s): %s", len(texts), exc)
            return None
        return np.asarray(vecs, dtype=np.float32)

    def encode_documents(self, texts: List[str]) -> Optional[np.ndarray]:
        if not texts:
            return None
        return self._encode(list(texts))

    def encode_query(self, text: str) -> Optional[np.ndarray]:
        vecs = self._encode([f"{self.query_prefix}{text}"])
        return None if vecs is None else vecs[0]


_CACHE: Dict[Tuple[str, bool, str], Embedder] = {}


def get_embedder(settings) -> Embedder:
    """Cached embedder for these settings. Loading a model is seconds, not ms."""
    key = (settings.embedding_model, settings.embedding_enabled,
           settings.embedding_query_prefix)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    if not settings.embedding_enabled:
        logger.info("embeddings disabled by config; Brain retrieval is lexical-only")
        emb: Embedder = NullEmbedder("disabled by config")
    else:
        candidate = SentenceTransformerEmbedder(
            settings.embedding_model, settings.embedding_query_prefix)
        emb = candidate if candidate.available else NullEmbedder(
            f"model {settings.embedding_model!r} failed to load")

    _CACHE[key] = emb
    return emb


def reset_embedder_cache() -> None:
    """Test seam — drops cached models so settings changes take effect."""
    _CACHE.clear()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_brain_embedding.py -v`
Expected: `NullEmbedderTest` and `FactoryTest` pass (6 tests). `SentenceTransformerEmbedderTest` passes if the model is cached locally, otherwise skips. If it skips, download it once with:

```bash
.venv/bin/python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"
```

then re-run and confirm all 9 pass.

- [ ] **Step 6: Commit**

```bash
git add analytics_platform/brain/embedding.py analytics_platform/config.py tests/test_brain_embedding.py
git commit -m "feat(brain): configurable embedder with explicit null fallback"
```

---

### Task 4: BrainIndex — write path and lexical recall

**Why:** This is the lexical leg. BM25 over an FTS5 index is what catches the internal vocabulary a company brain is full of — metric names, product codes, city names, acronyms like `BC2D` — which dense embeddings handle poorly. It is also the leg that always works, with no model to load, so it is the floor under retrieval quality.

**Files:**
- Create: `analytics_platform/brain/index.py`
- Test: `tests/test_brain_index.py` (append to the file created in Task 1)

**Interfaces:**
- Consumes: `Store` (`analytics_platform.database`), `to_fts_query` (Task 2), `Embedder` + `get_embedder` (Task 3), the tables from Task 1.
- Produces:
  - `class BrainIndex(store: Store, embedder: Optional[Embedder] = None)`
  - `.upsert(node_id: str, tenant_id: str, title: str, summary: str) -> None`
  - `.delete(node_id: str) -> None`
  - `.lexical_search(query: str, tenant_id: str, candidate_ids: Optional[List[str]], limit: int) -> List[str]` — node ids, best first
  - `.vector_search(...)` — added in Task 5
  - `.embedding_available: bool`

  Task 7 consumes all of these.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_brain_index.py` (keep the existing `SchemaTest`, and extend the imports at the top):

```python
from analytics_platform.brain.embedding import NullEmbedder
from analytics_platform.brain.index import BrainIndex


class LexicalSearchTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.index = BrainIndex(self.ctx.store, embedder=NullEmbedder("test"))
        self.index.upsert("kn_1", "t1", "Checkout conversion rate",
                          "Share of sessions that reach the payment page")
        self.index.upsert("kn_2", "t1", "BC2D attach rate",
                          "Bundled care attach on disbursed loans")
        self.index.upsert("kn_3", "t2", "Checkout conversion rate",
                          "Other tenant, same title")

    def tearDown(self):
        self.ctx.close()

    def test_finds_node_by_content_word(self):
        self.assertEqual(
            self.index.lexical_search("conversion", "t1", None, 10), ["kn_1"])

    def test_finds_node_from_a_full_natural_language_question(self):
        # The defect this replaces: LIKE '%<whole question>%' matched nothing.
        hits = self.index.lexical_search(
            "why did our checkout conversion drop last week?", "t1", None, 10)
        self.assertIn("kn_1", hits)

    def test_matches_internal_acronyms(self):
        self.assertEqual(self.index.lexical_search("BC2D", "t1", None, 10), ["kn_2"])

    def test_other_tenants_are_never_returned(self):
        self.assertNotIn("kn_3", self.index.lexical_search("conversion", "t1", None, 10))

    def test_candidate_ids_restrict_results(self):
        self.assertEqual(
            self.index.lexical_search("conversion", "t1", ["kn_2"], 10), [])

    def test_empty_candidate_list_returns_nothing(self):
        self.assertEqual(self.index.lexical_search("conversion", "t1", [], 10), [])

    def test_unmatchable_query_returns_empty_not_error(self):
        self.assertEqual(self.index.lexical_search("what is the", "t1", None, 10), [])

    def test_punctuation_heavy_query_does_not_raise(self):
        self.assertEqual(self.index.lexical_search('"; DROP TABLE --', "t1", None, 10), [])

    def test_upsert_replaces_rather_than_duplicates(self):
        self.index.upsert("kn_1", "t1", "Checkout conversion rate", "Updated summary")
        rows = self.ctx.store.query_all(
            "SELECT node_id FROM knowledge_fts WHERE node_id = ?", ("kn_1",))
        self.assertEqual(len(rows), 1)

    def test_delete_removes_from_index(self):
        self.index.delete("kn_1")
        self.assertEqual(self.index.lexical_search("conversion", "t1", None, 10), [])

    def test_ranking_puts_the_better_match_first(self):
        self.index.upsert("kn_4", "t1", "Conversion", "conversion conversion conversion")
        hits = self.index.lexical_search("conversion", "t1", None, 10)
        self.assertEqual(hits[0], "kn_4")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_brain_index.py -v`
Expected: `SchemaTest` still passes; every `LexicalSearchTest` errors with `ModuleNotFoundError: No module named 'analytics_platform.brain.index'`.

- [ ] **Step 3: Write the implementation**

Create `analytics_platform/brain/index.py`:

```python
"""Hybrid retrieval index for the Company Brain.

Two recall legs over the same SQLite database that holds `knowledge_nodes`:

* lexical — FTS5 + bm25(), which carries internal vocabulary (metric names,
  product codes, acronyms) that dense vectors handle poorly;
* dense — normalised embeddings in a BLOB column, brute-forced with numpy,
  which carries paraphrase ("drop off" ~ "abandonment").

Both are always restricted by `tenant_id` in SQL. Isolation is a property of the
source of truth, never of an index's metadata filter.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..database import Store
from ..domain import now_iso
from .embedding import Embedder, NullEmbedder
from .text import to_fts_query

logger = logging.getLogger(__name__)

# SQLite's default limit is 999 host parameters per statement.
_MAX_SQL_PARAMS = 900


class BrainIndex:
    def __init__(self, store: Store, embedder: Optional[Embedder] = None):
        self.store = store
        self.embedder = embedder or NullEmbedder("no embedder supplied")

    @property
    def embedding_available(self) -> bool:
        return bool(getattr(self.embedder, "available", False))

    # -- write ---------------------------------------------------------------
    def upsert(self, node_id: str, tenant_id: str, title: str, summary: str) -> None:
        """Index one node into both legs. Replaces any previous entry."""
        self._upsert_lexical(node_id, tenant_id, title, summary)
        self._upsert_vector(node_id, tenant_id, title, summary)

    def _upsert_lexical(self, node_id: str, tenant_id: str, title: str,
                        summary: str) -> None:
        try:
            self.store.execute_many([
                ("DELETE FROM knowledge_fts WHERE node_id = ?", (node_id,)),
                ("INSERT INTO knowledge_fts (node_id, tenant_id, title, summary) "
                 "VALUES (?,?,?,?)", (node_id, tenant_id, title or "", summary or "")),
            ])
        except Exception as exc:  # noqa: BLE001 - indexing must not fail a write
            logger.warning("lexical index upsert failed for %s: %s", node_id, exc,
                           exc_info=True)

    def _upsert_vector(self, node_id: str, tenant_id: str, title: str,
                       summary: str) -> None:
        if not self.embedding_available:
            return
        # Only prose is embedded. SQL is structure, not language: embedding it
        # dilutes the vector and cannot answer table-level questions anyway.
        text = f"{title}\n{summary}".strip()
        if not text:
            return
        vecs = self.embedder.encode_documents([text])
        if vecs is None:
            return
        vec = np.asarray(vecs[0], dtype=np.float32)
        try:
            self.store.execute(
                "INSERT INTO knowledge_vectors (node_id, tenant_id, model, dim, vector, updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET "
                "tenant_id=excluded.tenant_id, model=excluded.model, dim=excluded.dim, "
                "vector=excluded.vector, updated_at=excluded.updated_at",
                (node_id, tenant_id, self.embedder.model_name, int(vec.shape[0]),
                 vec.tobytes(), now_iso()))
        except Exception as exc:  # noqa: BLE001
            logger.warning("vector index upsert failed for %s: %s", node_id, exc,
                           exc_info=True)

    def delete(self, node_id: str) -> None:
        try:
            self.store.execute_many([
                ("DELETE FROM knowledge_fts WHERE node_id = ?", (node_id,)),
                ("DELETE FROM knowledge_vectors WHERE node_id = ?", (node_id,)),
            ])
        except Exception as exc:  # noqa: BLE001
            logger.warning("index delete failed for %s: %s", node_id, exc, exc_info=True)

    # -- read ----------------------------------------------------------------
    def lexical_search(self, query: str, tenant_id: str,
                       candidate_ids: Optional[Sequence[str]] = None,
                       limit: int = 40) -> List[str]:
        """Node ids ranked by bm25, best first. [] when nothing is searchable.

        `ORDER BY ... LIMIT` runs inside SQL, before any Python code sees a row —
        unlike the vector leg (Task 5), which loads every row and can safely
        re-filter in Python afterwards. That means a candidate set larger than
        SQLite's ~900-parameter limit cannot simply drop the restriction: doing so
        would rank the whole tenant and return the global top-`limit`, which may
        share nothing with the candidate set the caller actually asked about. So
        this leg chunks instead — one MATCH query per <=900-id slice, merging by
        each node's best (most negative) bm25 score across chunks, then re-sorting
        and truncating once at the end.
        """
        if candidate_ids is not None and len(candidate_ids) == 0:
            return []
        match = to_fts_query(query)
        if not match:
            return []

        chunks: List[Optional[Sequence[str]]]
        if candidate_ids is None:
            chunks = [None]
        else:
            ids = list(candidate_ids)
            chunks = [ids[i:i + _MAX_SQL_PARAMS] for i in range(0, len(ids), _MAX_SQL_PARAMS)]

        best: Dict[str, float] = {}
        for chunk in chunks:
            for node_id, score in self._lexical_search_chunk(match, tenant_id, chunk, limit):
                if node_id not in best or score < best[node_id]:
                    best[node_id] = score

        # bm25() is more negative for better matches, so ascending is best-first.
        ordered = sorted(best, key=lambda n: best[n])
        return ordered[:limit]

    def _lexical_search_chunk(self, match: str, tenant_id: str,
                              candidate_ids: Optional[Sequence[str]],
                              limit: int) -> List[Tuple[str, float]]:
        """One MATCH query, restricted to at most _MAX_SQL_PARAMS candidate ids."""
        sql = ("SELECT node_id, bm25(knowledge_fts) AS score FROM knowledge_fts "
               "WHERE knowledge_fts MATCH ? AND tenant_id = ?")
        params: List[object] = [match, tenant_id]
        if candidate_ids is not None:
            sql += f" AND node_id IN ({','.join('?' for _ in candidate_ids)})"
            params.extend(candidate_ids)
        sql += " ORDER BY score ASC LIMIT ?"
        params.append(limit)

        try:
            rows = self.store.query_all(sql, tuple(params))
        except Exception as exc:  # noqa: BLE001
            logger.warning("lexical search failed for tenant %s: %s", tenant_id, exc,
                           exc_info=True)
            return []
        return [(r["node_id"], r["score"]) for r in rows]
```

`_restrict_clause` (used by Task 5's `_load_vectors`, not by `lexical_search` above) keeps its original "drop the restriction above `_MAX_SQL_PARAMS`" behavior — that is safe there specifically because `_load_vectors` re-filters every returned row against `candidate_ids` in Python (see its `wanted` check) regardless of whether SQL applied the restriction, and it never applies a `LIMIT` before that Python-side filter runs. The lexical leg has no such downstream filter, which is exactly why it needed the chunking fix instead.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_brain_index.py -v`
Expected: 15 passed (4 schema + 11 lexical).

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/brain/index.py tests/test_brain_index.py
git commit -m "feat(brain): BM25 lexical recall leg over FTS5"
```

---

### Task 5: BrainIndex — dense recall leg

**Why:** This is the leg that answers the paraphrase case — the one thing vectors genuinely win at, and the reason to keep them. At a few thousand nodes per tenant, a brute-force dot product over a normalised float32 matrix is well under a millisecond, so no ANN index is warranted and none of ChromaDB's operational cost is justified.

**Files:**
- Modify: `analytics_platform/brain/index.py` (add `vector_search`)
- Test: `tests/test_brain_index.py` (append)

**Interfaces:**
- Consumes: `knowledge_vectors` (Task 1), `Embedder.encode_query` (Task 3).
- Produces: `BrainIndex.vector_search(query: str, tenant_id: str, candidate_ids: Optional[Sequence[str]], limit: int) -> List[str]` — node ids by descending cosine similarity. Returns `[]` when embeddings are unavailable. Task 7 consumes it.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_brain_index.py`. Add `from analytics_platform.brain.embedding import SentenceTransformerEmbedder` to the imports:

```python
class VectorSearchTest(unittest.TestCase):
    """Replaces the ChromaDB test that used to live in tests/test_vector_search.py."""

    @classmethod
    def setUpClass(cls):
        cls.embedder = SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5")
        if not cls.embedder.available:
            raise unittest.SkipTest("bge-small-en-v1.5 not available offline")

    def setUp(self):
        self.ctx = make_ctx()
        self.index = BrainIndex(self.ctx.store, embedder=self.embedder)
        self.index.upsert("kn_churn", "t1", "Q3 European churn",
                          "High user churn observed in Q3 for the European market.")
        # The unrelated doc must be topically unambiguous once title+summary are
        # embedded together (what upsert() actually does). A "server latency" doc
        # titled "Latency regression" was tried first and lost — "regression" reads
        # close enough to "churn regression model" that it out-scored the genuinely
        # on-topic doc for the query "customer attrition" (0.650 vs 0.617). This
        # pairing has a wide, verified margin (~0.19) instead of a coin flip.
        self.index.upsert("kn_palette", "t1", "New color palette",
                          "The design team shipped a refreshed color palette for the mobile app icon.")
        self.index.upsert("kn_other", "t2", "Q3 European churn",
                          "High user churn observed in Q3 for the European market.")

    def tearDown(self):
        self.ctx.close()

    def test_matches_on_meaning_not_keywords(self):
        # "customer attrition" shares no token with "user churn".
        hits = self.index.vector_search("customer attrition", "t1", None, 5)
        self.assertEqual(hits[0], "kn_churn")

    def test_lexical_leg_cannot_do_this(self):
        # Demonstrates why both legs exist.
        self.assertEqual(self.index.lexical_search("customer attrition", "t1", None, 5), [])

    def test_other_tenants_are_never_returned(self):
        hits = self.index.vector_search("customer attrition", "t1", None, 5)
        self.assertNotIn("kn_other", hits)

    def test_candidate_ids_restrict_results(self):
        hits = self.index.vector_search("customer attrition", "t1", ["kn_palette"], 5)
        self.assertEqual(hits, ["kn_palette"])

    def test_empty_candidate_list_returns_nothing(self):
        self.assertEqual(self.index.vector_search("customer attrition", "t1", [], 5), [])

    def test_delete_removes_the_vector(self):
        self.index.delete("kn_churn")
        hits = self.index.vector_search("customer attrition", "t1", None, 5)
        self.assertNotIn("kn_churn", hits)


class VectorSearchDegradationTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.index = BrainIndex(self.ctx.store, embedder=NullEmbedder("test"))

    def tearDown(self):
        self.ctx.close()

    def test_returns_empty_when_embeddings_unavailable(self):
        self.index.upsert("kn_1", "t1", "Churn", "User churn in Q3")
        self.assertEqual(self.index.vector_search("attrition", "t1", None, 5), [])

    def test_no_vector_row_is_written_without_an_embedder(self):
        self.index.upsert("kn_1", "t1", "Churn", "User churn in Q3")
        rows = self.ctx.store.query_all("SELECT node_id FROM knowledge_vectors")
        self.assertEqual(rows, [])

    def test_embedding_available_is_false(self):
        self.assertFalse(self.index.embedding_available)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_brain_index.py -v`
Expected: FAIL with `AttributeError: 'BrainIndex' object has no attribute 'vector_search'`.

- [ ] **Step 3: Write the implementation**

Append to `analytics_platform/brain/index.py`, inside `BrainIndex`:

```python
    def vector_search(self, query: str, tenant_id: str,
                      candidate_ids: Optional[Sequence[str]] = None,
                      limit: int = 40) -> List[str]:
        """Node ids by descending cosine similarity. [] when embeddings are off.

        Brute force is deliberate: a curated Brain is thousands of nodes, and a
        dot product over a normalised float32 matrix of that size is sub-millisecond.
        An ANN index would add a second stateful store for no measurable gain.
        """
        if candidate_ids is not None and len(candidate_ids) == 0:
            return []
        if not self.embedding_available:
            return []
        qvec = self.embedder.encode_query(query)
        if qvec is None:
            return []

        rows = self._load_vectors(tenant_id, candidate_ids)
        if not rows:
            return []

        ids, matrix = rows
        sims = matrix @ np.asarray(qvec, dtype=np.float32)
        order = np.argsort(-sims)[:limit]
        return [ids[i] for i in order]

    def _load_vectors(self, tenant_id: str,
                      candidate_ids: Optional[Sequence[str]]
                      ) -> Optional[Tuple[List[str], np.ndarray]]:
        sql = ("SELECT node_id, dim, vector FROM knowledge_vectors "
               "WHERE tenant_id = ? AND model = ?")
        params: List[object] = [tenant_id, self.embedder.model_name]
        sql += self._restrict_clause(candidate_ids, params)

        try:
            rows = self.store.query_all(sql, tuple(params))
        except Exception as exc:  # noqa: BLE001
            logger.warning("vector load failed for tenant %s: %s", tenant_id, exc,
                           exc_info=True)
            return None
        if not rows:
            return None

        wanted = set(candidate_ids) if candidate_ids else None
        ids: List[str] = []
        vectors: List[np.ndarray] = []
        expected = self.embedder.dim
        for r in rows:
            if wanted is not None and r["node_id"] not in wanted:
                continue  # candidate set was too large to bind; filter here instead
            if int(r["dim"]) != expected:
                logger.warning("skipping %s: vector dim %s != model dim %s "
                               "(reindex required)", r["node_id"], r["dim"], expected)
                continue
            ids.append(r["node_id"])
            vectors.append(np.frombuffer(r["vector"], dtype=np.float32))

        if not ids:
            return None
        return ids, np.vstack(vectors)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_brain_index.py -v`
Expected: 24 passed (3 degradation tests always run; the 6 `VectorSearchTest` cases skip if the model is not cached).

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/brain/index.py tests/test_brain_index.py
git commit -m "feat(brain): dense recall leg with brute-force cosine over SQLite blobs"
```

---

### Task 6: RRF fusion and confidence re-ranking

**Why:** Two rankings with incomparable score scales (bm25 is unbounded and negative; cosine is 0–1) cannot be combined by adding scores. Reciprocal Rank Fusion combines them by *position* instead, which needs no normalisation and no per-corpus tuning. Re-ranking afterwards is where the confidence dimensions the Brain already stores finally earn their keep: a reviewed, fresh node should outrank a stale one at the same relevance.

**Files:**
- Create: `analytics_platform/brain/fusion.py`
- Test: `tests/test_brain_fusion.py`

**Interfaces:**
- Consumes: nothing (pure functions over primitives).
- Produces:
  - `rrf_fuse(rankings: Sequence[Sequence[str]], k: int = 60) -> Dict[str, float]`
  - `confidence_boost(confidence: Dict[str, float], weight: float = 0.3) -> float`
  - `rank_nodes(fused: Dict[str, float], confidence_by_id: Dict[str, Dict[str, float]], weight: float = 0.3) -> List[str]`

  Task 7 consumes `rrf_fuse` and `rank_nodes`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_brain_fusion.py`:

```python
"""Reciprocal Rank Fusion and confidence re-ranking."""
from __future__ import annotations

import unittest

from analytics_platform.brain.fusion import (confidence_boost, rank_nodes,
                                             rrf_fuse)


class RrfFuseTest(unittest.TestCase):
    def test_single_ranking_preserves_order(self):
        fused = rrf_fuse([["a", "b", "c"]])
        self.assertEqual(sorted(fused, key=lambda i: -fused[i]), ["a", "b", "c"])

    def test_agreement_between_legs_wins(self):
        # "b" is 2nd in both legs; "a" and "c" are 1st in one and absent from the other.
        fused = rrf_fuse([["a", "b"], ["c", "b"]])
        self.assertEqual(max(fused, key=lambda i: fused[i]), "b")

    def test_score_matches_the_rrf_formula(self):
        fused = rrf_fuse([["a"]], k=60)
        self.assertAlmostEqual(fused["a"], 1.0 / 61.0)

    def test_ids_from_either_leg_are_included(self):
        self.assertEqual(set(rrf_fuse([["a"], ["b"]])), {"a", "b"})

    def test_empty_input_yields_empty(self):
        self.assertEqual(rrf_fuse([]), {})

    def test_empty_rankings_are_skipped(self):
        self.assertEqual(rrf_fuse([[], ["a"]]), {"a": 1.0 / 61.0})


class ConfidenceBoostTest(unittest.TestCase):
    def test_full_confidence_gives_the_maximum_boost(self):
        self.assertAlmostEqual(
            confidence_boost({"review": 1.0, "freshness": 1.0}, weight=0.3), 1.3)

    def test_zero_confidence_is_neutral_not_zero(self):
        self.assertAlmostEqual(
            confidence_boost({"review": 0.0, "freshness": 0.0}, weight=0.3), 1.0)

    def test_missing_dimensions_are_treated_as_zero(self):
        self.assertAlmostEqual(confidence_boost({}, weight=0.3), 1.0)

    def test_out_of_range_values_are_clamped(self):
        self.assertAlmostEqual(
            confidence_boost({"review": 5.0, "freshness": 5.0}, weight=0.3), 1.3)


class RankNodesTest(unittest.TestCase):
    def test_confidence_breaks_a_relevance_tie(self):
        fused = {"a": 0.5, "b": 0.5}
        conf = {"a": {"review": 0.0, "freshness": 0.0},
                "b": {"review": 1.0, "freshness": 1.0}}
        self.assertEqual(rank_nodes(fused, conf), ["b", "a"])

    def test_relevance_still_dominates_confidence(self):
        fused = {"a": 1.0, "b": 0.5}
        conf = {"a": {"review": 0.0, "freshness": 0.0},
                "b": {"review": 1.0, "freshness": 1.0}}
        self.assertEqual(rank_nodes(fused, conf), ["a", "b"])

    def test_nodes_without_confidence_still_rank(self):
        self.assertEqual(rank_nodes({"a": 1.0, "b": 0.5}, {}), ["a", "b"])

    def test_empty_input_yields_empty(self):
        self.assertEqual(rank_nodes({}, {}), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_brain_fusion.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'analytics_platform.brain.fusion'`.

- [ ] **Step 3: Write the implementation**

Create `analytics_platform/brain/fusion.py`:

```python
"""Combining the two recall legs into one ranking.

bm25 scores are unbounded and negative; cosine similarities are 0..1. They cannot
be added. Reciprocal Rank Fusion combines by *position* instead, which needs no
score normalisation and no per-corpus tuning — the standard, boring choice.

Confidence then acts as a tie-breaker, not a driver: a reviewed, fresh node should
win against a stale one of equal relevance, but should never outrank a clearly
better match.
"""
from __future__ import annotations

from typing import Dict, List, Sequence

# The conventional RRF constant. Larger k flattens the contribution of top ranks,
# which keeps a single confident leg from dominating the fusion.
DEFAULT_K = 60

# Dimensions that describe how much a node can be trusted *now*. `evidence`,
# `definition`, `reproducibility` and `source` describe how it was produced and
# are deliberately excluded from ranking.
_RANKING_DIMENSIONS = ("review", "freshness")


def rrf_fuse(rankings: Sequence[Sequence[str]], k: int = DEFAULT_K) -> Dict[str, float]:
    """Fuse ranked id lists into {node_id: score}. Higher is better."""
    scores: Dict[str, float] = {}
    for ranking in rankings:
        for position, node_id in enumerate(ranking, start=1):
            scores[node_id] = scores.get(node_id, 0.0) + 1.0 / (k + position)
    return scores


def confidence_boost(confidence: Dict[str, float], weight: float = 0.3) -> float:
    """Multiplier in [1.0, 1.0 + weight] from the ranking-relevant dimensions."""
    if not confidence:
        return 1.0
    values = [min(1.0, max(0.0, float(confidence.get(d, 0.0) or 0.0)))
              for d in _RANKING_DIMENSIONS]
    return 1.0 + weight * (sum(values) / len(values))


def rank_nodes(fused: Dict[str, float],
               confidence_by_id: Dict[str, Dict[str, float]],
               weight: float = 0.3) -> List[str]:
    """Final ordering: fused relevance scaled by a bounded confidence boost."""
    def score(node_id: str) -> float:
        return fused[node_id] * confidence_boost(
            confidence_by_id.get(node_id, {}), weight)

    # Secondary sort on id keeps the order deterministic for equal scores.
    return sorted(fused, key=lambda n: (-score(n), n))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_brain_fusion.py -v`
Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/brain/fusion.py tests/test_brain_fusion.py
git commit -m "feat(brain): RRF fusion with confidence-weighted reranking"
```

---

### Task 7: Rewrite CompanyBrain.search() on the hybrid index

**Why:** This is where the defects actually get fixed. The new `search()` computes the candidate id set in SQL first — so tenant, status and kind become invariants of the source of truth rather than filters on a secondary index — then runs both legs over that set, fuses, and re-ranks. The `LIKE '%<whole question>%'` path disappears, and so does the exception handler that was hiding the Chroma failure.

**Files:**
- Modify: `analytics_platform/brain/store.py:36-57` (constructor + `_sync_vector` → `_sync_index`), `analytics_platform/brain/store.py:162-202` (`search`)
- Test: `tests/test_brain_retrieval.py`

**Interfaces:**
- Consumes: `BrainIndex` (Tasks 4-5), `rrf_fuse` + `rank_nodes` (Task 6).
- Produces: `CompanyBrain(store, tenant_id, index: Optional[BrainIndex] = None)`. **The `vector_store=` keyword is removed** — Task 8 updates every call site. `search()` keeps its existing signature and return type so no caller changes shape.

- [ ] **Step 1: Write the failing test**

Create `tests/test_brain_retrieval.py`:

```python
"""End-to-end Brain retrieval: the behaviour that was broken before the rebuild."""
from __future__ import annotations

import unittest

from analytics_platform.brain.embedding import NullEmbedder
from analytics_platform.brain.index import BrainIndex
from analytics_platform.brain.store import CompanyBrain
from analytics_platform.domain import NodeKind, ReviewStatus
from tests.helpers import make_ctx


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.index = BrainIndex(self.ctx.store, embedder=NullEmbedder("test"))
        self.brain = CompanyBrain(self.ctx.store, "t1", index=self.index)
        self.other = CompanyBrain(self.ctx.store, "t2", index=self.index)

        self.approved = self._approved(
            self.brain, NodeKind.QUERY, "Checkout conversion rate",
            "Share of sessions that reach the payment page")
        self.definition = self._approved(
            self.brain, NodeKind.DEFINITION, "Conversion",
            "A session that reaches the payment page")
        self.candidate = self.brain.create(
            NodeKind.QUERY, "Checkout conversion by city",
            summary="Conversion split by city")   # stays CANDIDATE
        self.foreign = self._approved(
            self.other, NodeKind.QUERY, "Checkout conversion rate",
            "Another tenant's node")

    def tearDown(self):
        self.ctx.close()

    @staticmethod
    def _approved(brain, kind, title, summary):
        node = brain.create(kind, title, summary=summary)
        brain.submit(node.id, by="junior")
        return brain.approve(node.id, by="senior")

    def test_natural_language_question_finds_the_node(self):
        """The core regression: this returned [] before the rebuild."""
        hits = self.brain.search("why did our checkout conversion drop last week?",
                                 kind=NodeKind.QUERY)
        self.assertIn(self.approved.id, [n.id for n in hits])

    def test_unapproved_nodes_are_never_returned(self):
        hits = self.brain.search("checkout conversion by city", kind=NodeKind.QUERY)
        self.assertNotIn(self.candidate.id, [n.id for n in hits])

    def test_other_tenants_are_never_returned(self):
        hits = self.brain.search("checkout conversion rate", kind=NodeKind.QUERY)
        self.assertNotIn(self.foreign.id, [n.id for n in hits])

    def test_kind_filter_is_honoured(self):
        hits = self.brain.search("conversion", kind=NodeKind.DEFINITION)
        ids = [n.id for n in hits]
        self.assertIn(self.definition.id, ids)
        self.assertNotIn(self.approved.id, ids)

    def test_usable_only_false_includes_candidates(self):
        hits = self.brain.search("checkout conversion by city", kind=NodeKind.QUERY,
                                 usable_only=False)
        self.assertIn(self.candidate.id, [n.id for n in hits])

    def test_empty_query_returns_recent_nodes(self):
        hits = self.brain.search("", kind=NodeKind.QUERY)
        self.assertIn(self.approved.id, [n.id for n in hits])

    def test_unmatchable_query_returns_empty(self):
        self.assertEqual(self.brain.search("zzzz-nonexistent-token",
                                           kind=NodeKind.QUERY), [])

    def test_limit_is_respected(self):
        self.assertLessEqual(len(self.brain.search("conversion", limit=1)), 1)

    def test_results_are_knowledge_nodes(self):
        hits = self.brain.search("conversion", kind=NodeKind.QUERY)
        self.assertTrue(all(hasattr(n, "id") and hasattr(n, "title") for n in hits))

    def test_search_without_an_index_returns_nothing_for_a_real_query(self):
        """No index -> [] for a query, never unrelated nodes presented as matches.

        `self.approved` genuinely exists in this tenant's table and would match —
        proving this isn't just "empty database, nothing to find." An indexless
        brain that returned it (or any other recent node) here would be answering
        a real question with unrelated content, which is worse than the original
        bug this plan fixes (that one at least returned nothing).
        """
        bare = CompanyBrain(self.ctx.store, "t1")
        self.assertEqual(bare.search("checkout conversion rate", kind=NodeKind.QUERY), [])

    def test_search_without_an_index_and_no_query_still_browses_recent_nodes(self):
        """No query is a browsing request, not a relevance claim -- unaffected."""
        bare = CompanyBrain(self.ctx.store, "t1")
        hits = bare.search("", kind=NodeKind.QUERY)
        self.assertIn(self.approved.id, [n.id for n in hits])


class IndexSyncTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.index = BrainIndex(self.ctx.store, embedder=NullEmbedder("test"))
        self.brain = CompanyBrain(self.ctx.store, "t1", index=self.index)

    def tearDown(self):
        self.ctx.close()

    def test_create_indexes_the_node(self):
        node = self.brain.create(NodeKind.METRIC, "Gross margin",
                                 summary="Revenue minus cost of goods")
        rows = self.ctx.store.query_all(
            "SELECT node_id FROM knowledge_fts WHERE node_id = ?", (node.id,))
        self.assertEqual(len(rows), 1)

    def test_transition_reindexes_without_duplicating(self):
        node = self.brain.create(NodeKind.METRIC, "Gross margin", summary="x")
        self.brain.submit(node.id, by="junior")
        self.brain.approve(node.id, by="senior")
        rows = self.ctx.store.query_all(
            "SELECT node_id FROM knowledge_fts WHERE node_id = ?", (node.id,))
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_brain_retrieval.py -v`
Expected: FAIL — `CompanyBrain.__init__() got an unexpected keyword argument 'index'`.

- [ ] **Step 3: Replace the constructor and index sync**

In `analytics_platform/brain/store.py`, replace the imports block at lines 13-16:

```python
try:
    from .vector_store import BrainVectorStore
except ImportError:
    BrainVectorStore = None
```

with:

```python
from .fusion import rank_nodes, rrf_fuse
from .index import BrainIndex
```

Add logging after the other imports:

```python
import logging

logger = logging.getLogger(__name__)
```

Replace `__init__` and `_sync_vector` (lines 36-57) with:

```python
class CompanyBrain:
    def __init__(self, store: Store, tenant_id: str,
                 index: Optional[BrainIndex] = None):
        self.store = store
        self.tenant_id = tenant_id
        self.index = index

    def _sync_index(self, node: KnowledgeNode) -> None:
        """Keep both recall legs in step with the row. Same DB, same connection."""
        if self.index is None:
            return
        self.index.upsert(node.id, node.tenant_id, node.title, node.summary)
```

Then replace the three `self._sync_vector(node)` call sites — `add_node` (line 70), `transition` (line 115), `update_field` (line 153) — with `self._sync_index(node)`.

- [ ] **Step 4: Rewrite search()**

Replace `search` (lines 162-202) with:

```python
    # -- read ---------------------------------------------------------------
    _USABLE_STATUSES = (ReviewStatus.APPROVED.value,
                        ReviewStatus.APPROVED_WITH_CAVEATS.value)

    def _candidate_rows(self, kind: Optional[NodeKind], usable_only: bool,
                        cap: int) -> List[Any]:
        """The authorisation boundary: tenant + status + kind, decided in SQL."""
        sql = "SELECT * FROM knowledge_nodes WHERE tenant_id=?"
        params: List[Any] = [self.tenant_id]
        if usable_only:
            placeholders = ",".join("?" for _ in self._USABLE_STATUSES)
            sql += f" AND status IN ({placeholders})"
            params.extend(self._USABLE_STATUSES)
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind.value if hasattr(kind, "value") else kind)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(cap)
        rows = self.store.query_all(sql, tuple(params))
        if len(rows) == cap:
            # The pre-filter hit its own cap: some usable nodes older than the
            # cap-th are invisible to this call. A curated Brain is thousands of
            # nodes at most (see brain/index.py's brute-force design rationale),
            # so this should be rare — if it isn't, that's worth knowing.
            logger.warning("candidate pre-filter capped at %d rows for tenant %s "
                           "(kind=%s); older usable nodes are not searchable this call",
                           cap, self.tenant_id, kind)
        return rows

    def search(self, query: str = "", kind: Optional[NodeKind] = None,
               usable_only: bool = True, limit: int = 20) -> List[KnowledgeNode]:
        """Hybrid retrieval: SQL pre-filter, BM25 + dense recall, RRF, rerank.

        With no query, this is "most recently updated usable nodes" — a browsing
        mode, not a relevance claim. With a query, relevance is a real filter: both
        recall legs run over the pre-filtered candidate set and are fused by rank,
        and a node neither leg surfaces is not returned. This holds even with no
        index configured — returning recency-ordered nodes for a real question
        would be answering with content nobody asked about, presented as if it
        were a match. That is worse than returning nothing, which is what an
        unindexed brain does instead until Task 8 gives every consumer an index.
        """
        # Pre-filter wide enough that recall is not truncated before ranking.
        rows = self._candidate_rows(kind, usable_only, cap=max(limit * 25, 500))
        nodes = [self._row_to_node(r) for r in rows]
        if not query:
            return nodes[:limit]
        if not nodes:
            return []

        by_id = {n.id: n for n in nodes}
        candidate_ids = list(by_id)

        if self.index is None:
            logger.warning("search(%r) on tenant %s has no BrainIndex — returning no "
                           "results rather than unrelated recent nodes; this tenant's "
                           "brain needs an index (see Task 8)", query, self.tenant_id)
            return []

        recall = max(limit * 4, 40)
        lexical = self.index.lexical_search(query, self.tenant_id, candidate_ids, recall)
        dense = self.index.vector_search(query, self.tenant_id, candidate_ids, recall)

        if not lexical and not dense:
            if not self.index.embedding_available:
                logger.info("no lexical hits for %r on tenant %s and embeddings are "
                            "unavailable", query, self.tenant_id)
            return []

        fused = rrf_fuse([lexical, dense])
        confidence_by_id = {n.id: n.confidence for n in nodes}
        ordered = rank_nodes(fused, confidence_by_id)
        return [by_id[i] for i in ordered if i in by_id][:limit]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_brain_retrieval.py -v`
Expected: 13 passed.

- [ ] **Step 6: Confirm no caller still passes vector_store**

Run: `grep -rn "vector_store\|_sync_vector" --include="*.py" analytics_platform/ tests/`
Expected: hits only in `analytics_platform/brain/vector_store.py` and `tests/test_vector_search.py`, both deleted in Task 9. Any other hit is a call site to fix in Task 8.

- [ ] **Step 7: Commit**

```bash
git add analytics_platform/brain/store.py tests/test_brain_retrieval.py
git commit -m "feat(brain): hybrid search with SQL prefilter, replacing LIKE fallback"
```

---

### Task 8: Inject the index at every construction site

**Why:** A correct `search()` changes nothing if the object that runs it has no index — which is exactly the situation today, where eleven of twelve `CompanyBrain(...)` call sites omit the vector store and the Stakeholder Analyst is one of them. This task makes the index reach every consumer, and adds the regression test that would have caught the original defect.

**Baseline going into this task: 330 passed, 14 failed, 1 skipped.** Task 7's own review caught and fixed a Critical bug — an indexless `search()` was returning unrelated recent nodes as if they matched a real query, rather than honoring its own "relevance is a real filter" contract. The fix (returning `[]` for a query with no index) is correct, but it means every existing test exercising an indexless `CompanyBrain` through a real question now fails loudly instead of accidentally passing on a spurious match. That is the true, complete shape of what this task closes — not the 6 tests a narrower read of the diff would suggest. All 14 failures trace to the same root cause (a `CompanyBrain` built without an index) across two symptoms: 6 raise `TypeError: unexpected keyword argument 'vector_store'` (the one lingering call site at `api.py:296`), and 8 fail on the new, correct `[]`/WARNING behavior (`test_brain.py::test_search_filters_by_usable_status`; `test_stakeholder.py::test_approved_definition_falls_through`, `test_approved_query_chart_synthesis_token_accounting`, `test_feedback_and_quality`, `test_reuse_approved_query_with_citation`, `test_routes`; `test_pipeline_e2e.py::test_approved_query_reuse_runs_and_completes`, `test_telemetry_recorded`). Expected after this task: all 14 resolved, full suite green.

**Files:**
- Modify: `analytics_platform/api.py:280-300`, `analytics_platform/stakeholder.py:40-61`, `analytics_platform/junior.py:111`, `analytics_platform/junior.py:186`, `analytics_platform/junior_worker.py:307`, `analytics_platform/junior_worker.py:483`, `analytics_platform/onboarding.py:30`, `analytics_platform/research.py:143`, `analytics_platform/triage.py:30`, `analytics_platform/anomaly.py:11`, `analytics_platform/pipeline.py:29-48`
- Test: `tests/test_brain_retrieval.py` (append)

**Interfaces:**
- Consumes: `BrainIndex` (Tasks 4-5), `get_embedder` (Task 3), `TenantStoreProvider` (prerequisite plan).
- Produces: every service that builds a `CompanyBrain` accepts an `embedder: Optional[Embedder] = None` constructor argument and constructs the index per tenant inside `brain()`.

**Note the shape change from per-tenant databases.** There is no single process-wide `BrainIndex` any more — an index is bound to one tenant's store, so it is built where the store is resolved. What *is* shared is the `Embedder`: loading a model costs seconds and hundreds of megabytes, so exactly one is created in `make_context` and handed to every service. `BrainIndex` itself is a thin wrapper over `(store, embedder)` and is cheap to construct per call.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_brain_retrieval.py`:

```python
class ContextWiringTest(unittest.TestCase):
    """Regression guard for the defect that made the Brain look empty.

    Every service that reads the Brain must receive an index. This test fails if
    any of them is constructed without one.
    """

    def setUp(self):
        import tempfile
        from analytics_platform.api import make_context
        from analytics_platform.config import Settings
        self._tmp = tempfile.TemporaryDirectory()
        self.ctx = make_context(Settings(data_dir=self._tmp.name,
                                         embedding_enabled=False))
        self.ctx.tenants.create("t1", name="T1")

    def tearDown(self):
        self.ctx.stores.close_all()
        self._tmp.cleanup()

    def test_every_brain_reader_has_an_index(self):
        for name in ("stakeholder", "junior", "onboarding", "research", "triage"):
            service = getattr(self.ctx, name, None)
            if service is None:
                continue
            brain = service.brain("t1")
            self.assertIsNotNone(
                brain.index, f"{name}.brain() has no BrainIndex — searches "
                             f"will silently return recency order")

    def test_each_brain_index_uses_its_own_tenants_store(self):
        """An index bound to the wrong store would read another company's data."""
        self.ctx.tenants.create("t2", name="T2")
        a = self.ctx.stakeholder.brain("t1").index
        b = self.ctx.stakeholder.brain("t2").index
        self.assertNotEqual(a.store.db_path, b.store.db_path)

    def test_the_embedder_is_shared_across_tenants(self):
        """Loading a model per tenant would cost seconds and hundreds of MB each."""
        self.ctx.tenants.create("t2", name="T2")
        self.assertIs(self.ctx.stakeholder.brain("t1").index.embedder,
                      self.ctx.stakeholder.brain("t2").index.embedder)

    def test_stakeholder_retrieves_an_approved_query_from_a_paraphrase(self):
        brain = self.ctx.stakeholder.brain("t1")
        node = brain.create(NodeKind.QUERY, "Checkout conversion rate",
                            payload={"sql": "SELECT 1", "dialect": "duckdb"},
                            summary="Share of sessions reaching payment")
        brain.submit(node.id, by="junior")
        brain.approve(node.id, by="senior")

        q, d = self.ctx.stakeholder._retrieve(
            "t1", "how is our checkout conversion doing?")
        self.assertIn(node.id, [n.id for n in q])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_brain_retrieval.py::ContextWiringTest -v`
Expected: FAIL — `test_every_brain_reader_has_an_index` reports `stakeholder.brain() has no BrainIndex`.

- [ ] **Step 3: Thread the shared embedder through each service**

For each of these files, add `embedder: Optional[Embedder] = None` to the constructor signature, store it as `self.embedder = embedder`, and build the index where the tenant's store is resolved.

`analytics_platform/stakeholder.py` — add the imports and change `brain()`:

```python
from .brain.embedding import Embedder
from .brain.index import BrainIndex
```

```python
    def brain(self, tenant_id: str) -> CompanyBrain:
        # The index is bound to one tenant's database, so it is built where that
        # store is resolved. The embedder is the expensive part and is shared.
        store = self.stores.for_tenant(tenant_id)
        return CompanyBrain(store, tenant_id,
                            index=BrainIndex(store, embedder=self.embedder))
```

Add `embedder: Optional[Embedder] = None` to `StakeholderService.__init__` (after `settings`) and set `self.embedder = embedder` alongside the other assignments.

Apply the identical change to:
- `analytics_platform/onboarding.py:30` (`OnboardingService.brain`)
- `analytics_platform/junior.py:111` (`JuniorEngine.brain`) and the direct construction at `junior.py:186`
- `analytics_platform/triage.py:30`
- `analytics_platform/research.py:143`
- `analytics_platform/anomaly.py:11` (the `self.brain = lambda t: ...` closure)
- `analytics_platform/junior_worker.py:307` and `:483` — this class is per-tenant, so build one `BrainIndex` in `__init__` and reuse it
- `analytics_platform/pipeline.py:39` — the default `brain_factory` becomes
  `lambda s, t: CompanyBrain(s, t, index=BrainIndex(s, embedder=self.embedder))`

- [ ] **Step 4: Build one shared embedder in make_context**

In `analytics_platform/api.py`, replace the vector-store block:

```python
    try:
        from .brain.vector_store import BrainVectorStore
        vector_store = BrainVectorStore(settings.resolve_vector_path())
    except Exception:
        vector_store = None
```

with:

```python
    from .brain.embedding import get_embedder
    # One model for the whole process. Loading it costs seconds and hundreds of
    # megabytes; the per-tenant BrainIndex objects that wrap it are free.
    embedder = get_embedder(settings)
    if not embedder.available:
        logger.warning("Brain retrieval running lexical-only: embeddings unavailable")
```

Replace the `brain_factory` line:

```python
                        brain_factory=lambda s, t: CompanyBrain(s, t, vector_store=vector_store))
```

with:

```python
                        brain_factory=lambda s, t: CompanyBrain(
                            s, t, index=BrainIndex(s, embedder=embedder)))
```

Then pass `embedder=embedder` into the constructors of `StakeholderService`, `OnboardingService`, `ResearchService`, `JuniorEngine`, and any other service updated in Step 3. Add near the top of `api.py` if not present:

```python
import logging

from .brain.index import BrainIndex

logger = logging.getLogger(__name__)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_brain_retrieval.py -v`
Expected: 17 passed (13 from Task 7 + this step's 4 new `ContextWiringTest` cases).

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: **all 14 of Task 7's documented failures are now resolved** — 344 passed, 1 skipped, 0 failed (330 + 14). If any of the 14 named failures (see Task 7's "Baseline going into this task" note above) is still failing, that construction site was missed; go back and check it against the Files list above. `tests/test_vector_search.py` may now fail — it is deleted in Task 9. If it does, note it and continue; it is not one of the 14.

- [ ] **Step 6b: Re-verify the two isolation tests that were passing vacuously**

Task 7's review found that `tests/test_brain.py::test_tenant_isolation` and `::test_mark_stale` currently pass **vacuously**: with no index anywhere, `search()` always returns `[]` regardless of tenant, so the isolation assertion (`brain_b.search("churn") == []`) proves nothing about isolation — it would pass even if isolation were broken. Now that every `CompanyBrain` has an index, re-run these two specifically and read what they actually assert:

Run: `.venv/bin/python -m pytest tests/test_brain.py::TestBrain::test_tenant_isolation tests/test_brain.py::TestBrain::test_mark_stale -v`

Confirm `test_tenant_isolation` now has a real positive case to fail against (a node that DOES exist and DOES match for the node's own tenant, alongside the negative case for the other tenant) — if it only ever asserts the negative case, it is still vacuous and should be strengthened here, not left for a future task to rediscover.

- [ ] **Step 7: Commit**

```bash
git add analytics_platform/ tests/test_brain_retrieval.py
git commit -m "fix(brain): inject the retrieval index into every brain consumer"
```

---

### Task 9: Remove ChromaDB and backfill the index

**Why:** Chroma is now dead weight: a second stateful store, a 1.3 GB model default, a shared cross-tenant collection, and a dual-write path whose failures were swallowed. Removing it is the point of the exercise. Existing databases also need their nodes indexed once — without a backfill, everything written before this change is invisible to search.

**Files:**
- Delete: `analytics_platform/brain/vector_store.py`, `tests/test_vector_search.py`
- Modify: `requirements-advanced.txt`, `analytics_platform/cli.py`
- Test: `tests/test_brain_index.py` (append)

**Interfaces:**
- Consumes: `BrainIndex` (Tasks 4-5).
- Produces: `BrainIndex.reindex_tenant(tenant_id: str) -> int` (returns nodes indexed) and a `reindex` CLI command.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_brain_index.py`:

```python
class ReindexTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.index = BrainIndex(self.ctx.store, embedder=NullEmbedder("test"))
        # Nodes written before the index existed: rows with no FTS entries.
        for i, (title, summary) in enumerate([
                ("Checkout conversion", "Sessions reaching payment"),
                ("Refund rate", "Share of orders refunded")], start=1):
            self.ctx.store.execute(
                "INSERT INTO knowledge_nodes (id,tenant_id,kind,status,version,title,"
                "summary,payload,confidence,evidence_ref,source_ref,created_at,"
                "updated_at,created_by,reviewed_by,review_notes,supersedes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"kn_{i}", "t1", "METRIC", "APPROVED", 1, title, summary,
                 "{}", "{}", "", "", "2026-01-01", "2026-01-01", "seed", "", "", ""))

    def tearDown(self):
        self.ctx.close()

    def test_nodes_are_invisible_before_reindex(self):
        self.assertEqual(self.index.lexical_search("conversion", "t1", None, 10), [])

    def test_reindex_returns_the_node_count(self):
        self.assertEqual(self.index.reindex_tenant("t1"), 2)

    def test_nodes_are_searchable_after_reindex(self):
        self.index.reindex_tenant("t1")
        self.assertEqual(self.index.lexical_search("conversion", "t1", None, 10), ["kn_1"])

    def test_reindex_is_idempotent(self):
        self.index.reindex_tenant("t1")
        self.index.reindex_tenant("t1")
        rows = self.ctx.store.query_all(
            "SELECT node_id FROM knowledge_fts WHERE node_id = ?", ("kn_1",))
        self.assertEqual(len(rows), 1)

    def test_reindex_does_not_touch_other_tenants(self):
        self.assertEqual(self.index.reindex_tenant("t2"), 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_brain_index.py::ReindexTest -v`
Expected: FAIL with `AttributeError: 'BrainIndex' object has no attribute 'reindex_tenant'`.

- [ ] **Step 3: Add reindex_tenant**

Append to `BrainIndex` in `analytics_platform/brain/index.py`:

```python
    def reindex_tenant(self, tenant_id: str, batch: int = 256) -> int:
        """Rebuild both legs for one tenant. Safe to re-run; returns node count."""
        rows = self.store.query_all(
            "SELECT id, title, summary FROM knowledge_nodes WHERE tenant_id = ?",
            (tenant_id,))
        for row in rows:
            self.upsert(row["id"], tenant_id, row["title"] or "", row["summary"] or "")
        logger.info("reindexed %d node(s) for tenant %s (embeddings=%s)",
                    len(rows), tenant_id, self.embedding_available)
        return len(rows)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_brain_index.py::ReindexTest -v`
Expected: 5 passed.

- [ ] **Step 5: Add the CLI command**

In `analytics_platform/cli.py`, following the existing subcommand pattern in that file, add a `reindex` command that takes `--tenant`:

```python
def _cmd_reindex(args) -> int:
    from .brain.embedding import get_embedder
    from .brain.index import BrainIndex
    from .config import Settings
    from .stores import TenantStoreProvider

    settings = Settings.from_env()
    stores = TenantStoreProvider(
        control_db_path=settings.resolve_control_db_path(),
        tenants_root=settings.resolve_tenants_root())
    try:
        # Reindexing a tenant means opening that tenant's own database.
        store = stores.for_tenant(args.tenant)
        index = BrainIndex(store, embedder=get_embedder(settings))
        if not index.embedding_available:
            print("warning: embeddings unavailable; rebuilding the lexical leg only")
        total = index.reindex_tenant(args.tenant)
        print(f"reindexed {total} node(s) for tenant {args.tenant} ({store.db_path})")
    finally:
        stores.close_all()
    return 0
```

Register it alongside the other subparsers:

```python
    p_reindex = sub.add_parser("reindex", help="rebuild Brain search indexes for a tenant")
    p_reindex.add_argument("--tenant", required=True)
    p_reindex.set_defaults(func=_cmd_reindex)
```

- [ ] **Step 6: Delete Chroma**

```bash
git rm analytics_platform/brain/vector_store.py tests/test_vector_search.py
```

In `requirements-advanced.txt`, delete the `chromadb>=0.4.24` line. Keep `sentence-transformers>=2.5.0` — the new embedder uses it. Update the section comment:

```
# Embeddings for Brain hybrid retrieval (vectors live in SQLite; no vector DB)
sentence-transformers>=2.5.0
```

- [ ] **Step 7: Verify nothing references Chroma**

Run: `grep -rniE "chroma|vector_store|resolve_vector_path" --include="*.py" --include="*.txt" --include="*.md" analytics_platform/ tests/ requirements*.txt`
Expected: no matches. Any hit is a leftover to remove.

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass. The pre-existing failures baseline from Task 1 Step 6 should be unchanged or improved.

- [ ] **Step 9: Reindex the existing tenant and verify by hand**

```bash
.venv/bin/python -m analytics_platform reindex --tenant tnt_d23cd823d4c6
```

Expected: a non-zero node count. Then confirm retrieval works on real data:

```bash
.venv/bin/python -c "
from analytics_platform.api import make_context
from analytics_platform.config import Settings
from analytics_platform.domain import NodeKind
ctx = make_context(Settings.from_env())
hits = ctx.stakeholder.brain('tnt_d23cd823d4c6').search('checkout conversion', kind=NodeKind.QUERY)
print(f'{len(hits)} hit(s)')
for n in hits[:5]:
    print(' -', n.title)
"
```

Expected: at least one hit if the tenant has approved QUERY nodes whose titles or summaries relate to the phrase. If it prints `0 hit(s)`, check `.venv/bin/python -m analytics_platform reindex` actually reported a non-zero count, and confirm the tenant has approved nodes with `ctx.stakeholder.brain('tnt_d23cd823d4c6').stats()`.

- [ ] **Step 10: Clean up the orphaned Chroma directories**

These are no longer read by anything. Confirm the previous step worked before removing them.

```bash
rm -rf .chroma_db tenants/DTDL/.chroma_db
# The legacy per-tenant Chroma dir goes too; vectors now live in tenant.db.
```

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "refactor(brain): remove ChromaDB, add reindex command

Vectors now live in SQLite beside the nodes they describe, so the index cannot
drift from the source of truth and tenant isolation is a SQL invariant. Adds
'reindex' for existing databases."
```

---

## Verification

After Task 9, confirm the whole plan landed:

- [ ] `.venv/bin/python -m pytest tests/ -q` — all green
- [ ] `grep -rni "chroma" analytics_platform/ tests/ requirements*.txt` — no matches
- [ ] `grep -rn "except Exception:\s*$" analytics_platform/brain/` followed by a `pass` — no matches
- [ ] `.venv/bin/python -m analytics_platform reindex --tenant tnt_d23cd823d4c6` — non-zero count
- [ ] A paraphrased question against a real approved node returns that node (Task 9 Step 9)

## Follow-on plans

This plan fixes retrieval only. Three sibling plans cover the rest of the evaluation:

- `2026-08-13-brain-governance.md` — junior self-approval, ungated review endpoint, the AI senior's non-gating verdict, two dead write paths
- `2026-08-13-skills-portability.md` — placeholder mismatch, tenant-specific SQL in core, skills as a fallback rather than an orthogonal axis, no write-back loop
- `2026-08-13-frameworks-and-confidence.md` — the four friction types and Metrics Tree in prompts, freshness decay, evidence scoring

## PR #3 — hold, then port (do not merge as-is)

[PR #3](https://github.com/BusinessAnalyst-AbhinavGupta/ai_analytics_advanced/pull/3) ("feat(stakeholder): wire tenant-isolated vector search into Brain, Stakeholder, and Triage") was opened before the tenant-store-isolation plan merged and now conflicts with it — `mergeable: CONFLICTING` on GitHub, confirmed via `git merge-tree`. It still constructs `StakeholderService`/`TriageService` with the pre-isolation `store: Store` + `vector_store: Any` signature; `main` now takes `stores: TenantStoreProvider` with no `vector_store` parameter. The conflict isn't resolvable by picking a side — the PR's features need re-implementing against the current constructors.

Decision (2026-08-14, user-confirmed): **hold PR #3 closed/unmerged until this plan's Task 9 removes ChromaDB, then port its durable features onto whatever `CompanyBrain.search()` looks like post-Task-9** — do not rebase it onto the old architecture first, since its Chroma-specific fixes become moot the moment Chroma is gone.

What's durable and worth porting after Task 9:
- **`StakeholderService._extract_search_intent()`** — a fast LLM call that distills a verbose question into a 2-4 word topic before running retrieval, improving recall quality. Applies directly to this plan's `search()` regardless of backend — port as-is, called before the `lexical_search`/`vector_search` calls in the rewritten `CompanyBrain.search()`.
- **`StakeholderService._synthesize_sql()`** — lets the stakeholder analyst write and execute ad-hoc SQL from approved query/definition context (a new `AnswerMode.ADAPTED_APPROVED_QUERY` path) instead of only reusing an approved query verbatim. Independent of the retrieval backend — port as a stakeholder.py feature, not a brain.py one.

What becomes unnecessary once Task 9 lands (do not port):
- The `BrainVectorStore.search_similar()` `$and` filter fix — dead once ChromaDB is removed.
- `CompanyBrain.reindex_vectors()` (Chroma-specific rebuild) — superseded by this plan's own `reindex_tenant()` (Task 9).
- The `vector_store` threading through `AppContext`/`make_context` — this plan's `embedder` (Task 8) replaces it.

Action when starting this plan: after Task 9 is reviewed clean, re-open or re-create a PR porting `_extract_search_intent` and `_synthesize_sql` onto the then-current `stakeholder.py`, then close PR #3 without merging (its diff will no longer apply).
