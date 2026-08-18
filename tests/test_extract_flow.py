"""Task 14 -- the analyst pipeline end to end.

Read this before changing an assertion here, because it inverts what the
original bug report implies. The reported failure was "the SQL was a
pre-aggregated answer". Under this design the warehouse SQL *is* aggregated --
it is a GROUP BY cube -- and that is correct. What was actually wrong was never
the GROUP BY; it was that the aggregate was a one-off answer to one question,
authored from scratch, reusable for nothing and reconcilable with nothing. A
cube is aggregated at a dimension set chosen to be REUSED, over a GOVERNED
population, carrying a population_hash.

So there is no test here asserting `"GROUP BY" not in queries_run[0]`. The
assertions that matter are that a second, related question issues ZERO
warehouse queries, and that both answers carry the same population_hash.
"""
from __future__ import annotations

import tempfile
import unittest

import pandas as pd

from analytics_platform.api import create_app
from analytics_platform.domain import AttributionRule, BaseView, DataSourceKind
from analytics_platform.execution.base import QueryResult, SessionStatus
from tests.test_api import app_ctx

CUBE_1 = ('{"base_view":"checkout_sessions",'
          '"cube":{"dimensions":["country","device"],'
          '"measures":[{"name":"revenue","expr":"SUM(revenue)"}],"filters":{}},'
          '"analysis":"python"}')
PY_CELL = "```python\nresult = df_1.groupby('country')['revenue'].sum().to_dict()\n```"
NARRATIVE = "Revenue is concentrated in DE."


class SequencedLLM:
    """Canned responses in call order. Running past the end is an error, so a
    turn that makes an unexpected LLM call fails loudly instead of drifting."""

    name = "gateway"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.prompts = []
        self.system_prompts = []

    def generate(self, prompt="", system_prompt="", **kw):
        from analytics_platform.llm.client import LLMResponse
        self.calls += 1
        self.prompts.append(prompt)
        self.system_prompts.append(system_prompt)
        if not self.responses:
            raise AssertionError(
                f"the LLM was called {self.calls} times; the test allowed fewer")
        return LLMResponse(text=self.responses.pop(0), tokens_in=5, tokens_out=5)

    @property
    def exhausted(self):
        return not self.responses


class CubeExecutor:
    """A warehouse that answers the grain probe and returns a small cube."""

    def __init__(self):
        self.all_sql = []
        self.probe_call_count = 0
        self.probe_rows = 1_200
        self.probe_keys = 1_200
        self.fail_with = ""
        self.fail_cubes_only = ""
        self.frame = pd.DataFrame({
            "country": ["DE", "DE", "US", "US"],
            "device": ["ios", "web", "ios", "web"],
            "revenue": [10.0, 20.0, 30.0, 40.0]})

    def supports(self, ctx):
        return True

    def session_status(self, tenant_id):
        return SessionStatus(state="valid", tenant_id=tenant_id)

    def execute(self, sql, ctx):
        if "key_count" in sql:
            self.probe_call_count += 1
            return QueryResult(ok=True, row_count=1,
                               columns=["row_count", "key_count"],
                               data=pd.DataFrame({"row_count": [self.probe_rows],
                                                  "key_count": [self.probe_keys]}))
        self.all_sql.append(sql)
        if self.fail_with:
            return QueryResult(ok=False, error=self.fail_with)
        if self.fail_cubes_only and "WITH base AS (" in sql:
            return QueryResult(ok=False, error=self.fail_cubes_only)
        df = self.frame.copy()
        return QueryResult(ok=True, data=df, row_count=len(df), columns=list(df.columns))

    def cancel(self, execution_id):
        return True


