# Brain Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make "a human approved this" a fact the system enforces rather than a string a caller supplies — and repair the three write paths that currently crash or silently do nothing.

**Architecture:** Every write that turns AI output into approved company fact is funnelled through one guarded chokepoint. The junior's self-approval becomes an explicit, default-off tenant setting routed through the same gates as any other approval; the raw knowledge-review endpoint gets the `AuthGate` the senior endpoints already imply; the AI senior's verdict is parsed and actually decides the outcome. Three broken methods are repaired or deleted, with tests that fail if they regress to silence.

**Tech Stack:** Python 3.14, FastAPI 0.141, stdlib `sqlite3`, `unittest` + `pytest`.

## Global Constraints

- **No new dependencies.**
- **Core, not tenant.** All code lands in `analytics_platform/`. Per `AGENTS.md` Part 1 §2.
- **`AGENTS.md` Part 1 §3 is the specification:** "Agents generate hypotheses, execute SQL, and profile data, but humans approve knowledge into the Company Brain. Nothing generated silently becomes company fact without review."
- **Defaults are safe.** Every new setting that permits autonomous approval defaults to *off*. A fresh tenant must not self-approve.
- **No silent failures.** Every `except` this plan touches logs at WARNING or higher via `logging.getLogger(__name__)`.
- **Backwards compatibility.** Existing tests in `tests/test_review_flow.py`, `tests/test_senior_depth.py`, `tests/test_governance_auth.py` and `tests/test_junior.py` must keep passing, or be updated in the same commit with a note explaining why the old behaviour was wrong.
- Run all commands from the repo root with `.venv/bin/python`.

---

## File Structure

**Created:**
- `tests/test_brain_governance.py` — autopromote gating, endpoint auth, AI verdict gating
- `tests/test_dead_write_paths.py` — the three broken methods

**Modified:**
- `analytics_platform/config.py` — `junior_autopromote_enabled` setting
- `analytics_platform/tenancy.py` — expose the setting on the per-tenant analyst config
- `analytics_platform/junior_worker.py:285-321` — `_maybe_autopromote` gating
- `analytics_platform/api.py:710-723` — `AuthGate` on the knowledge review endpoint
- `analytics_platform/senior.py:195-205` — idempotent SQL node approval
- `analytics_platform/senior.py:223-257` — AI verdict gates the decision
- `analytics_platform/onboarding.py:77-135` — repair `bulk_ingest_json`
- `analytics_platform/anomaly.py:51-75` — repair or remove `evaluate_kpis`

---

### Task 1: The junior stops approving its own findings

**Why:** `_maybe_autopromote` creates a FINDING, submits it, and approves it as `by="junior"` — up to 500 per tenant — with no human and no senior in the loop. That is the exact thing `AGENTS.md` forbids, and it also degrades retrieval: 500 auto-approved exploratory probes outrank a handful of curated nodes by sheer volume. The fix is not to delete the feature but to make it an explicit, default-off decision that a tenant owner takes knowingly, and to leave the findings in the review queue when it is off.

**Files:**
- Modify: `analytics_platform/config.py`, `analytics_platform/tenancy.py`, `analytics_platform/junior_worker.py:285-321`
- Test: `tests/test_brain_governance.py`

**Interfaces:**
- Consumes: `TenantService.get_analyst_config` (existing).
- Produces: `Settings.junior_autopromote_enabled: bool = False`; the analyst config gains the same field; `_maybe_autopromote` returns a node left at `UNDER_REVIEW` when autopromote is off, and an `APPROVED` node only when it is explicitly on.

- [ ] **Step 1: Write the failing test**

Create `tests/test_brain_governance.py`:

