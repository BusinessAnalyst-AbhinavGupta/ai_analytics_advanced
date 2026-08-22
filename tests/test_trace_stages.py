"""Stage attribution, and the step that was missing from the trail.

`_extract_search_intent` and `_retrieve` run before the pipeline emits its first
event, so the two operations most worth seeing -- the question rewrite and the
brain search -- were invisible in the live trail and would have been
`unattributed` in the trace. The `recalling` step fixes both, and it goes first
because it runs first.
"""
from __future__ import annotations

import json

from analytics_platform.domain import PIPELINE_STEPS, STEP_LABELS

from tests.test_answer_stream import _StreamCase
from tests.test_extract_flow import CUBE_1, NARRATIVE, PY_CELL, SequencedLLM


class RecallingStepTest(_StreamCase):
    def test_recalling_is_the_first_pipeline_step(self):
        self.assertEqual(PIPELINE_STEPS[0], "recalling")
        self.assertIn("recalling", STEP_LABELS)

    def test_the_stream_emits_recalling_before_understanding(self):
        self.approve_base()
        events = self.first_turn_stream()
        steps = [s["step"] for s in self.steps(events)]
        self.assertIn("recalling", steps)
        self.assertLess(steps.index("recalling"), steps.index("understanding"))

    def test_recalling_detail_names_the_intent_and_the_node_counts(self):
        detail = self.svc._recalling_detail("session conversion", [1, 2], [3])
        self.assertIn("session conversion", detail)
        self.assertIn("2", detail)
        self.assertIn("1", detail)


class StageAttributionTest(_StreamCase):
    def traces(self):
        store = self.ctx.stores.for_tenant(self.tid)
        return [(r["stage"], r["kind"], json.loads(r["payload"]))
                for r in store.query_all(
                    "SELECT stage, kind, payload FROM llm_traces ORDER BY seq")]

    def test_a_turn_writes_llm_traces_under_named_stages(self):
        self.approve_base()
        self.first_turn()
        stages = {stage for stage, kind, _ in self.traces() if kind == "llm"}
        self.assertTrue(stages, "a turn wrote no llm traces at all")
        self.assertIn("planning", stages)
        self.assertNotIn("unattributed", stages)

    def test_the_search_intent_rewrite_is_recorded(self):
        """The string that was thrown away one line after it was produced."""
        self.approve_base()
        self.first_turn()
        recalling = [p for stage, kind, p in self.traces()
                     if stage == "recalling" and kind == "llm"]
        self.assertTrue(recalling)
        self.assertEqual(recalling[0]["response_text"], "sales by country")

    def test_traces_carry_this_turns_trace_id(self):
        self.approve_base()
        out, _ = self.first_turn()
        store = self.ctx.stores.for_tenant(self.tid)
        answer = store.query_one(
            "SELECT trace_id FROM stakeholder_answers WHERE id=?", (out["answer_id"],))
        traced = store.query_all("SELECT DISTINCT trace_id FROM llm_traces")
        self.assertEqual([r["trace_id"] for r in traced], [answer["trace_id"]])

    def test_a_sink_that_cannot_write_still_answers_the_turn(self):
        """The constraint the whole feature is subordinate to. A trace is never
        worth an answer, so a sink that throws on every insert must cost the
        records and nothing else."""
        import analytics_platform.tracing as tracing_mod

        class _Exploding:
            def execute(self, *a, **kw):
                raise RuntimeError("disk gone")

        real_sink = tracing_mod.TraceSink
        tracing_mod.TraceSink = lambda store, tid, trace, **kw: real_sink(
            _Exploding(), tid, trace, **kw)
        try:
            self.approve_base()
            out, _ = self.first_turn()
        finally:
            tracing_mod.TraceSink = real_sink
        self.assertEqual(out["status"], "ANSWERED")