class _FlowCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ctx, self.base = app_ctx()
        self.ctx.settings.data_dir = self._tmp.name
        self.tid = self.ctx.tenants.create_tenant("FlowCo").id
        self.app = create_app(self.ctx)
        self.svc = self.ctx.stakeholder
        self.spy = CubeExecutor()
        self.svc.executor = self.spy
        # The junior profiles inline during context building, and the cube guard
        # fails closed on an unprofiled dimension -- so the profiler has to see
        # the same warehouse the cube will.
        self.svc.junior.executor = self.spy
        self.ctx.tenants.add_datasource(self.tid, "Orders", DataSourceKind.DIRECT_DB,
                                        dialect="athena", tables=["orders"])
        # Real conversation ids, so a follow-up turn lands in the SAME
        # conversation the first turn's extract was written to -- an unknown id
        # silently starts a new one, and reuse would then never be observable.
        # A question only pulls a table into context when it names the table or
        # one of its columns, so the catalog has to exist before the first turn.
        self.svc.junior.refresh_catalog(self.tid)
        self.c1 = self.svc._ensure_conversation(self.tid, "", "flow")
        self.c2 = self.svc._ensure_conversation(self.tid, "", "flow2")

    def tearDown(self):
        self.svc.workspace.close_all()
        self.base.close()
        self._tmp.cleanup()

    def approve_base(self, view=None):
        view = view or BaseView(
            name="checkout_sessions", grain=["session_id"],
            source_sql="SELECT session_id, country, device, revenue FROM orders "
                       "WHERE is_test_traffic = false",
            dimension_columns=["country", "device"], measure_columns=["revenue"],
            row_count_estimate=1_200)
        node = self.svc.base_views.upsert(self.tid, view, by="senior")
        brain = self.ctx.pipeline.brain(self.tid)
        brain.submit(node.id, by="junior")
        brain.approve(node.id, by="senior")
        return view

    def answer(self, llm, question, conversation_id=None):
        self.svc.llm = llm
        with _llm_patched(self.svc, llm):
            return self.svc.answer(self.tid, question,
                                   conversation_id=conversation_id or self.c1)

    def first_turn(self, conversation_id=None):
        llm = SequencedLLM(["sales by country", CUBE_1, PY_CELL, NARRATIVE])
        return self.answer(llm, "what are sales by country?", conversation_id), llm


class _llm_patched:
    """`answer()` builds its own client from tenant config; swap in the canned one."""

    def __init__(self, svc, llm):
        self.svc, self.llm = svc, llm

    def __enter__(self):
        import analytics_platform.stakeholder as mod
        self._orig = mod.make_role_client
        mod.make_role_client = lambda *a, **k: self.llm
        return self

    def __exit__(self, *exc):
        import analytics_platform.stakeholder as mod
        mod.make_role_client = self._orig
        return False


class TestTheFirstTurn(_FlowCase):
    def test_first_turn_builds_a_cube_and_answers_in_python(self):
        """The trail the user reported missing: a warehouse query AND a Python
        cell. The SQL is aggregated on purpose -- what makes it not the old bug
        is that it is aggregated over a governed population at a dimension set
        chosen to be reused."""
        view = self.approve_base()
        out, _ = self.first_turn()
        self.assertEqual(len(out["queries_run"]), 1)
        self.assertIn("WITH base AS (", out["queries_run"][0])
        self.assertIn(view.source_sql, out["queries_run"][0])
        self.assertEqual(len(out["python_cells"]), 1)
        self.assertEqual(out["produced_df_label"], "df_1")
        self.assertEqual(out["extract_meta"]["dimensions"], ["country", "device"])
        self.assertTrue(out["extract_meta"]["population_hash"])
        self.assertEqual(out["analysis"]["coverage"]["decision"], "retrieve")
        self.assertIs(out["analysis"]["reconcilable"], True)

    def test_no_sql_llm_call_is_made_on_the_cube_path(self):
        """Task 12's contract, observed end to end: the LLM plans, it does not
        write the warehouse SQL."""
        self.approve_base()
        _, llm = self.first_turn()
        self.assertTrue(llm.exhausted, f"{len(llm.responses)} responses unused")
        self.assertEqual(llm.calls, 4)

    def test_the_artifact_records_every_stage(self):
        self.approve_base()
        a = self.first_turn()[0]["analysis"]
        self.assertEqual(a["base_view"], "checkout_sessions")
        self.assertTrue(a["population_hash"])
        self.assertEqual(a["datasets_used"], ["df_1"])
        self.assertTrue(a["warehouse_sql"])
        self.assertTrue(a["python_code"])
        self.assertTrue(a["created_at"])

    def test_the_artifact_is_persisted_on_the_answer_row(self):
        self.approve_base()
        out, _ = self.first_turn()
        row = self.ctx.stores.for_tenant(self.tid).query_one(
            "SELECT analysis, extract_meta FROM stakeholder_answers WHERE id=?",
            (out["answer_id"],))
        from analytics_platform.database import load_json
        self.assertEqual(load_json(row["analysis"])["base_view"], "checkout_sessions")
        self.assertTrue(load_json(row["extract_meta"])["population_hash"])