```python
"""Governance: nothing becomes approved company fact without an authorised review."""
from __future__ import annotations

import unittest

import pandas as pd

from analytics_platform.domain import NodeKind, ReviewStatus
from analytics_platform.junior import JuniorEngine
from analytics_platform.junior_worker import JuniorWorker
from tests.helpers import make_ctx

_WAREHOUSE = {"things": pd.DataFrame({"col": [1, 2, 3, 4]})}


def _worker(ctx, tid: str, **kw):
    eng = JuniorEngine(ctx.store, executor=ctx.executor, tenants=ctx.tenants,
                       observability=ctx.obs)
    return JuniorWorker(ctx.store, eng, tenant_id=tid, observability=ctx.obs,
                        autopromote_cap=kw.get("autopromote_cap", 500),
                        autopromote_enabled=kw.get("autopromote_enabled", False))


class AutopromoteTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx(_WAREHOUSE)
        self.ctx.tenants.create("t1", name="T1")
        self.worker = _worker(self.ctx, "t1")

    def tearDown(self):
        self.ctx.close()

    def test_autopromote_is_off_by_default(self):
        self.assertFalse(_worker(self.ctx, "t1").autopromote_enabled)

    def test_disabled_leaves_the_finding_awaiting_review(self):
        run = self._completed_low_run()
        node = self.worker._maybe_autopromote("t1", run)
        self.assertIsNotNone(node)
        self.assertEqual(node.status, ReviewStatus.UNDER_REVIEW)

    def test_disabled_never_writes_an_approved_node(self):
        self.worker._maybe_autopromote("t1", self._completed_low_run())
        approved = self.ctx.store.query_all(
            "SELECT id FROM knowledge_nodes WHERE tenant_id=? AND kind=? AND status=?",
            ("t1", NodeKind.FINDING.value, ReviewStatus.APPROVED.value))
        self.assertEqual(approved, [])

    def test_enabled_approves_and_attributes_to_junior_auto(self):
        worker = _worker(self.ctx, "t1", autopromote_enabled=True)
        node = worker._maybe_autopromote("t1", self._completed_low_run())
        self.assertEqual(node.status, ReviewStatus.APPROVED)
        self.assertEqual(node.reviewed_by, "junior-auto")

    def test_high_level_runs_never_autopromote(self):
        worker = _worker(self.ctx, "t1", autopromote_enabled=True)
        run = self._completed_low_run()
        run.level = "high"
        self.assertIsNone(worker._maybe_autopromote("t1", run))

    def _completed_low_run(self):
        from analytics_platform.domain import AnalysisRun, RunStatus, new_id
        return AnalysisRun(
            id=new_id("run"), tenant_id="t1", question_id=new_id("q"),
            question_text="How many things are there?", sql="SELECT COUNT(*) FROM things",
            status=RunStatus.COMPLETED, row_count=1, level="low",
            answer="There are 4 things.", rule_triggers=[])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_brain_governance.py -v`
Expected: FAIL — `JuniorWorker.__init__() got an unexpected keyword argument 'autopromote_enabled'`.

If `AnalysisRun`'s constructor signature differs from the helper above, read `analytics_platform/domain.py` and adjust the field names — do not change the assertions.

- [ ] **Step 3: Add the setting**

In `analytics_platform/config.py`, beside `junior_autopromote_cap`:

```python
    # Governance: AGENTS.md requires humans to approve knowledge. Auto-approval of
    # the junior's own low-level findings is therefore opt-in per tenant, never a
    # default. When off, findings still reach the Brain — as UNDER_REVIEW.
    junior_autopromote_enabled: bool = False
```

In `analytics_platform/tenancy.py`, add the same field to the analyst config dataclass that `get_analyst_config` returns, defaulting to `False`, following the pattern the existing junior settings use in that file.

- [ ] **Step 4: Gate the promotion**

In `analytics_platform/junior_worker.py`, add `autopromote_enabled: bool = False` to `JuniorWorker.__init__` and store `self.autopromote_enabled = autopromote_enabled`.

Replace the tail of `_maybe_autopromote` (the `brain = CompanyBrain(...)` block through `return node`) with:

```python
        brain = CompanyBrain(self.store, tid, index=self.index)
        node = brain.create(
            NodeKind.FINDING, title=run.question_text, summary=run.answer,
            payload={"facts": run.facts, "hypotheses": run.hypotheses,
                     "rule_triggers": run.rule_triggers, "sql": run.sql,
                     "row_count": run.row_count, "category": run.category,
                     "level": run.level, "supportive_of": run.supportive_of},
            evidence_ref=run.id, source_ref=run.question_id, created_by="junior")
        node = brain.submit(node.id, by="junior")

        if not self.autopromote_enabled:
            # AGENTS.md: nothing generated silently becomes company fact. The
            # finding is preserved and queued, not approved.
            self.obs.event(tenant_id=tid, stage="junior.autopromote_withheld",
                           actor="junior", resource=node.id, status="OK",
                           meta={"run": run.id, "reason": "autopromote disabled"})
            return node

        node = brain.approve(node.id, by="junior-auto",
                             notes="auto-approved: low-level exploratory, "
                                   "junior_autopromote_enabled=True")
        self.obs.event(tenant_id=tid, stage="junior.autopromote", actor="junior-auto",
                       resource=node.id, status="OK",
                       meta={"run": run.id, "category": run.category})
        return node
```

Note `index=self.index` — that argument comes from the retrieval plan's Task 8. If that plan has not landed yet, use `CompanyBrain(self.store, tid)` and revisit.

- [ ] **Step 5: Pass the setting through the construction chain**

Wherever `JuniorWorker` is constructed in `analytics_platform/scheduler.py` and `analytics_platform/api.py`, pass `autopromote_enabled=cfg.junior_autopromote_enabled` from the tenant's analyst config, following how `autopromote_cap` is already threaded.

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_brain_governance.py -v`
Expected: 5 passed.

- [ ] **Step 7: Update the existing suite**

Run: `.venv/bin/python -m pytest tests/test_review_flow.py tests/test_junior.py -v`

Any test asserting that a low-level run becomes `APPROVED` was asserting the defect. Update it to pass `autopromote_enabled=True` explicitly where it is testing the promotion mechanics, or to expect `UNDER_REVIEW` where it is testing default behaviour. Add a comment on each change naming this plan.

- [ ] **Step 8: Commit**

```bash
git add analytics_platform/ tests/test_brain_governance.py
git commit -m "fix(governance): junior auto-approval becomes opt-in, default off

