# Analytical Frameworks & Confidence Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the analytical frameworks that differentiate this product from generic text-to-SQL into the code path that actually runs, and make the confidence dimensions the Brain stores reflect reality instead of their initial constants.

**Architecture:** The frameworks in `AGENTS.md` Parts 2 and 3 become a versioned prompt module in core plus a validator that checks the model's output honoured them. Confidence stops being write-once: freshness decays with age on read, evidence is derived from what actually backs a node, and a scheduled sweep marks nodes stale once they fall below a threshold.

**Tech Stack:** Python 3.14, stdlib `datetime`/`json`, `unittest` + `pytest`.

## Global Constraints

- **No new dependencies.**
- **Core, not tenant.** All code lands in `analytics_platform/`. Per `AGENTS.md` Part 1 §2.
- **`AGENTS.md` Parts 2 and 3 are the specification.** Copy the definitions verbatim; do not paraphrase the four friction types or the Metrics Tree tiers.
- **Prompts are data, not string literals scattered through logic.** All framework text lives in one module so it can be versioned and diffed.
- **Validation reports, it does not silently rewrite.** If the model skips a required layer, the answer carries a caveat — the platform never fabricates the missing section.
- **No silent failures.** Every `except` this plan touches logs at WARNING or higher.
- Run all commands from the repo root with `.venv/bin/python`.

---

## File Structure

**Created:**
- `analytics_platform/frameworks.py` — the friction taxonomy, the Metrics Tree, the analytics sequence, and the prompt fragments built from them
- `tests/test_frameworks.py`, `tests/test_confidence.py`

**Modified:**
- `analytics_platform/stakeholder.py:294-300` — `_synthesize` system prompt
- `analytics_platform/junior.py` — hypothesis prompts use the friction taxonomy
- `analytics_platform/brain/store.py` — freshness decay on read, evidence scoring on write
- `analytics_platform/scheduler.py` — a staleness sweep
- `analytics_platform/config.py` — `freshness_half_life_days`, `staleness_threshold`

---

### Task 1: The frameworks module

**Why:** A grep for `Matching Friction`, `North Star`, `Driver Metric` or `Guardrail` across the whole codebase returns nothing. The four friction types and the Metrics Tree exist only in `AGENTS.md`, so no model has ever seen them — the Stakeholder Analyst's entire system prompt is "a cautious internal analytics assistant... do not invent figures." The frameworks that most distinguish this product from a generic text-to-SQL wrapper are documentation nobody reads at runtime.

**Files:**
- Create: `analytics_platform/frameworks.py`
- Test: `tests/test_frameworks.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `FRICTION_TYPES: Dict[str, str]` — the four types and their diagnostic questions
  - `METRIC_TIERS: Dict[str, str]` — NSM, Input, Driver, Guardrail
  - `ANALYTICS_SEQUENCE: Tuple[str, ...]` — `("descriptive", "diagnostic", "prescriptive")`
  - `stakeholder_system_prompt() -> str`
  - `junior_hypothesis_prompt() -> str`
  - `validate_answer(text: str) -> List[str]` — caveats naming any framework rule the answer appears to break

  Tasks 2 and 3 consume these.

- [ ] **Step 1: Write the failing test**

Create `tests/test_frameworks.py`:

```python
"""The AGENTS.md analytical frameworks, as code the models actually see."""
from __future__ import annotations

import unittest

from analytics_platform.frameworks import (ANALYTICS_SEQUENCE, FRICTION_TYPES,
                                           METRIC_TIERS,
                                           junior_hypothesis_prompt,
                                           stakeholder_system_prompt,
                                           validate_answer)


class TaxonomyTest(unittest.TestCase):
    def test_all_four_friction_types_are_present(self):
        self.assertEqual(set(FRICTION_TYPES),
                         {"matching", "educational", "operational", "motivational"})

    def test_each_friction_type_has_a_diagnostic_question(self):
        for name, text in FRICTION_TYPES.items():
            self.assertTrue(text.strip().endswith("?"), f"{name} is not a question")

    def test_all_four_metric_tiers_are_present(self):
        self.assertEqual(set(METRIC_TIERS),
                         {"north_star", "input", "driver", "guardrail"})

    def test_the_analytics_sequence_is_ordered(self):
        self.assertEqual(ANALYTICS_SEQUENCE,
                         ("descriptive", "diagnostic", "prescriptive"))