class TestReuse(_FlowCase):
    def _followup(self, plan_json, analysis_resp, question="break that down by device"):
        llm = SequencedLLM(["device breakdown", plan_json, analysis_resp, "iOS leads."])
        return self.answer(llm, question), llm

    def test_a_reuse_turn_issues_no_warehouse_query(self):
        """The observable that was broken. `device` is already a dimension of
        df_1, so the follow-up rolls up locally -- zero SQL to the warehouse."""
        self.approve_base()
        self.first_turn()
        before = len(self.spy.all_sql)
        out, _ = self._followup(
            '{"base_view":"checkout_sessions","cube":{"dimensions":["device"],'
            '"measures":[{"name":"revenue","expr":"SUM(revenue)"}],"filters":{}},'
            '"analysis":"workspace_sql"}',
            "```sql\nSELECT device, SUM(revenue) AS r FROM df_1 GROUP BY device\n```")
        self.assertEqual(out["queries_run"], [])
        self.assertEqual(len(self.spy.all_sql), before)
        self.assertEqual(len(out["analysis"]["workspace_sql"]), 1)
        self.assertIn("df_1", out["analysis"]["coverage"]["reason"])

    def test_a_reuse_turn_can_answer_in_python_too(self):
        self.approve_base()
        self.first_turn()
        out, _ = self._followup(
            '{"base_view":"checkout_sessions","cube":{"dimensions":["device"],'
            '"measures":[{"name":"revenue","expr":"SUM(revenue)"}],"filters":{}},'
            '"analysis":"python"}',
            "```python\nresult = df_1.groupby('device')['revenue'].sum().to_dict()\n```")
        self.assertEqual(out["queries_run"], [])
        self.assertEqual(len(out["python_cells"]), 1)

    def test_a_failed_workspace_query_falls_back_to_python_not_to_the_warehouse(self):
        """The data is already on this disk; a bad local query is not a reason to
        re-bill the warehouse."""
        self.approve_base()
        self.first_turn()
        before = len(self.spy.all_sql)
        llm = SequencedLLM([
            "device breakdown",
            '{"base_view":"checkout_sessions","cube":{"dimensions":["device"],'
            '"measures":[{"name":"revenue","expr":"SUM(revenue)"}],"filters":{}},'
            '"analysis":"workspace_sql"}',
            "```sql\nSELECT nope FROM df_1\n```",
            "```sql\nSELECT still_nope FROM df_1\n```",
            "```sql\nSELECT nope_again FROM df_1\n```",
            "```python\nresult = df_1['revenue'].sum()\n```",
            "iOS leads."])
        out = self.answer(llm, "break that down by device")
        self.assertEqual(len(self.spy.all_sql), before)
        self.assertEqual(out["queries_run"], [])
        self.assertEqual(len(out["python_cells"]), 1)

    def test_extract_survives_a_cold_workspace(self):
        """Reopening a conversation in a fresh process must still be answerable
        locally -- that is what the Parquet sidecar is for."""
        self.approve_base()
        self.first_turn()
        from analytics_platform.execution.dataframe_cache import ConversationDataCache
        from analytics_platform.execution.extract_store import ExtractStore
        from analytics_platform.execution.workspace import AnalyticalWorkspace
        store = ExtractStore(self.svc.extract_store.tenants_dir)
        self.svc.data_cache = ConversationDataCache(store=store)
        self.svc.extract_store = store
        self.svc.workspace.close_all()
        self.svc.workspace = AnalyticalWorkspace(store, self.ctx.settings.policy)
        before = len(self.spy.all_sql)
        out, _ = self._followup(
            '{"base_view":"checkout_sessions","cube":{"dimensions":["device"],'
            '"measures":[{"name":"revenue","expr":"SUM(revenue)"}],"filters":{}},'
            '"analysis":"python"}',
            "```python\nresult = df_1['revenue'].sum()\n```")
        self.assertEqual(out["queries_run"], [])
        self.assertEqual(len(self.spy.all_sql), before)