AGENTS.md requires human approval before knowledge becomes company fact. Findings
are still captured — they now wait at UNDER_REVIEW instead of self-approving."
```

---

### Task 2: Authenticate the knowledge review endpoint

**Why:** `POST /knowledge/{tenant_id}/{node_id}/review` calls `brain.approve` directly with no `AuthGate`, no signoff-window check, and a caller-supplied `by` string. It is a complete bypass of every gate `SeniorService.review` enforces. `AuthGate` already exists and is already applied to the billing and retention routes — this endpoint simply never got it.

**Files:**
- Modify: `analytics_platform/api.py:710-723`
- Test: `tests/test_brain_governance.py` (append)

**Interfaces:**
- Consumes: `AuthGate.require`, `Role`, `AuthError` (all existing in `analytics_platform/auth.py`), and the `_unauth` helper at `api.py:1026`.
- Produces: the endpoint gains an `authorization: Optional[str] = Header(default=None)` parameter and rejects unauthenticated callers with 401 when auth is enabled. `by` is taken from the verified principal, not the request body.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_brain_governance.py`:

```python
class ReviewEndpointAuthTest(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        from fastapi.testclient import TestClient
        from analytics_platform.api import build_app, make_context
        from analytics_platform.config import Settings

        self._tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(db_path=os.path.join(self._tmp.name, "t.db"),
                                 auth_enabled=True, auth_secret="test-secret",
                                 embedding_enabled=False)
        self.ctx = make_context(self.settings)
        self.ctx.tenants.create("t1", name="T1")
        self.client = TestClient(build_app(self.ctx))

        brain = self.ctx.pipeline.brain("t1")
        node = brain.create(NodeKind.METRIC, "Gross margin", summary="Revenue minus COGS")
        brain.submit(node.id, by="junior")
        self.node_id = node.id

    def tearDown(self):
        self.ctx.store.close()
        self._tmp.cleanup()

    def _token(self, role: str) -> str:
        from analytics_platform.auth import issue
        return issue(self.settings.auth_secret, "t1", role, sub="tester")

    def test_unauthenticated_approval_is_rejected(self):
        r = self.client.post(f"/knowledge/t1/{self.node_id}/review",
                             json={"action": "approve", "by": "definitely-a-human"})
        self.assertEqual(r.status_code, 401)

    def test_node_is_not_approved_after_a_rejected_call(self):
        self.client.post(f"/knowledge/t1/{self.node_id}/review",
                         json={"action": "approve", "by": "definitely-a-human"})
        node = self.ctx.pipeline.brain("t1").get(self.node_id)
        self.assertEqual(node.status, ReviewStatus.UNDER_REVIEW)

    def test_authenticated_reviewer_can_approve(self):
        r = self.client.post(
            f"/knowledge/t1/{self.node_id}/review",
            json={"action": "approve"},
            headers={"Authorization": f"Bearer {self._token('tenant_admin')}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], ReviewStatus.APPROVED.value)

    def test_reviewer_identity_comes_from_the_token_not_the_body(self):
        self.client.post(
            f"/knowledge/t1/{self.node_id}/review",
            json={"action": "approve", "by": "someone-else"},
            headers={"Authorization": f"Bearer {self._token('tenant_admin')}"})
        node = self.ctx.pipeline.brain("t1").get(self.node_id)
        self.assertNotEqual(node.reviewed_by, "someone-else")

    def test_a_token_for_another_tenant_is_rejected(self):
        from analytics_platform.auth import issue
        other = issue(self.settings.auth_secret, "t2", "tenant_admin", sub="tester")
        r = self.client.post(f"/knowledge/t1/{self.node_id}/review",
                             json={"action": "approve"},
                             headers={"Authorization": f"Bearer {other}"})
        self.assertIn(r.status_code, (401, 403))
```

