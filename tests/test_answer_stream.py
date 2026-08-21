"""Plan B Task 1 -- the analyst pipeline observed as a stream of named steps.

Two things are being pinned here, and they pull in opposite directions.

The first is that `answer()` did not change. Plan B's whole safety argument is
that `answer_stream()` became the single implementation and `answer()` became a
wrapper that drains it, so every Plan A caller and test is untouched. If
`test_answer_still_returns_the_same_dict` ever fails, the refactor was wrong and
no amount of nice streaming makes up for it.

The second is that the steps are *honest*. A step trail that says "Analysing"
and nothing else is a spinner wearing a costume. The `detail` assertions below
are the real subject of this file: the workspace step has to say which dataset
it reused, and the retrieving step has to say it was skipped and why. Those two
sentences are Plan A's entire value proposition made visible, so they are tested
as behaviour rather than as decoration.

A note on failure: an exception inside the pipeline still propagates out of
`answer_stream()`, exactly as it always propagated out of `answer()`. Turning a
crash into a tidy terminal event is the streaming *route's* job (Task 2), not
the service's -- swallowing it here would change `answer()`'s contract and hide
real bugs behind a friendly-looking answer.
"""
from __future__ import annotations

from analytics_platform.domain import PIPELINE_STEPS

from tests.test_extract_flow import CUBE_1, NARRATIVE, PY_CELL, SequencedLLM, _FlowCase, _llm_patched


REUSE_PLAN = ('{"base_view":"checkout_sessions","cube":{"dimensions":["device"],'
              '"measures":[{"name":"revenue","expr":"SUM(revenue)"}],"filters":{}},'
              '"analysis":"workspace_sql"}')
REUSE_SQL = "```sql\nSELECT device, SUM(revenue) AS r FROM df_1 GROUP BY device\n```"


class _StreamCase(_FlowCase):
    """The extract-flow harness, driven through answer_stream instead of answer."""

    def stream(self, llm, question, conversation_id=None):
        self.svc.llm = llm
        with _llm_patched(self.svc, llm):
            return list(self.svc.answer_stream(
                self.tid, question, conversation_id=conversation_id or self.c1))

    def first_turn_stream(self, conversation_id=None):
        llm = SequencedLLM(["sales by country", CUBE_1, PY_CELL, NARRATIVE])
        return self.stream(llm, "what are sales by country?", conversation_id)

    @staticmethod
    def steps(events, name=None):
        out = [e["payload"] for e in events if e["type"] == "step"]
        return [s for s in out if s["step"] == name] if name else out


class TestCompatibility(_StreamCase):
    def test_answer_still_returns_the_same_dict(self):
        """The compatibility guarantee. If this breaks, every Plan A test breaks."""
        self.approve_base()
        out, _ = self.first_turn()
        self.assertTrue(out["answer"])
        self.assertTrue(out["answer_id"])
        self.assertIn("analysis", out)
        self.assertIn("extract_meta", out)

    def test_answer_and_answer_stream_agree_on_the_payload(self):
        """One implementation, not two. The wrapper must not add or drop keys."""
        self.approve_base()
        blocking, _ = self.first_turn(conversation_id=self.c1)
        streamed = self.first_turn_stream(conversation_id=self.c2)[-1]["payload"]
        self.assertEqual(sorted(blocking.keys()), sorted(streamed.keys()))
        self.assertEqual(blocking["answer_mode"], streamed["answer_mode"])
        self.assertEqual(blocking["analysis"]["base_view"],
                         streamed["analysis"]["base_view"])


class TestStreamShape(_StreamCase):
    def test_stream_emits_steps_then_exactly_one_answer(self):
        self.approve_base()
        evs = self.first_turn_stream()
        self.assertEqual(evs[-1]["type"], "answer")
        self.assertEqual([e["type"] for e in evs].count("answer"), 1)
        self.assertTrue(any(e["type"] == "step" for e in evs[:-1]))

    def test_steps_arrive_in_pipeline_order(self):
        self.approve_base()
        names = [s["step"] for s in self.steps(self.first_turn_stream())]
        order = [PIPELINE_STEPS.index(n) for n in names]
        self.assertEqual(order, sorted(order))

    def test_every_step_is_a_known_pipeline_step(self):
        self.approve_base()
        for s in self.steps(self.first_turn_stream()):
            self.assertIn(s["step"], PIPELINE_STEPS)
            self.assertIn(s["state"], ("start", "done", "skipped"))
            self.assertTrue(s["label"], f"{s['step']} has no human-facing label")

    def test_steps_carry_elapsed_time(self):
        self.approve_base()
        done = [s for s in self.steps(self.first_turn_stream()) if s["state"] == "done"]
        self.assertTrue(done)
        self.assertTrue(all(s["elapsed_ms"] >= 0 for s in done))

    def test_a_turn_that_never_reaches_the_pipeline_still_terminates(self):
        """Tenant disabled: answer() returns early, so the stream is one event.
        A client that waits for a step before an answer would hang here."""
        self.ctx.tenants.set_analyst_config(self.tid, {"stakeholder": {"enabled": False}})
        evs = list(self.svc.answer_stream(self.tid, "anything", conversation_id=self.c1))
        self.assertEqual(evs[-1]["type"], "answer")
        self.assertEqual([e["type"] for e in evs].count("answer"), 1)