class TestTriangulation(_FlowCase):
    def test_two_answers_over_one_base_carry_the_same_population_hash(self):
        """The triangulation guarantee, end to end. A and B differ in dimensions
        and filters and are still provably computed from the same rows."""
        self.approve_base()
        a, _ = self.first_turn()
        llm = SequencedLLM([
            "revenue germany device",
            '{"base_view":"checkout_sessions","cube":{"dimensions":["device"],'
            '"measures":[{"name":"revenue","expr":"SUM(revenue)"}],'
            '"filters":{"country":["Germany"]}},"analysis":"python"}',
            "```python\nresult = df_1['revenue'].sum()\n```",
            "Germany skews web."])
        b = self.answer(llm, "sales in Germany by device", self.c2)
        self.assertEqual(a["analysis"]["population_hash"],
                         b["analysis"]["population_hash"])
        self.assertEqual(b["analysis"]["slice_filters"], {"country": ["Germany"]})

    def test_a_widen_supersedes_the_narrower_cube_and_keeps_both(self):
        self.approve_base()
        llm = SequencedLLM([
            "revenue by country",
            '{"base_view":"checkout_sessions","cube":{"dimensions":["country"],'
            '"measures":[{"name":"revenue","expr":"SUM(revenue)"}],"filters":{}},'
            '"analysis":"python"}',
            "```python\nresult = df_1['revenue'].sum()\n```", NARRATIVE])
        self.answer(llm, "sales by country")
        out, _ = SequencedLLM([]), None
        llm2 = SequencedLLM([
            "revenue by country and device",
            CUBE_1,
            "```python\nresult = df_2['revenue'].sum()\n```", NARRATIVE])
        out = self.answer(llm2, "and split that by device")
        self.assertEqual(out["analysis"]["supersedes"], "df_1")
        labels = {m.label for m in self.svc.extract_store.list_metas(self.tid, self.c1)}
        self.assertEqual(labels, {"df_1", "df_2"})
        first = self.svc.extract_store.meta(self.tid, self.c1, "df_1")
        self.assertEqual(out["extract_meta"]["population_hash"], first.population_hash)