class PromptTest(unittest.TestCase):
    def test_the_stakeholder_prompt_names_every_friction_type(self):
        prompt = stakeholder_system_prompt().lower()
        for name in FRICTION_TYPES:
            self.assertIn(name, prompt)

    def test_the_stakeholder_prompt_states_the_guardrail_rule(self):
        self.assertIn("guardrail", stakeholder_system_prompt().lower())

    def test_the_stakeholder_prompt_keeps_the_no_invented_figures_rule(self):
        self.assertIn("invent", stakeholder_system_prompt().lower())

    def test_the_junior_prompt_names_every_friction_type(self):
        prompt = junior_hypothesis_prompt().lower()
        for name in FRICTION_TYPES:
            self.assertIn(name, prompt)

    def test_prompts_are_not_empty(self):
        self.assertGreater(len(stakeholder_system_prompt()), 200)
        self.assertGreater(len(junior_hypothesis_prompt()), 200)


class ValidateAnswerTest(unittest.TestCase):
    def test_a_prescription_without_facts_is_flagged(self):
        caveats = validate_answer("We should immediately redesign the checkout page.")
        self.assertTrue(any("descriptive" in c.lower() for c in caveats))

    def test_a_driver_recommendation_without_a_guardrail_is_flagged(self):
        caveats = validate_answer(
            "Conversion was 62% in Q3. The drop is operational friction. "
            "We recommend increasing push notification frequency.")
        self.assertTrue(any("guardrail" in c.lower() for c in caveats))

    def test_a_well_formed_answer_is_not_flagged(self):
        caveats = validate_answer(
            "Checkout completion was 62% in Q3, down from 71%. "
            "This is operational friction: the payment step errors on mobile. "
            "We recommend fixing the mobile payment form. "
            "Guardrail: watch refund rate, which must not rise above 2%.")
        self.assertEqual(caveats, [])

    def test_a_purely_descriptive_answer_is_not_flagged(self):
        self.assertEqual(validate_answer("Checkout completion was 62% in Q3."), [])

    def test_empty_input_is_not_flagged(self):
        self.assertEqual(validate_answer(""), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_frameworks.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'analytics_platform.frameworks'`.

- [ ] **Step 3: Write the implementation**

Create `analytics_platform/frameworks.py`:

```python
"""The analytical frameworks from AGENTS.md Parts 2 and 3, as runtime code.

These frameworks are what separate this platform from a text-to-SQL wrapper, so
they belong in the prompts the models actually receive rather than in a document
nobody reads at runtime. Keeping them in one module means they can be versioned
and diffed, instead of drifting across scattered string literals.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

# -- AGENTS.md Part 2 §1: Diagnostic Framework for Funnels --------------------
FRICTION_TYPES: Dict[str, str] = {
    "matching": "Were the wrong users acquired for the product?",
    "educational": "Do users not understand the value proposition or the next step?",
    "operational": "Is there a broken UI, a bug, performance issues, or UX flow problems?",
    "motivational": "Do users lack the incentive or psychological push to proceed?",
}

# -- AGENTS.md Part 2 §2: Metrics Hierarchy & Guardrails ----------------------
METRIC_TIERS: Dict[str, str] = {
    "north_star": ("The single outcome representing value "
                   "(e.g. total listening minutes per active user per month)."),
    "input": ("Components that mathematically contribute to the NSM "
              "(e.g. active days/month x sessions/day x minutes/session)."),
    "driver": ("Operational levers owned by teams that move the input metrics "
               "(e.g. push-open rate, click rate, return rate)."),
    "guardrail": ("Metrics that protect against harmful optimization "
                  "(e.g. trust, technical health, economics, long-term retention)."),
}

# -- AGENTS.md Part 3 §1: The Analytics Sequence ------------------------------
ANALYTICS_SEQUENCE: Tuple[str, ...] = ("descriptive", "diagnostic", "prescriptive")

GUARDRAIL_RULE = ("Any optimization or prescriptive action targeting a Driver Metric "
                  "MUST explicitly state and check a relevant Guardrail metric.")


def _friction_block() -> str:
    return "\n".join(f"- {name.capitalize()} Friction: {q}"
                     for name, q in FRICTION_TYPES.items())


def _tier_block() -> str:
    labels = {"north_star": "North Star Metric (NSM)", "input": "Input Metrics",
              "driver": "Driver Metrics", "guardrail": "Guardrails"}
    return "\n".join(f"- {labels[k]}: {v}" for k, v in METRIC_TIERS.items())


def stakeholder_system_prompt() -> str:
    """System prompt for the Stakeholder Analyst's synthesis step."""
    return (
        "You are a cautious internal analytics assistant. Do not invent figures: "
        "every number you state must come from the data provided to you.\n\n"
        "Follow this sequence, and never skip a layer:\n"
        "1. Descriptive - establish the hard facts first.\n"
        "2. Diagnostic - form hypotheses about what drives those facts.\n"
        "3. Prescriptive - only then recommend prioritised actions.\n"
        "Never jump straight to a recommendation without stating the facts and "
        "diagnosing a plausible cause.\n\n"
        "When a drop-off or bottleneck is involved, classify it as exactly one of:\n"
        f"{_friction_block()}\n\n"
        "When you discuss metrics, place them in this hierarchy:\n"
        f"{_tier_block()}\n\n"
        f"{GUARDRAIL_RULE}\n"
    )


def junior_hypothesis_prompt() -> str:
    """Prompt fragment for the Junior Analyst's hypothesis generation."""
    return (
        "Generate hypotheses about what drives the observed pattern. Each "
        "hypothesis must name the friction type it belongs to, chosen from:\n"
        f"{_friction_block()}\n\n"
        "State what evidence would confirm or refute each hypothesis, and what "
        "query would produce that evidence. Do not recommend actions - that is "
        "the prescriptive layer and comes after diagnosis.\n"
    )


# -- output validation --------------------------------------------------------
_PRESCRIPTIVE = re.compile(
    r"\b(we should|we recommend|recommend(?:ation|ed)?|you should|next step|"
    r"action item|propose|suggest(?:ion|ed)?)\b", re.IGNORECASE)
_DESCRIPTIVE = re.compile(r"\d")          # any figure counts as a stated fact
_DIAGNOSTIC = re.compile(
    r"\b(because|driven by|caused by|due to|friction|hypothes[ie]s|likely|"
    r"explains?|attributable)\b", re.IGNORECASE)
_DRIVER_LEVER = re.compile(
    r"\b(increase|decrease|boost|raise|lower|push|optimi[sz]e|drive up|"
    r"drive down|improve)\b", re.IGNORECASE)
_GUARDRAIL = re.compile(r"\bguardrail\b", re.IGNORECASE)


def validate_answer(text: str) -> List[str]:
    """Caveats naming framework rules the answer appears to break.

    Reports only; it never edits the answer. Fabricating a missing descriptive
    layer would be worse than flagging its absence.
    """
    answer = (text or "").strip()
    if not answer:
        return []

    caveats: List[str] = []
    prescriptive = bool(_PRESCRIPTIVE.search(answer))

    if prescriptive and not _DESCRIPTIVE.search(answer):
        caveats.append("recommends an action without stating the descriptive facts "
                       "it rests on (AGENTS.md Part 3: sequence matters)")
    if prescriptive and not _DIAGNOSTIC.search(answer):
        caveats.append("recommends an action without a diagnostic layer explaining "
                       "why (AGENTS.md Part 3: sequence matters)")
    if prescriptive and _DRIVER_LEVER.search(answer) and not _GUARDRAIL.search(answer):
        caveats.append("targets a driver metric without naming a guardrail "
                       "(AGENTS.md Part 2 §2)")
    return caveats
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_frameworks.py -v`
Expected: 14 passed.

If `test_a_well_formed_answer_is_not_flagged` fails, the regexes are too eager — widen `_DIAGNOSTIC` or `_GUARDRAIL` rather than weakening the assertions. False positives on good answers are worse than missed detections, because every caveat is shown to a stakeholder.

- [ ] **Step 5: Commit**

```bash
git add analytics_platform/frameworks.py tests/test_frameworks.py
git commit -m "feat: encode the AGENTS.md analytical frameworks as runtime prompts"
```

---

### Task 2: Wire the frameworks into the prompts

**Why:** The module is inert until the personas use it. This is the task that changes what a stakeholder actually reads.

**Files:**
- Modify: `analytics_platform/stakeholder.py:294-300`, `analytics_platform/junior.py:499-607`
- Test: `tests/test_frameworks.py` (append)

**Interfaces:**
- Consumes: `stakeholder_system_prompt`, `junior_hypothesis_prompt`, `validate_answer` (Task 1).
- Produces: `_synthesize` uses the framework prompt; answers carry framework caveats in the existing `caveats` list.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_frameworks.py`:

```python
class WiringTest(unittest.TestCase):
    def setUp(self):
        from analytics_platform.stakeholder import StakeholderService
        from tests.helpers import make_ctx
        self.ctx = make_ctx()
        self.ctx.tenants.create("t1", name="T1")
        self.svc = StakeholderService(self.ctx.store, tenants=self.ctx.tenants,
                                      executor=self.ctx.executor,
                                      observability=self.ctx.obs,
                                      settings=self.ctx.settings)

    def tearDown(self):
        self.ctx.close()

    def test_the_synthesis_prompt_carries_the_frameworks(self):
        captured = {}

        class RecordingLLM:
            def generate(self, prompt, system_prompt="", **kw):
                captured["system"] = system_prompt

                class R:
                    text = "Checkout completion was 62%."
                    tokens_in = 1
                    tokens_out = 1
                return R()

        self.svc._synthesize(RecordingLLM(), "how is checkout doing?", "trend")
        for name in FRICTION_TYPES:
            self.assertIn(name, captured["system"].lower())

    def test_framework_caveats_reach_the_answer(self):
        caveats = validate_answer("We should immediately redesign checkout.")
        self.assertTrue(caveats)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_frameworks.py::WiringTest -v`
Expected: FAIL — the captured system prompt contains none of the friction type names.

If `_synthesize` has a different signature, read `stakeholder.py:290-320` and adapt the call — do not change the assertion.

- [ ] **Step 3: Replace the system prompt**

In `analytics_platform/stakeholder.py`, in `_synthesize`, replace the literal system prompt (around line 294) with:

```python
        system_prompt = stakeholder_system_prompt()
```

Add the import:

```python
from .frameworks import stakeholder_system_prompt, validate_answer
```

- [ ] **Step 4: Attach framework caveats to answers**

In `answer()`, on each path that produces a synthesised `answer` string, extend the caveats:

```python
            caveats = list(caveats) + validate_answer(answer)
```

Apply this to the skill path, the `NEW_LOW_RISK_ANALYSIS` fallback, and the approved-knowledge synthesis path — every branch where an LLM wrote prose. Do not apply it to `CANNOT_ANSWER` or escalation paths, which have no analytical content to validate.

- [ ] **Step 5: Use the taxonomy in the junior's hypotheses**

In `analytics_platform/junior.py`, in `suggest_hypotheses`, prepend `junior_hypothesis_prompt()` to the LLM enrichment prompt so generated hypotheses name a friction type. Add the import:

```python
from .frameworks import junior_hypothesis_prompt
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_frameworks.py -v`
Expected: 16 passed.

- [ ] **Step 7: Run the full suite and commit**

Run: `.venv/bin/python -m pytest tests/ -q`

```bash
git add analytics_platform/ tests/test_frameworks.py
git commit -m "feat: use the analytical frameworks in stakeholder and junior prompts"
```

---

### Task 3: Make confidence reflect reality

**Why:** Six confidence dimensions are initialised and only two are ever updated — `review` and `reproducibility`, both on approval. Nothing computes `evidence` or `definition`. `freshness` is pinned at 1.0 forever with no decay, yet `stakeholder.py` reads it for ranking, so ranking currently sorts on a constant. `AGENTS.md` also names a `data_quality` dimension that does not exist. Either the dimensions mean something or they should be deleted; this task makes them mean something.

**Files:**
- Modify: `analytics_platform/config.py`, `analytics_platform/brain/store.py`, `analytics_platform/scheduler.py`
- Test: `tests/test_confidence.py`

**Interfaces:**
- Consumes: `KnowledgeNode.confidence`, `updated_at` (existing).
- Produces:
  - `Settings.freshness_half_life_days: int = 90`, `Settings.staleness_threshold: float = 0.25`
  - `analytics_platform/brain/store.py`: `freshness_at(updated_at: str, half_life_days: int, now: Optional[datetime] = None) -> float` (module-level)
  - `CompanyBrain.effective_confidence(node) -> Dict[str, float]` — stored dimensions with `freshness` recomputed from age
  - `CompanyBrain.sweep_stale(threshold: float) -> int` — transitions decayed APPROVED nodes to STALE

- [ ] **Step 1: Write the failing test**

Create `tests/test_confidence.py`:

```python
"""Confidence dimensions that change with reality rather than staying constant."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from analytics_platform.brain.store import CompanyBrain, freshness_at
from analytics_platform.domain import NodeKind, ReviewStatus
from tests.helpers import make_ctx


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class FreshnessDecayTest(unittest.TestCase):
    def test_a_brand_new_node_is_fully_fresh(self):
        self.assertAlmostEqual(freshness_at(_iso(0), half_life_days=90), 1.0, places=2)

    def test_one_half_life_halves_freshness(self):
        self.assertAlmostEqual(freshness_at(_iso(90), half_life_days=90), 0.5, places=2)

    def test_two_half_lives_quarter_it(self):
        self.assertAlmostEqual(freshness_at(_iso(180), half_life_days=90), 0.25, places=2)

    def test_freshness_never_reaches_zero(self):
        self.assertGreater(freshness_at(_iso(3650), half_life_days=90), 0.0)

    def test_an_unparseable_timestamp_is_treated_as_stale_not_fresh(self):
        self.assertLess(freshness_at("not a date", half_life_days=90), 0.5)

    def test_a_future_timestamp_is_clamped_to_fully_fresh(self):
        self.assertAlmostEqual(freshness_at(_iso(-10), half_life_days=90), 1.0, places=2)


class EffectiveConfidenceTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.brain = CompanyBrain(self.ctx.store, "t1")
        self.node = self.brain.create(NodeKind.METRIC, "Gross margin", summary="x")

    def tearDown(self):
        self.ctx.close()

    def test_freshness_is_recomputed_not_read_from_storage(self):
        self.ctx.store.execute(
            "UPDATE knowledge_nodes SET updated_at=? WHERE id=?",
            (_iso(90), self.node.id))
        node = self.brain.get(self.node.id)
        self.assertAlmostEqual(
            self.brain.effective_confidence(node)["freshness"], 0.5, places=2)

    def test_other_dimensions_pass_through_unchanged(self):
        node = self.brain.get(self.node.id)
        self.assertEqual(self.brain.effective_confidence(node)["source"],
                         node.confidence["source"])

    def test_evidence_is_set_when_a_node_has_an_evidence_ref(self):
        node = self.brain.create(NodeKind.FINDING, "Backed finding",
                                 summary="x", evidence_ref="run_123")
        self.assertGreater(node.confidence["evidence"], 0.0)

    def test_evidence_stays_zero_without_a_reference(self):
        self.assertEqual(self.node.confidence["evidence"], 0.0)


class SweepStaleTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.brain = CompanyBrain(self.ctx.store, "t1")

    def tearDown(self):
        self.ctx.close()

    def _approved_aged(self, days: float):
        node = self.brain.create(NodeKind.METRIC, f"Metric {days}", summary="x")
        self.brain.submit(node.id, by="junior")
        node = self.brain.approve(node.id, by="senior")
        self.ctx.store.execute("UPDATE knowledge_nodes SET updated_at=? WHERE id=?",
                               (_iso(days), node.id))
        return node

    def test_a_decayed_node_is_marked_stale(self):
        node = self._approved_aged(400)
        self.assertEqual(self.brain.sweep_stale(threshold=0.25), 1)
        self.assertEqual(self.brain.get(node.id).status, ReviewStatus.STALE)

    def test_a_fresh_node_is_untouched(self):
        node = self._approved_aged(1)
        self.brain.sweep_stale(threshold=0.25)
        self.assertEqual(self.brain.get(node.id).status, ReviewStatus.APPROVED)

    def test_the_sweep_is_idempotent(self):
        self._approved_aged(400)
        self.brain.sweep_stale(threshold=0.25)
        self.assertEqual(self.brain.sweep_stale(threshold=0.25), 0)

    def test_candidates_are_not_swept(self):
        node = self.brain.create(NodeKind.METRIC, "Candidate", summary="x")
        self.ctx.store.execute("UPDATE knowledge_nodes SET updated_at=? WHERE id=?",
                               (_iso(400), node.id))
        self.brain.sweep_stale(threshold=0.25)
        self.assertEqual(self.brain.get(node.id).status, ReviewStatus.CANDIDATE)

    def test_other_tenants_are_not_swept(self):
        node = self._approved_aged(400)
        other = CompanyBrain(self.ctx.store, "t2")
        self.assertEqual(other.sweep_stale(threshold=0.25), 0)
        self.assertEqual(self.brain.get(node.id).status, ReviewStatus.APPROVED)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_confidence.py -v`
Expected: FAIL with `ImportError: cannot import name 'freshness_at'`.

- [ ] **Step 3: Add the settings**

In `analytics_platform/config.py`, beside the other Brain settings:

```python
    freshness_half_life_days: int = 90   # confidence.freshness halves every N days
    staleness_threshold: float = 0.25    # below this, an approved node is swept STALE
```

- [ ] **Step 4: Implement decay and the sweep**

Add to `analytics_platform/brain/store.py`, at module level:

```python
import math
from datetime import datetime, timezone

# Knowledge does not expire on a cliff, it loses authority gradually, so freshness
# decays exponentially rather than dropping at a fixed age.
def freshness_at(updated_at: str, half_life_days: int,
                 now: Optional[datetime] = None) -> float:
    """Freshness in (0, 1] from a node's age. Unparseable timestamps decay hard."""
    if half_life_days <= 0:
        return 1.0
    try:
        stamp = datetime.fromisoformat((updated_at or "").replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("unparseable updated_at %r, treating as stale: %s",
                       updated_at, exc)
        return 0.1
    reference = now or datetime.now(timezone.utc)
    age_days = (reference - stamp).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0   # clock skew: never more than fully fresh
    return float(2 ** (-age_days / half_life_days))
```

Add to `CompanyBrain`:

```python
    def effective_confidence(self, node: KnowledgeNode,
                             half_life_days: int = 90) -> Dict[str, float]:
        """Stored dimensions with `freshness` recomputed from the node's age.

        Freshness is a function of time, so storing it would mean rewriting every
        row on a schedule. It is derived on read instead.
        """
        conf = dict(node.confidence or {})
        conf["freshness"] = freshness_at(node.updated_at, half_life_days)
        return conf

    def sweep_stale(self, threshold: float = 0.25,
                    half_life_days: int = 90) -> int:
        """Mark decayed APPROVED nodes STALE. Returns the number transitioned."""
        rows = self.store.query_all(
            "SELECT * FROM knowledge_nodes WHERE tenant_id=? AND status IN (?,?)",
            (self.tenant_id, ReviewStatus.APPROVED.value,
             ReviewStatus.APPROVED_WITH_CAVEATS.value))
        swept = 0
        for row in rows:
            node = self._row_to_node(row)
            if freshness_at(node.updated_at, half_life_days) >= threshold:
                continue
            try:
                self.mark_stale(node.id, by="system")
                swept += 1
            except BrainConflict as exc:
                logger.warning("could not mark %s stale: %s", node.id, exc)
        if swept:
            logger.info("swept %d node(s) to STALE for tenant %s", swept, self.tenant_id)
        return swept
```

- [ ] **Step 5: Seed evidence on creation**

In `CompanyBrain.create`, after `base_conf.update(confidence)`, add:

```python
        # A node backed by a concrete analysis run carries more evidential weight
        # than one asserted from nothing.
        if evidence_ref and not (confidence or {}).get("evidence"):
            base_conf["evidence"] = 0.6
```

- [ ] **Step 6: Use effective confidence in ranking**

In `search()` (from the retrieval plan's Task 7), replace:

```python
        confidence_by_id = {n.id: n.confidence for n in nodes}
```

with:

```python
        confidence_by_id = {n.id: self.effective_confidence(n) for n in nodes}
```

If the retrieval plan has not landed, apply this wherever `confidence["freshness"]` is read in `stakeholder.py:158,200`.

- [ ] **Step 7: Schedule the sweep**

In `analytics_platform/scheduler.py`, add a `_staleness_tick` alongside `_junior_tick` that runs `sweep_stale` once per day per tenant, using `settings.staleness_threshold` and `settings.freshness_half_life_days`, following the existing tick pattern in that file.

- [ ] **Step 8: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_confidence.py -v`
Expected: 15 passed.

- [ ] **Step 9: Run the full suite and commit**

Run: `.venv/bin/python -m pytest tests/ -q`

```bash
git add analytics_platform/ tests/test_confidence.py
git commit -m "feat(brain): decay freshness, seed evidence, sweep stale knowledge

Ranking previously sorted on a constant because freshness was written once and
never recomputed."
```

---

### Task 4: Reconcile the documented dimensions with the stored ones

**Why:** `AGENTS.md` names four dimensions — Evidence, Review status, Freshness, Data Quality — while the code stores six, of which `data_quality` is not one and `definition`, `reproducibility` and `source` are undocumented. One of the two is wrong. Leaving them out of step means neither can be trusted as the specification.

**Files:**
- Modify: `AGENTS.md:25`, `analytics_platform/brain/store.py:77-78`
- Test: `tests/test_confidence.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_confidence.py`:

```python
class DimensionParityTest(unittest.TestCase):
    """The stored dimensions and the documented ones must agree."""

    DOCUMENTED = {"evidence", "review", "freshness", "data_quality",
                  "definition", "reproducibility", "source"}

    def setUp(self):
        self.ctx = make_ctx()

    def tearDown(self):
        self.ctx.close()

    def test_stored_dimensions_match_the_documented_set(self):
        brain = CompanyBrain(self.ctx.store, "t1")
        node = brain.create(NodeKind.METRIC, "M", summary="x")
        self.assertEqual(set(node.confidence), self.DOCUMENTED)

    def test_data_quality_is_present(self):
        brain = CompanyBrain(self.ctx.store, "t1")
        node = brain.create(NodeKind.METRIC, "M", summary="x")
        self.assertIn("data_quality", node.confidence)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_confidence.py::DimensionParityTest -v`
Expected: FAIL — `data_quality` is absent from the stored set.

- [ ] **Step 3: Add the missing dimension**

In `analytics_platform/brain/store.py`, `create`:

```python
        base_conf = {"evidence": 0.0, "review": 0.0, "definition": 0.0,
                     "freshness": 1.0, "reproducibility": 0.0, "source": 0.5,
                     "data_quality": 0.0}
```

- [ ] **Step 4: Update AGENTS.md**

Replace line 25 of `AGENTS.md`:

```markdown
- **Dimensional Confidence:** Confidence in a finding is not a single weight. Agents must evaluate findings across multiple dimensions: Evidence, Review status, Freshness, and Data Quality.
```

with:

```markdown
- **Dimensional Confidence:** Confidence in a finding is not a single weight. Agents evaluate findings across seven dimensions, stored on every knowledge node: **evidence** (is there a concrete analysis run behind it), **review** (has a human or senior approved it), **definition** (are its terms defined in the Brain), **freshness** (decays with age; recomputed on read, never stored), **reproducibility** (does its query still run and return the same shape), **source** (credibility of the origin), and **data_quality** (profiling signal from the underlying data). Only **freshness** and **review** participate in retrieval ranking; the rest are shown to reviewers.
```

- [ ] **Step 5: Populate data_quality from the profiler**

`core/profiler/` already computes profile summaries for analysis runs. Where a FINDING is created from an `AnalysisRun` — `junior_worker.py` `_maybe_autopromote` and `pipeline.promote_finding` — pass a `data_quality` score derived from the run's `profile_summary` (for example `1.0 - null_fraction` of the primary column) into `brain.create(..., confidence={"data_quality": score})`. Read the profiler's output shape before choosing the formula, and record the choice in `HARDCODED_REGISTRY.md`.

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_confidence.py -v`
Expected: 17 passed.

- [ ] **Step 7: Run the full suite and commit**

Run: `.venv/bin/python -m pytest tests/ -q`

```bash
git add analytics_platform/ AGENTS.md tests/test_confidence.py
git commit -m "docs+feat: reconcile the documented and stored confidence dimensions"
```

---

## Verification

- [ ] `.venv/bin/python -m pytest tests/ -q` — all green
- [ ] `grep -rn "Matching Friction\|North Star\|Guardrail" analytics_platform/` — matches in `frameworks.py`
- [ ] A stakeholder answer recommending a driver action without a guardrail carries a caveat saying so
- [ ] A node whose `updated_at` is a year old reports `freshness` well below 1.0
- [ ] The documented dimensions in `AGENTS.md` and the stored keys in `create()` are the same set