class TestStepDetail(_StreamCase):
    """`detail` is the whole point -- see the module docstring."""

    def test_the_workspace_step_detail_is_the_coverage_reason(self):
        self.approve_base()
        self.first_turn_stream()
        evs = self.stream(
            SequencedLLM(["device breakdown", REUSE_PLAN, REUSE_SQL, "iOS leads."]),
            "break that down by device")
        ws = [s for s in self.steps(evs, "checking_workspace") if s["state"] == "done"]
        self.assertTrue(ws, "no checking_workspace step was emitted")
        self.assertIn("df_1", ws[-1]["detail"])

    def test_a_reuse_turn_marks_retrieving_as_skipped(self):
        """Plan A's headline behaviour, now visible: no warehouse query, and the
        UI can say so rather than showing an empty step."""
        self.approve_base()
        self.first_turn_stream()
        before = len(self.spy.all_sql)
        evs = self.stream(
            SequencedLLM(["device breakdown", REUSE_PLAN, REUSE_SQL, "iOS leads."]),
            "break that down by device")
        retrieving = self.steps(evs, "retrieving")
        self.assertTrue(retrieving, "no retrieving step was emitted")
        self.assertEqual(retrieving[-1]["state"], "skipped")
        self.assertTrue(retrieving[-1]["detail"],
                        "a skipped step with no reason is worse than no step")
        self.assertEqual(evs[-1]["payload"]["queries_run"], [])
        self.assertEqual(len(self.spy.all_sql), before)

    def test_a_retrieve_turn_reports_the_row_count(self):
        self.approve_base()
        evs = self.first_turn_stream()
        done = [s for s in self.steps(evs, "retrieving") if s["state"] == "done"]
        self.assertTrue(done)
        self.assertIn("rows", done[-1]["detail"])

    def test_the_analysing_step_says_where_the_work_ran(self):
        """Athena and DuckDB are different claims about where a number came
        from; the trail must not blur them."""
        self.approve_base()
        self.first_turn_stream()
        evs = self.stream(
            SequencedLLM(["device breakdown", REUSE_PLAN, REUSE_SQL, "iOS leads."]),
            "break that down by device")
        done = [s for s in self.steps(evs, "analysing") if s["state"] == "done"]
        self.assertTrue(done)
        self.assertIn("DuckDB", done[-1]["detail"])


class TestDetailHelpers(_StreamCase):
    """The detail strings are pure functions of what the pipeline computed, so
    they are tested directly -- driving a real turn into an unresolved metric
    would test the semantic layer, not the trail."""

    def test_an_unresolved_metric_shows_up_in_the_understanding_step(self):
        class _Res:
            metrics = ()
            dimensions = ()
            unresolved_terms = ("churn",)

        class _Ctx:
            semantics = _Res()

        detail = self.svc._understanding_detail(_Ctx())
        self.assertIn("churn", detail)

    def test_matched_semantics_are_named(self):
        class _Named:
            def __init__(self, name):
                self.name = name

        class _Res:
            metrics = (_Named("revenue"),)
            dimensions = (_Named("country"),)
            unresolved_terms = ()

        class _Ctx:
            semantics = _Res()

        detail = self.svc._understanding_detail(_Ctx())
        self.assertIn("revenue", detail)
        self.assertIn("country", detail)

    def test_no_semantic_layer_is_not_a_crash(self):
        class _Ctx:
            semantics = None

        self.assertEqual(self.svc._understanding_detail(_Ctx()), "")


class TestFailuresStillTerminate(_StreamCase):
    def test_a_failed_warehouse_still_terminates_with_an_answer_event(self):
        """A handled failure is still an answer. A client that receives steps
        and then nothing cannot tell a crash from a slow warehouse."""
        self.approve_base()
        self.spy.fail_with = "warehouse exploded"
        evs = self.first_turn_stream()
        self.assertEqual(evs[-1]["type"], "answer")
        payload = evs[-1]["payload"]
        self.assertTrue(payload["caveats"] or payload["status"] != "ANSWERED")