class TestCaveats(_FlowCase):
    def test_a_provisional_base_view_is_caveated(self):
        """Day one: the planner proposed the base and nobody has approved it."""
        llm = SequencedLLM([
            "guest revenue",
            '{"base_view":"checkout_sessions","propose_base_view":'
            '{"name":"checkout_sessions","grain":["session_id"],'
            '"source_sql":"SELECT session_id, country, device, revenue FROM orders",'
            '"dimension_columns":["country","device"],"measure_columns":["revenue"]},'
            '"cube":{"dimensions":["country"],'
            '"measures":[{"name":"revenue","expr":"SUM(revenue)"}]},'
            '"analysis":"python"}',
            "```python\nresult = df_1['revenue'].sum()\n```", NARRATIVE])
        out = self.answer(llm, "guest sales by country")
        self.assertIs(out["analysis"]["base_view_approved"], False)
        self.assertTrue(any("provisional" in c for c in out["caveats"]), out["caveats"])

    def test_the_aggregate_path_says_it_cannot_be_reconciled(self):
        llm = SequencedLLM([
            "sales", '{"base_view":"checkout_sessions","aggregate_only":true,'
                     '"cube":{"dimensions":[],"measures":[]}}',
            "```sql\nSELECT SUM(revenue) AS r FROM orders\n```",
            "```python\nresult = df_1['revenue'].sum()\n```", NARRATIVE])
        self.approve_base()
        out = self.answer(llm, "total sales")
        self.assertIs(out["analysis"]["reconcilable"], False)
        self.assertTrue(any("cannot be reconciled" in c for c in out["caveats"]),
                        out["caveats"])

    def test_a_failed_cube_downgrade_is_announced_not_silent(self):
        """Falling back to aggregate loses reconcilability. Saying nothing about
        it is the worst possible outcome."""
        self.approve_base()
        # Only the cube fails. A warehouse that fails everything is an outage,
        # not a downgrade -- the downgrade is the case where the governed cube
        # dies and a one-off query still answers.
        self.spy.fail_cubes_only = "COLUMN_NOT_FOUND: revenue"
        llm = SequencedLLM([
            "sales by country", CUBE_1,
            "```sql\nSELECT SUM(revenue) AS r FROM orders\n```",
            "```python\nresult = df_1['revenue'].sum()\n```", NARRATIVE])
        out = self.answer(llm, "what are sales by country?")
        self.assertIs(out["analysis"]["reconcilable"], False)
        self.assertTrue(any("fell back to a one-off query" in c for c in out["caveats"]),
                        out["caveats"])
        self.assertTrue(any("cannot be reconciled" in c for c in out["caveats"]),
                        out["caveats"])

    def test_an_attribution_caveat_rides_along_on_a_reuse_turn(self):
        """The reuse turn ran no SQL at all, but the number still depends on the
        ranking, so the caveat is inherited from the base rather than attached to
        the query that happened to fetch it."""
        self.approve_base(BaseView(
            name="checkout_sessions", grain=["session_id"],
            source_sql="SELECT session_id, country, device, revenue FROM orders",
            dimension_columns=["country", "device"], measure_columns=["revenue"],
            row_count_estimate=1_200,
            attributions=[AttributionRule(
                column="service_line", grain=["session_id"], strategy="highest_intent",
                priority_values=["mobile", "fixed", "ott"], source="brain")]))
        self.first_turn()
        llm = SequencedLLM([
            "device", '{"base_view":"checkout_sessions","cube":{"dimensions":["device"],'
                      '"measures":[{"name":"revenue","expr":"SUM(revenue)"}],"filters":{}},'
                      '"analysis":"python"}',
            "```python\nresult = df_1['revenue'].sum()\n```", "iOS leads."])
        out = self.answer(llm, "break that down by device")
        self.assertEqual(out["queries_run"], [])
        self.assertTrue(any("highest intent" in c for c in out["caveats"]), out["caveats"])

    def test_a_truncated_cube_produces_a_visible_caveat(self):
        self.approve_base()
        # Only the MATERIALISED ceiling: lowering the transport ceiling instead
        # would also shrink the profiler's sample query, and an unprofiled
        # dimension fails the cube guard closed -- a different bug entirely.
        self.svc.settings.policy.raw_extract_row_limit = 2
        out, _ = self.first_turn()
        self.assertIs(out["extract_meta"]["truncated"], True)
        self.assertTrue(any("truncated" in c for c in out["caveats"]), out["caveats"])

    def test_an_undefined_metric_is_flagged_as_uncertain(self):
        """No approved 'churn' metric exists -- the answer must say so rather
        than quietly inventing a definition."""
        self.approve_base()
        llm = SequencedLLM(["churn", CUBE_1, PY_CELL, NARRATIVE])
        out = self.answer(llm, "what is our churn rate?")
        self.assertTrue(any("churn" in c and "not a defined metric" in c
                            for c in out["caveats"]), out["caveats"])
        self.assertIn("churn", out["analysis"]["unresolved_terms"])