If `build_app` has a different name or signature in `api.py`, read the file and use the real one — do not change the assertions.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_brain_governance.py::ReviewEndpointAuthTest -v`
Expected: FAIL — the unauthenticated call returns 200 and the node reaches `APPROVED`.

- [ ] **Step 3: Guard the endpoint**

In `analytics_platform/api.py`, replace the `review` endpoint (lines 710-723) with:

```python
    @app.post("/knowledge/{tenant_id}/{node_id}/review")
    def review(tenant_id: str, node_id: str, body: ReviewIn,
               authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        tenant_or_404(tenant_id)
        try:
            principal = C.auth.require(authorization, tenant_id,
                                       [Role.OWNER, Role.TENANT_ADMIN, Role.ANALYST])
        except AuthError as e:
            _unauth(e)

        brain = C.pipeline.brain(tenant_id)
        actions = {"approve": brain.approve, "approve_with_caveats": brain.approve_with_caveats,
                   "reject": brain.reject, "revise": brain.revise,
                   "submit": brain.submit, "stale": brain.mark_stale}
        fn = actions.get(body.action)
        if fn is None:
            raise HTTPException(400, f"Unknown action {body.action}")

        # Identity comes from the verified principal. A caller-supplied `by` would
        # make "a human approved this" unfalsifiable.
        reviewer = principal.get("sub") or principal.get("role") or "unknown"
        node = fn(node_id, by=reviewer, notes=body.notes)
        C.observability.event(tenant_id=tenant_id, stage=f"knowledge.{body.action}",
                              actor=reviewer, resource=node_id, status="OK")
        return node.to_dict()
```

Mark `ReviewIn.by` as ignored so the API contract stays honest — in the model at `api.py:110`:

```python
class ReviewIn(BaseModel):
    action: str = "approve"     # approve | approve_with_caveats | reject | revise | submit | stale
    by: str = "senior"          # DEPRECATED: ignored; the reviewer comes from the auth principal
    notes: str = ""
```

Confirm `AuthError` and `Role` are imported at `api.py:43` (they are) and that `Header` is imported from `fastapi`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_brain_governance.py::ReviewEndpointAuthTest -v`
Expected: 5 passed.

- [ ] **Step 5: Audit the sibling endpoints**

Run: `grep -n "@app.post" analytics_platform/api.py | grep -iE "review|promote|approve"`

For each result, confirm it either goes through `SeniorService.review` (already gated) or has an `AuthGate.require` call. Add the same guard to any that does not, and add one test per endpoint mirroring `test_unauthenticated_approval_is_rejected`.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: no new failures. `tests/test_governance_auth.py` and `tests/test_api.py` are the likely places to need updates — tests that posted reviews without a token now need one, or need `auth_enabled=False`.

- [ ] **Step 7: Commit**

```bash
git add analytics_platform/api.py tests/test_brain_governance.py
git commit -m "fix(governance): authenticate the knowledge review endpoint

The reviewer is now taken from the verified principal rather than a request-body
string, so approval provenance is no longer self-asserted."
```

---

### Task 3: The AI senior's verdict decides the outcome

**Why:** `run_senior_review` prompts an LLM for review feedback, then calls `self.review(..., action="approve", ...)` unconditionally. The model's assessment lands in `notes` and changes nothing. A step that looks like review and always approves is worse than no step: it produces an audit trail that implies scrutiny that did not happen. Separately, the SQL auto-approval block re-approves already-approved nodes, which raises `BrainConflict` on the second approval of the same query.

**Files:**
- Modify: `analytics_platform/senior.py:195-205`, `analytics_platform/senior.py:223-257`
- Test: `tests/test_brain_governance.py` (append)

**Interfaces:**
- Consumes: `make_role_client` (existing), `LLMResult` from `analytics_platform.llm`.
- Produces: `SeniorService._parse_verdict(text: str) -> Tuple[str, str]` returning `(action, notes)` where action is one of `approve`, `revise`, `reject`. `run_senior_review` calls `self.review` with the parsed action.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_brain_governance.py`:

```python
class AiSeniorVerdictTest(unittest.TestCase):
    def setUp(self):
        from analytics_platform.senior import SeniorService
        self.ctx = make_ctx(_WAREHOUSE)
        self.ctx.tenants.create("t1", name="T1")
        self.senior = SeniorService(self.ctx.store, pipeline=self.ctx.pipeline,
                                    tenants=self.ctx.tenants, observability=self.ctx.obs,
                                    settings=self.ctx.settings)

    def tearDown(self):
        self.ctx.close()

    def test_approve_verdict_is_parsed(self):
        action, notes = self.senior._parse_verdict(
            '{"verdict": "approve", "reasoning": "SQL matches the question."}')
        self.assertEqual(action, "approve")
        self.assertIn("SQL matches", notes)

    def test_reject_verdict_is_parsed(self):
        action, _ = self.senior._parse_verdict(
            '{"verdict": "reject", "reasoning": "The join drops rows."}')
        self.assertEqual(action, "reject")

    def test_revise_verdict_is_parsed(self):
        action, _ = self.senior._parse_verdict('{"verdict": "revise", "reasoning": "x"}')
        self.assertEqual(action, "revise")

    def test_fenced_json_is_parsed(self):
        action, _ = self.senior._parse_verdict(
            '```json\n{"verdict": "reject", "reasoning": "bad"}\n```')
        self.assertEqual(action, "reject")

    def test_unparseable_output_defaults_to_revise_not_approve(self):
        """An unreadable verdict must never become an approval."""
        action, notes = self.senior._parse_verdict("I think it looks fine, roughly?")
        self.assertEqual(action, "revise")
        self.assertIn("could not be parsed", notes.lower())

    def test_empty_output_defaults_to_revise(self):
        self.assertEqual(self.senior._parse_verdict("")[0], "revise")

    def test_unknown_verdict_value_defaults_to_revise(self):
        action, _ = self.senior._parse_verdict('{"verdict": "looks-good", "reasoning": "x"}')
        self.assertEqual(action, "revise")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_brain_governance.py::AiSeniorVerdictTest -v`
Expected: FAIL — `'SeniorService' object has no attribute '_parse_verdict'`.

- [ ] **Step 3: Add verdict parsing**

Add to `SeniorService` in `analytics_platform/senior.py`:

```python
    _VALID_VERDICTS = ("approve", "revise", "reject")

    def _parse_verdict(self, text: str) -> Tuple[str, str]:
        """(action, notes) from an LLM review. Anything unreadable means 'revise'.

        Defaulting to approve on a parse failure would make the review step a
        rubber stamp, which is the defect this replaces.
        """
        raw = (text or "").strip()
        if not raw:
            return "revise", "AI review returned no output; verdict could not be parsed."

        body = raw
        if "```json" in body:
            body = body.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in body:
            body = body.split("```", 1)[1].split("```", 1)[0]

        try:
            parsed = json.loads(body.strip())
            verdict = str(parsed.get("verdict", "")).strip().lower()
            reasoning = str(parsed.get("reasoning", "")).strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI senior verdict could not be parsed: %s", exc)
            return "revise", (f"AI review output could not be parsed as a verdict; "
                              f"routed to human review. Raw output: {raw[:500]}")

        if verdict not in self._VALID_VERDICTS:
            logger.warning("AI senior returned unknown verdict %r", verdict)
            return "revise", (f"AI review returned an unrecognised verdict {verdict!r}; "
                              f"verdict could not be parsed. Reasoning: {reasoning[:500]}")
        return verdict, reasoning
```

Add at the top of `senior.py` if absent:

```python
import json
import logging
from typing import Tuple

logger = logging.getLogger(__name__)
```

- [ ] **Step 4: Make the verdict drive the decision**

In `run_senior_review`, replace the prompt and the final `return` with:

```python
        llm = make_role_client(self.settings, cfg.senior)
        prompt = (
            f"Review the following analysis run for tenant {tenant_id}.\n"
            f"Question: {run_doc.get('question_text', '')}\n"
            f"SQL: {run_doc.get('sql', '')}\n"
            f"Answer: {run_doc.get('answer', '')}\n\n"
            "Decide whether this analysis should become approved company knowledge.\n"
            "Respond with JSON only:\n"
            '{"verdict": "approve" | "revise" | "reject", "reasoning": "<one paragraph>"}\n'
            "Choose 'approve' only if the SQL answers the question asked, the answer "
            "follows from the data, and no caveat is material. Choose 'revise' if it is "
            "close but incomplete. Choose 'reject' if the logic is wrong."
        )
        try:
            res = llm.generate(
                prompt=prompt,
                system_prompt="You are a senior data analyst reviewing a junior "
                              "analyst's work. You are the last gate before this "
                              "becomes company fact. Respond with JSON only.",
                temperature=0.2,
            )
            action, notes = self._parse_verdict(res.text if res else "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI senior review call failed for run %s: %s", run_id, exc)
            action, notes = "revise", f"AI review call failed: {exc}"

        self.obs.event(tenant_id=tenant_id, stage="senior.ai_verdict", actor="ai",
                       resource=run_id, status="OK", meta={"verdict": action})
        return self.review(tenant_id, run_id, action=action, by="ai", notes=notes)
```

- [ ] **Step 5: Make the SQL auto-approval idempotent**

In `review()`, replace the auto-approval loop (lines 195-205) with:

```python
            if run.sql:
                from .brain.ingest import ingest_sql
                b = self.pipeline.brain(tenant_id)
                nodes = ingest_sql(b, run.sql, source_ref=run.id,
                                   title=f"Query from approved analysis {run_id[:8]}",
                                   created_by=by)
                for n in nodes:
                    # Re-approving an already-approved node raises BrainConflict:
                    # APPROVED -> APPROVED is not a legal transition.
                    if n.status in (ReviewStatus.APPROVED,
                                    ReviewStatus.APPROVED_WITH_CAVEATS):
                        continue
                    try:
                        if n.status == ReviewStatus.CANDIDATE:
                            b.transition(n.id, ReviewStatus.UNDER_REVIEW, by=by,
                                         notes="auto-transition from analysis review")
                        b.approve(n.id, by=by, notes="auto-approved from analysis review")
                    except BrainConflict as exc:
                        logger.warning("could not auto-approve %s from run %s: %s",
                                       n.id, run_id, exc)
```

Import `BrainConflict` at the top of `senior.py`:

```python
from .brain.store import BrainConflict
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_brain_governance.py -v`
Expected: all pass (5 autopromote + 5 endpoint auth + 7 verdict).

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: `tests/test_senior_depth.py` may assert that an AI review approves — with a null LLM provider, `_parse_verdict("")` now yields `revise`. That is the correct new behaviour; update the assertion and comment the change.

- [ ] **Step 8: Commit**

```bash
git add analytics_platform/senior.py tests/test_brain_governance.py
git commit -m "fix(governance): the AI senior's verdict now decides the review

Previously the LLM's assessment was recorded as notes while the code approved
unconditionally. Unparseable verdicts route to human review, never to approval."
```

---

### Task 4: Repair the dead write paths

**Why:** Two ingestion routes reference methods that do not exist. `onboarding.bulk_ingest_json` calls `brain.save(node)` (no such method), sets `node.description` (no such field), and calls `KnowledgeNode.definition(...)` (no such classmethod) — it raises `AttributeError` on any input. `anomaly.evaluate_kpis` calls `pipeline.ingest(...)` (no such method) inside `except Exception: pass`, so proactive KPI monitoring has silently never fired. Its brain-derived KPI list also omits the `threshold` key its own loop requires, so even a repaired call would skip every KPI from that source.

**Files:**
- Modify: `analytics_platform/onboarding.py:77-135`, `analytics_platform/anomaly.py:36-75`
- Test: `tests/test_dead_write_paths.py`

**Interfaces:**
- Consumes: `CompanyBrain.create` / `add_node` (existing), `Pipeline.run` (existing, `pipeline.py:53`).
- Produces: `bulk_ingest_json` returns `{"created_nodes_count": int}` with nodes at `CANDIDATE`. `evaluate_kpis` triggers a real `Pipeline.run` per breached KPI and returns the trigger count.

- [ ] **Step 1: Write the failing test**

Create `tests/test_dead_write_paths.py`:

```python
"""The three write paths that referenced methods which do not exist."""
from __future__ import annotations

import json
import unittest

import pandas as pd

from analytics_platform.domain import ReviewStatus
from tests.helpers import make_ctx

_WAREHOUSE = {"orders": pd.DataFrame({"id": [1, 2, 3], "amount": [10, 20, 30]})}


class _StubLLM:
    """Returns the shape bulk_ingest_json expects, without a network call."""

    def generate_json(self, prompt: str):
        return {"title": "Order count", "description": "Total orders placed",
                "definitions": [{"name": "Order", "description": "A placed order"}]}


class BulkIngestJsonTest(unittest.TestCase):
    def setUp(self):
        from analytics_platform.onboarding import OnboardingService
        self.ctx = make_ctx(_WAREHOUSE)
        self.ctx.tenants.create("t1", name="T1")
        self.svc = OnboardingService(self.ctx.store, tenants=self.ctx.tenants,
                                     pipeline=self.ctx.pipeline, observability=self.ctx.obs)
        self.payload = json.dumps([
            {"sql": "SELECT COUNT(*) FROM orders", "dashboard": "Ops", "purpose": "volume"}])

    def tearDown(self):
        self.ctx.close()

    def test_does_not_raise(self):
        result = self.svc.bulk_ingest_json("t1", self.payload, _StubLLM())
        self.assertNotIn("error", result)

    def test_creates_nodes(self):
        result = self.svc.bulk_ingest_json("t1", self.payload, _StubLLM())
        self.assertGreater(result["created_nodes_count"], 0)

    def test_nodes_land_as_candidates_not_approved(self):
        self.svc.bulk_ingest_json("t1", self.payload, _StubLLM())
        approved = self.ctx.store.query_all(
            "SELECT id FROM knowledge_nodes WHERE tenant_id=? AND status=?",
            ("t1", ReviewStatus.APPROVED.value))
        self.assertEqual(approved, [])

    def test_invalid_json_returns_an_error_not_a_crash(self):
        self.assertIn("error", self.svc.bulk_ingest_json("t1", "not json", _StubLLM()))

    def test_non_array_json_returns_an_error(self):
        self.assertIn("error", self.svc.bulk_ingest_json("t1", '{"a": 1}', _StubLLM()))

    def test_items_without_sql_are_skipped(self):
        result = self.svc.bulk_ingest_json("t1", json.dumps([{"dashboard": "X"}]), _StubLLM())
        self.assertEqual(result["created_nodes_count"], 0)


class EvaluateKpisTest(unittest.TestCase):
    def setUp(self):
        from analytics_platform.anomaly import AnomalyService
        self.ctx = make_ctx(_WAREHOUSE)
        self.ctx.tenants.create("t1", name="T1")
        self.svc = AnomalyService(self.ctx.store, observability=self.ctx.obs)

    def tearDown(self):
        self.ctx.close()

    def test_breached_threshold_triggers_a_run(self):
        self.svc.create_kpi("t1", name="Order count",
                            sql_query="SELECT COUNT(*) FROM orders", threshold="< 100")
        triggered = self.svc.evaluate_kpis("t1", self.ctx.executor, self.ctx.pipeline)
        self.assertEqual(triggered, 1)

    def test_a_triggered_run_is_recorded(self):
        self.svc.create_kpi("t1", name="Order count",
                            sql_query="SELECT COUNT(*) FROM orders", threshold="< 100")
        self.svc.evaluate_kpis("t1", self.ctx.executor, self.ctx.pipeline)
        runs = self.ctx.store.query_all(
            "SELECT id FROM analysis_runs WHERE tenant_id=?", ("t1",))
        self.assertGreater(len(runs), 0)

    def test_unbreached_threshold_triggers_nothing(self):
        self.svc.create_kpi("t1", name="Order count",
                            sql_query="SELECT COUNT(*) FROM orders", threshold="> 100")
        self.assertEqual(
            self.svc.evaluate_kpis("t1", self.ctx.executor, self.ctx.pipeline), 0)

    def test_a_failing_kpi_is_logged_not_swallowed(self):
        import logging
        self.svc.create_kpi("t1", name="Broken", sql_query="SELECT * FROM nope",
                            threshold="< 100")
        with self.assertLogs("analytics_platform.anomaly", level=logging.WARNING):
            self.svc.evaluate_kpis("t1", self.ctx.executor, self.ctx.pipeline)


if __name__ == "__main__":
    unittest.main()
```

If `AnomalyService.create_kpi` has a different signature, read `analytics_platform/anomaly.py` and adapt the calls — do not change the assertions.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_dead_write_paths.py -v`
Expected: `BulkIngestJsonTest` fails with `AttributeError: 'CompanyBrain' object has no attribute 'save'`; `EvaluateKpisTest` fails with `triggered == 0` because `pipeline.ingest` raises into a bare `except`.

- [ ] **Step 3: Repair bulk_ingest_json**

In `analytics_platform/onboarding.py`, replace the node-creation block inside the item loop:

```python
            # Create QUERY node using AST parser
            nodes = ingest_sql(brain, sql, source_ref=dashboard, title=title, created_by=by)
            for node in nodes:
                node.description = desc
                node.payload["purpose"] = purpose
                brain.save(node)
                created_nodes.append(node)

            # Create DEFINITION nodes from LLM
            defs = res.get("definitions", [])
            for d in defs:
                d_name = d.get("name", "Unknown")
                d_desc = d.get("description", "")
                def_node = KnowledgeNode.definition(d_name, d_desc, created_by=by)
                def_node.source_ref = dashboard
                brain.save(def_node)
                created_nodes.append(def_node)
```

with:

```python
            # ingest_sql already persists the QUERY/DEFINITION nodes it derives from
            # the AST; enrich them in place rather than re-saving.
            nodes = ingest_sql(brain, sql, source_ref=dashboard, title=title,
                               created_by=by)
            for node in nodes:
                brain.update_field(node.id, "summary", desc or node.summary)
                payload = dict(node.payload or {})
                payload["purpose"] = purpose
                brain.update_field(node.id, "payload", dump_json(payload))
                created_nodes.append(node)

            # DEFINITION nodes named by the LLM. CANDIDATE only — an LLM's reading
            # of a legacy dashboard is a proposal, not company fact.
            for d in res.get("definitions", []) or []:
                name = (d.get("name") or "").strip()
                if not name:
                    continue
                def_node = brain.create(
                    NodeKind.DEFINITION, title=name,
                    summary=(d.get("description") or "").strip(),
                    source_ref=dashboard, created_by=by,
                    status=ReviewStatus.CANDIDATE)
                created_nodes.append(def_node)
```

Update the imports at the top of `onboarding.py`:

```python
from .database import dump_json
from .domain import NodeKind, ReviewStatus
```

Remove the now-unused `from .domain import KnowledgeNode` inside the method.

- [ ] **Step 4: Repair evaluate_kpis**

In `analytics_platform/anomaly.py`, replace the `pipeline.ingest([...])` call and the bare `except` with:

```python
                if res and res.ok and res.data is not None and not res.data.empty:
                    val = res.data.iloc[0, 0]
                    if self._evaluate_threshold(val, k["threshold"]):
                        question = (f"KPI '{k['name']}' breached its threshold: "
                                    f"value {val} against {k['threshold']}. "
                                    f"What is driving this?")
                        pipeline.run(tenant_id, question, mode_budget="low_cost")
                        self.obs.event(tenant_id=tenant_id, stage="anomaly.triggered",
                                       actor="anomaly", resource=k["name"], status="OK",
                                       meta={"value": str(val),
                                             "threshold": k["threshold"]})
                        triggered += 1
            except Exception as exc:  # noqa: BLE001 - one bad KPI must not stop the rest
                logger.warning("KPI %r evaluation failed for tenant %s: %s",
                               k.get("name"), tenant_id, exc, exc_info=True)
                self.obs.event(tenant_id=tenant_id, stage="anomaly.kpi_failed",
                               actor="anomaly", resource=str(k.get("name")),
                               status="ERROR", meta={"error": str(exc)[:200]})
```

Add at the top of `anomaly.py`:

```python
import logging

logger = logging.getLogger(__name__)
```

- [ ] **Step 5: Give brain-derived KPIs a threshold**

The brain-derived branch of `list_kpis` builds `{"name", "description", "sql_query"}` with no `threshold` and no `is_active`, so the loop's first guard skips every one of them. Make that explicit rather than accidental — in `list_kpis`, replace the append:

```python
                out.append({"name": n.title, "description": n.summary or "", "sql_query": sql})
```

with:

```python
                # Brain-derived KPIs have no configured threshold, so they are
                # listed for display but never evaluated. Registering one for
                # monitoring is an explicit action via create_kpi().
                out.append({"name": n.title, "description": n.summary or "",
                            "sql_query": sql, "threshold": "", "is_active": 0,
                            "source": "brain"})
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_dead_write_paths.py -v`
Expected: 10 passed.

- [ ] **Step 7: Sweep for other references to methods that do not exist**

```bash
.venv/bin/python - <<'PY'
import ast, pathlib
KNOWN = {"CompanyBrain", "Pipeline", "SeniorService", "OnboardingService",
         "AnomalyService", "StakeholderService", "JuniorEngine", "JuniorWorker"}
defined = {}
for p in pathlib.Path("analytics_platform").rglob("*.py"):
    tree = ast.parse(p.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in KNOWN:
            defined[node.name] = {n.name for n in node.body
                                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
for name, methods in sorted(defined.items()):
    print(f"{name}: {len(methods)} methods")
    print("  ", ", ".join(sorted(methods)))
PY
```

Cross-check each attribute call on these objects against the printed lists. Any call to a name not listed is another dead path — fix it or open a follow-up.

- [ ] **Step 8: Run the full suite and commit**

Run: `.venv/bin/python -m pytest tests/ -q`

```bash
git add analytics_platform/ tests/test_dead_write_paths.py
git commit -m "fix(governance): repair bulk_ingest_json and evaluate_kpis

Both called methods that do not exist; one crashed, the other failed into a bare
except so proactive KPI monitoring silently never ran."
```

---

### Task 5: Record what remains hardcoded

**Why:** `AGENTS.md` Part 1 §1 requires a `HARDCODED_REGISTRY.md` documenting every deliberate hardcoded value. It does not exist, so the tenant-specific and structural constants found in the evaluation have no paper trail.

**Files:**
- Create: `HARDCODED_REGISTRY.md`

- [ ] **Step 1: Create the registry**

Create `HARDCODED_REGISTRY.md`:

```markdown
# Hardcoded Registry

Per `AGENTS.md` Part 1 §1: any value hardcoded for temporary or structural reasons
is recorded here with **what**, **where**, and **why**. Remove the entry when the
value becomes configurable or the code is deleted.

| What | Where | Why | Exit condition |
|---|---|---|---|
| RRF constant `k = 60` | `analytics_platform/brain/fusion.py` | Standard value from the RRF literature; tuning it needs a labelled relevance set we do not have. | Expose as a setting once retrieval quality is measured. |
| Confidence ranking uses only `review` and `freshness` | `analytics_platform/brain/fusion.py` | The other four dimensions are not yet computed (see the frameworks plan). | Widen when `evidence` and `data_quality` are scored. |
| Lexical stopword list | `analytics_platform/brain/text.py` | English-only. Adequate for current tenants. | Replace with a per-tenant list when a non-English tenant onboards. |
| Skills base path `.agents/skills` | `analytics_platform/stakeholder.py` | Directory convention, resolved relative to the repo root. | None — this is the intended contract. |
| Funnel skill SQL bound to one tenant's schema | `.agents/skills/funnel-conversion-analysis/references/` | Pre-dates the skill data-contract split. **Violates `AGENTS.md` Part 1 §2.** | Removed by the skills-portability plan. |
| Default embedding model `BAAI/bge-small-en-v1.5` | `analytics_platform/config.py` | A default, not a hardcode — overridable via `Settings.embedding_model`. | None. |
```

- [ ] **Step 2: Commit**

```bash
git add HARDCODED_REGISTRY.md
git commit -m "docs: add HARDCODED_REGISTRY.md required by AGENTS.md"
```

---

## Verification

- [ ] `.venv/bin/python -m pytest tests/ -q` — all green
- [ ] A fresh tenant's junior run produces an `UNDER_REVIEW` finding, never `APPROVED`
- [ ] `POST /knowledge/{t}/{n}/review` without a token returns 401 when auth is enabled
- [ ] An AI senior review of a run with a null LLM provider results in `revise`, not `approve`
- [ ] `grep -rn "except Exception:" analytics_platform/anomaly.py analytics_platform/onboarding.py` — every hit is followed by a `logger` call, never a bare `pass`