class TestChartSpec(_FlowCase):
    def test_a_python_analysis_turn_can_return_a_chart_spec(self):
        self.approve_base()
        llm = SequencedLLM([
            "revenue by country", CUBE_1,
            "```python\nresult = df_1.groupby('country')['revenue'].sum().to_dict()\n"
            "chart = {'kind': 'bar', 'x': 'country', 'y': 'revenue', 'title': 'Revenue'}\n```",
            NARRATIVE])
        out = self.answer(llm, "what are sales by country?")
        self.assertEqual(out["analysis"]["chart_spec"]["kind"], "bar")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Task 15: download, reconcile, replay -- the whole interface Plan B consumes
# ---------------------------------------------------------------------------
class TestDownload(_FlowCase):
    def _route(self, label="df_1", conversation=None):
        from tests.test_api import route
        handler = route(self.app, "GET",
                        "/stakeholder/{tenant_id}/conversations/{conversation_id}"
                        "/extracts/{label}/download")
        return handler(self.tid, conversation or self.c1, label)

    def test_download_returns_csv_for_a_real_extract(self):
        self.approve_base()
        self.first_turn()
        r = self._route()
        self.assertEqual(r.media_type, "text/csv")
        self.assertEqual(r.body.decode().splitlines()[0], "country,device,revenue")
        self.assertIn("filename*=UTF-8''", r.headers["content-disposition"])

    def test_download_404s_for_an_unknown_label(self):
        from fastapi import HTTPException
        self.approve_base()
        self.first_turn()
        with self.assertRaises(HTTPException) as e:
            self._route("df_99")
        self.assertEqual(e.exception.status_code, 404)

    def test_download_400s_on_a_traversal_attempt(self):
        """Never let an id reach the filesystem, and never let ExtractStore's
        own ValueError surface as a 500."""
        from fastapi import HTTPException
        self.approve_base()
        self.first_turn()
        with self.assertRaises(HTTPException) as e:
            self._route("../../etc/passwd")
        self.assertEqual(e.exception.status_code, 400)

    def test_deleting_a_conversation_deletes_its_extracts(self):
        self.approve_base()
        self.first_turn()
        self.assertTrue(self.svc.extract_store.list_metas(self.tid, self.c1))
        self.svc.delete_conversation(self.tid, self.c1)
        self.assertEqual(self.svc.extract_store.list_metas(self.tid, self.c1), [])


class TestReconcile(_FlowCase):
    def _reconcile(self, a, b, measure="revenue"):
        from tests.test_api import route
        handler = route(self.app, "POST",
                        "/stakeholder/{tenant_id}/conversations/{conversation_id}/reconcile")
        body = type("Body", (), {"answer_a": a, "answer_b": b, "measure": measure})()
        return handler(self.tid, self.c1, body)

    def _second_cube(self, filters='{}', dimensions='["country","device"]'):
        llm = SequencedLLM([
            "sales again",
            '{"base_view":"checkout_sessions","cube":{"dimensions":' + dimensions + ','
            '"measures":[{"name":"revenue","expr":"SUM(revenue)"}],"filters":'
            + filters + '},"analysis":"python"}',
            "```python\nresult = 1\n```", NARRATIVE])
        return self.answer(llm, "sales again by country")

    def test_two_answers_over_one_base_reconcile(self):
        self.approve_base()
        a, _ = self.first_turn()
        b = self._second_cube()
        r = self._reconcile(a["answer_id"], b["answer_id"])
        self.assertIs(r["same_population"], True)
        self.assertIs(r["agrees"], True)
        self.assertAlmostEqual(r["value_a"], r["value_b"])

    def test_the_comparison_is_made_at_the_intersected_slice(self):
        """A is unfiltered, B is Germany-only. They agree about Germany, and that
        is the only thing they can be asked to agree about."""
        self.approve_base()
        a, _ = self.first_turn()
        b = self._second_cube(filters='{"country":["DE"]}')
        r = self._reconcile(a["answer_id"], b["answer_id"])
        self.assertIs(r["agrees"], True)
        self.assertIn("DE", r["explanation"])
        self.assertAlmostEqual(r["value_a"], 30.0)   # DE only, not the 100.0 total

    def test_an_inexpressible_slice_is_explained_not_faked(self):
        """B is filtered on a column A's cube does not carry, so A cannot isolate
        the same subset. Comparing at a wider slice would report a disagreement
        that is an artifact of the question, not of the data."""
        self.approve_base()
        llm = SequencedLLM([
            "sales by country",
            '{"base_view":"checkout_sessions","cube":{"dimensions":["country"],'
            '"measures":[{"name":"revenue","expr":"SUM(revenue)"}],"filters":{}},'
            '"analysis":"python"}',
            "```python\nresult = 1\n```", NARRATIVE])
        a = self.answer(llm, "sales by country")
        b = self._second_cube(filters='{"device":["ios"]}')
        r = self._reconcile(a["answer_id"], b["answer_id"])
        self.assertIs(r["agrees"], False)
        self.assertIn("does not carry", r["explanation"])

    def test_an_aggregate_path_answer_reconciles_with_nothing(self):
        self.approve_base()
        a, _ = self.first_turn()
        llm = SequencedLLM([
            "total", '{"base_view":"checkout_sessions","aggregate_only":true,'
                     '"cube":{"dimensions":[],"measures":[]}}',
            "```sql\nSELECT SUM(revenue) AS revenue FROM orders\n```", NARRATIVE])
        b = self.answer(llm, "total sales")
        r = self._reconcile(b["answer_id"], a["answer_id"])
        self.assertIs(r["same_population"], False)
        self.assertIn("no base view", r["explanation"].lower())
        self.assertIsNone(r["value_a"])

    def test_a_real_disagreement_names_a_likely_cause(self):
        """A WIDEN fetches its own cube, so the two answers really do read two
        different frames -- and when one of them is truncated, its total is
        understated. That is a finding about the data, not a bug in the compare."""
        self.spy.frame = pd.DataFrame({
            "country": ["DE", "DE", "US", "US"], "device": ["ios", "web", "ios", "web"],
            "channel": ["app", "web", "app", "web"], "revenue": [10.0, 20.0, 30.0, 40.0]})
        self.svc.junior.refresh_catalog(self.tid)
        self.approve_base(BaseView(
            name="checkout_sessions", grain=["session_id"],
            source_sql="SELECT session_id, country, device, channel, revenue FROM orders",
            dimension_columns=["country", "device", "channel"],
            measure_columns=["revenue"], row_count_estimate=1_200))
        a, _ = self.first_turn()
        self.spy.frame = pd.DataFrame({"country": ["DE"], "device": ["ios"],
                                       "channel": ["app"], "revenue": [1.0]})
        self.svc.settings.policy.raw_extract_row_limit = 1
        b = self._second_cube(dimensions='["country","device","channel"]')
        r = self._reconcile(a["answer_id"], b["answer_id"])
        self.assertIs(r["same_population"], True)
        self.assertIs(r["agrees"], False)
        self.assertIn("truncat", r["explanation"].lower())

    def test_reconcile_404s_on_an_unknown_message_id(self):
        from fastapi import HTTPException
        self.approve_base()
        self.first_turn()
        with self.assertRaises(HTTPException) as e:
            self._reconcile("nope", "nope")
        self.assertEqual(e.exception.status_code, 404)


class TestReplay(_FlowCase):
    def test_replay_carries_the_population_provenance(self):
        self.approve_base()
        self.first_turn()
        msgs = self.svc.get_conversation(self.tid, self.c1)["messages"]
        a = next(m for m in msgs if m.get("analysis"))["analysis"]
        self.assertTrue(a["population_hash"])
        self.assertTrue(a["base_view"])
        self.assertIn("base_view_approved", a)

    def test_replay_carries_the_extract_meta(self):
        self.approve_base()
        self.first_turn()
        msgs = self.svc.get_conversation(self.tid, self.c1)["messages"]
        m = next(m for m in msgs if m.get("extract_meta"))["extract_meta"]
        self.assertEqual(m["label"], "df_1")
        self.assertEqual(m["dimensions"], ["country", "device"])
