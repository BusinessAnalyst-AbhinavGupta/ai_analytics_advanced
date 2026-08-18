"""Task 13 -- whose ranking wins, and whether the base is at the grain it claims.

Two separate guarantees that happen to share a cause. Both are about a base
population lying to you:

  * Attribution. When one session carries three service lines, *something* has
    to decide which one it counts as. Letting each question decide means two
    questions apply two rankings to the same sessions and produce two
    defensible, contradictory numbers. So the ranking is a tenant fact in the
    Company Brain, a human approves it, and it lives inside the base view's
    source_sql -- inside its population_hash -- where no prompt can reach it.

  * Grain. The earlier design checked `df.duplicated(subset=grain)` on the
    returned extract. That is useless against a cube: GROUP BY deduplicates the
    dimension tuple unconditionally, so a base emitting three rows per
    session_id produces a cube where every cell is unique and every SUM is
    silently tripled. The check therefore moves onto the base itself, where the
    fan-out actually lives, and runs once per population_hash.
"""
from __future__ import annotations

import unittest

import pandas as pd

from analytics_platform.base_view import BaseViewRegistry
from analytics_platform.domain import (AttributionRule, BaseView, ColumnProfile, CubeMeasure,
                                       CubeSpec, DataSourceKind, NodeKind, ReviewStatus)
from analytics_platform.execution.base import QueryResult, SessionStatus
from analytics_platform.junior import ATTRIBUTION_TITLE_PREFIX, JuniorEngine
from analytics_platform.schema_context import SchemaContextBuilder
from analytics_platform.semantic import SemanticLayer
from tests.helpers import make_ctx

SUM_REVENUE = CubeMeasure("revenue", "SUM(revenue)", True)


def _view(**kw):
    d = dict(name="checkout_sessions", grain=["session_id"],
             source_sql="SELECT session_id, country, revenue FROM orders",
             dimension_columns=["country"], measure_columns=["revenue"],
             row_count_estimate=1_200_000, grain_verified=True)
    d.update(kw)
    return BaseView(**d)


def _profiles(**counts):
    return {c: ColumnProfile(column=c, dtype="object", distinct_count=n, null_fraction=0.0,
                             values=[], values_complete=False) for c, n in counts.items()}


class FakeExecutor:
    """Profiling sample + COUNT(*) probe, same shape as test_column_profiler's."""

    def __init__(self):
        self._df = pd.DataFrame()
        self.sql_seen = []

    def returns(self, df):
        self._df = df

    def supports(self, ctx):
        return True

    def session_status(self, tenant_id):
        return SessionStatus(state="valid", tenant_id=tenant_id)

    def execute(self, sql, ctx):
        self.sql_seen.append(sql)
        if sql.lower().lstrip().startswith("select count(*)"):
            return QueryResult(ok=True, data=pd.DataFrame({"row_count": [len(self._df)]}),
                               row_count=1, columns=["row_count"])
        return QueryResult(ok=True, data=self._df.copy(), row_count=len(self._df),
                           columns=list(self._df.columns))

    def cancel(self, execution_id):
        return True


def _fanned_events(n=100):
    """6 sessions in 100 carry two service lines; country never fans out."""
    rows = []
    for i in range(n):
        rows.append({"session_id": f"s{i}", "service_line": "retail",
                     "country": "DE" if i % 2 else "FR"})
    for i in range(6):
        rows.append({"session_id": f"s{i}", "service_line": "wholesale",
                     "country": "DE" if i % 2 else "FR"})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# the junior proposes the problem; a human supplies the business ranking
# ---------------------------------------------------------------------------
class TestProposeAttributionRules(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.tid = self.ctx.tenants.create_tenant("AttrCo").id
        self.fake = FakeExecutor()
        self.fake.returns(_fanned_events())
        self.engine = JuniorEngine(self.ctx.stores, executor=self.fake,
                                   tenants=self.ctx.tenants, settings=self.ctx.settings)
        self.ctx.tenants.add_datasource(self.tid, "events", DataSourceKind.DIRECT_DB,
                                        dialect="athena", tables=["events"])
        self.engine.profile_tables(self.tid, ["events"])

    def tearDown(self):
        self.ctx.close()

    def test_junior_proposes_a_draft_rule_for_a_fanned_out_column(self):
        nodes = self.engine.propose_attribution_rules(self.tid, ["events"])
        n = next(n for n in nodes if "service_line" in n.title)
        self.assertEqual(n.status, ReviewStatus.CANDIDATE)   # the plan's "DRAFT"
        self.assertEqual(n.payload["strategy"], "most_frequent")
        self.assertAlmostEqual(n.payload["fanout"], 0.06, delta=0.01)
        self.assertIn("6%", n.summary)

    def test_a_clean_column_gets_no_rule(self):
        nodes = self.engine.propose_attribution_rules(self.tid, ["events"])
        self.assertFalse([n for n in nodes if "country" in n.title], [n.title for n in nodes])

    def test_the_proposal_names_the_grain_key_it_fans_out_against(self):
        nodes = self.engine.propose_attribution_rules(self.tid, ["events"])
        n = next(n for n in nodes if "service_line" in n.title)
        self.assertEqual(n.payload["grain"], ["session_id"])
        self.assertIn("session_id", n.title)

    def test_the_junior_supplies_no_business_ranking(self):
        """It can measure that the problem exists. It cannot know which value
        outranks which -- that is the judgement a human is being asked for."""
        nodes = self.engine.propose_attribution_rules(self.tid, ["events"])
        n = next(n for n in nodes if "service_line" in n.title)
        self.assertEqual(n.payload["priority_values"], [])

    def test_proposing_twice_does_not_duplicate(self):
        self.engine.propose_attribution_rules(self.tid, ["events"])
        brain = self.engine.brain(self.tid)
        before = len(brain.all(kind=NodeKind.DEFINITION, limit=1000))
        self.engine.propose_attribution_rules(self.tid, ["events"])
        self.assertEqual(len(brain.all(kind=NodeKind.DEFINITION, limit=1000)), before)

    def test_a_fanout_under_the_threshold_is_not_worth_a_rule(self):
        self.ctx.settings.attribution_fanout_threshold = 0.5
        self.assertEqual(self.engine.propose_attribution_rules(self.tid, ["events"]), [])


# ---------------------------------------------------------------------------
# only approved rules reach a prompt
# ---------------------------------------------------------------------------
class TestAttributionInTheContext(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.tid = self.ctx.tenants.create_tenant("AttrCo").id
        self.registry = BaseViewRegistry(self.ctx.pipeline.brain)
        self.fake = FakeExecutor()
        self.fake.returns(pd.DataFrame({"session_id": ["s1"], "country": ["DE"],
                                        "revenue": [1]}))
        junior = JuniorEngine(self.ctx.stores, executor=self.fake,
                              tenants=self.ctx.tenants, settings=self.ctx.settings)
        self.builder = SchemaContextBuilder(
            junior, self.ctx.pipeline.brain, self.ctx.settings,
            SemanticLayer(self.ctx.pipeline.brain), self.registry)
        self.ctx.tenants.add_datasource(self.tid, "orders", DataSourceKind.DIRECT_DB,
                                        dialect="athena", tables=["orders"])

    def tearDown(self):
        self.ctx.close()

    def _rule_node(self, column, approved):
        brain = self.ctx.pipeline.brain(self.tid)
        rule = AttributionRule(column=column, grain=["session_id"],
                               strategy="most_frequent", source="junior")
        from dataclasses import asdict
        payload = asdict(rule)
        payload.update({"table": "orders", "fanout": 0.06})
        node = brain.create(kind=NodeKind.DEFINITION,
                            title=f"{ATTRIBUTION_TITLE_PREFIX}orders.{column} by session_id",
                            summary=f"{column} fans out", payload=payload,
                            created_by="junior", status=ReviewStatus.CANDIDATE)
        if approved:
            brain.submit(node.id, by="junior")
            node = brain.approve(node.id, by="senior")
        return node

    def _approved_view(self):
        node = self.registry.upsert(self.tid, _view(), by="stakeholder")
        brain = self.ctx.pipeline.brain(self.tid)
        brain.submit(node.id, by="junior")
        brain.approve(node.id, by="senior")

    def test_only_approved_rules_reach_the_prompt(self):
        self._rule_node("service_line", approved=True)
        self._rule_node("campaign", approved=False)
        rules = self.builder.attribution_rules(self.tid, ["orders"])
        self.assertEqual([r.column for r in rules], ["service_line"])

    def test_a_draft_rule_never_steers_an_answer(self):
        self._rule_node("campaign", approved=False)
        rendered = self.builder.build(self.tid, "revenue by country", [], []).rendered
        self.assertNotIn("ATTRIBUTION RULES", rendered)

    def test_approved_rules_render_between_base_views_and_schema(self):
        self._approved_view()
        self._rule_node("service_line", approved=True)
        r = self.builder.build(self.tid, "revenue by country", [], []).rendered
        self.assertLess(r.index("BASE VIEWS"), r.index("ATTRIBUTION RULES"))
        self.assertLess(r.index("ATTRIBUTION RULES"), r.index("DATABASE SCHEMA"))

    def test_the_rendered_rule_says_it_belongs_inside_the_base_view(self):
        """Rendered as a per-question filter it would be applied twice, or not at
        all. The prompt has to say where it goes."""
        self._rule_node("service_line", approved=True)
        r = self.builder.build(self.tid, "revenue by country", [], []).rendered
        self.assertIn("source_sql", r)

    def test_rules_for_unrelated_tables_are_not_dragged_in(self):
        brain = self.ctx.pipeline.brain(self.tid)
        from dataclasses import asdict
        payload = asdict(AttributionRule(column="tier", grain=["user_id"]))
        payload.update({"table": "billing", "fanout": 0.2})
        node = brain.create(kind=NodeKind.DEFINITION,
                            title=f"{ATTRIBUTION_TITLE_PREFIX}billing.tier by user_id",
                            summary="x", payload=payload, created_by="junior",
                            status=ReviewStatus.CANDIDATE)
        brain.submit(node.id, by="junior")
        brain.approve(node.id, by="senior")
        self.assertEqual(self.builder.attribution_rules(self.tid, ["orders"]), [])


# ---------------------------------------------------------------------------
# the grain probe
# ---------------------------------------------------------------------------
class TestGrainProbe(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.tid = self.ctx.tenants.create_tenant("GrainCo").id
        self.registry = BaseViewRegistry(self.ctx.pipeline.brain)

    def tearDown(self):
        self.ctx.close()

    def test_the_probe_inlines_the_base_and_counts_both_ways(self):
        view = _view()
        sql = self.registry.compose_grain_probe(view)
        self.assertIn(view.source_sql, sql)
        self.assertIn("COUNT(*)", sql)
        self.assertIn("COUNT(DISTINCT session_id)", sql)

    def test_a_composite_grain_is_concatenated_not_row_constructed(self):
        """Trino/Athena will not COUNT(DISTINCT ROW(...)); the separator is a
        character that cannot appear in an identifier value."""
        sql = self.registry.compose_grain_probe(_view(grain=["session_id", "day"]))
        self.assertIn("CHR(31)", sql)
        self.assertNotIn("DISTINCT (", sql.upper())

    def test_a_probe_over_a_grainless_view_is_refused(self):
        with self.assertRaises(ValueError):
            self.registry.compose_grain_probe(_view(grain=[]))

    def test_a_clean_base_is_marked_verified_and_gets_a_real_row_count(self):
        v = self.registry.record_grain_check(self.tid, _view(row_count_estimate=1),
                                             rows=1_200_000, keys=1_200_000)
        self.assertTrue(v.grain_verified)
        self.assertEqual(v.grain_violation_ratio, 0.0)
        self.assertEqual(v.row_count_estimate, 1_200_000)   # replaces the sampled floor

    def test_a_fanned_out_base_is_marked_unusable_with_the_ratio(self):
        v = self.registry.record_grain_check(self.tid, _view(), rows=1_300_000, keys=1_200_000)
        self.assertFalse(v.grain_verified)
        self.assertAlmostEqual(v.grain_violation_ratio, 1 - 1_200_000 / 1_300_000, places=5)

    def test_the_check_is_persisted_on_the_node(self):
        self.registry.record_grain_check(self.tid, _view(grain_verified=False),
                                         rows=10, keys=10)
        stored = self.registry.get(self.tid, "checkout_sessions", approved_only=False)
        self.assertTrue(stored.grain_verified)
        self.assertTrue(stored.grain_checked_at)

    def test_recording_a_check_does_not_reset_approval(self):
        node = self.registry.upsert(self.tid, _view(grain_verified=False), by="stakeholder")
        brain = self.ctx.pipeline.brain(self.tid)
        brain.submit(node.id, by="junior")
        brain.approve(node.id, by="senior")
        self.registry.record_grain_check(self.tid, _view(grain_verified=False),
                                         rows=10, keys=10)
        self.assertTrue(self.registry.is_approved(self.tid, "checkout_sessions"))

    def test_a_verified_base_is_not_re_probed(self):
        v = self.registry.record_grain_check(self.tid, _view(), rows=10, keys=10)
        self.assertFalse(self.registry.needs_grain_check(v))

    def test_editing_the_source_sql_forces_a_re_probe(self):
        """Keyed by population_hash, not by a version number nobody would bump."""
        v = self.registry.record_grain_check(self.tid, _view(), rows=10, keys=10)
        edited = _view(source_sql="SELECT session_id, country, revenue FROM orders_v2")
        edited.grain_checked_hash = v.grain_checked_hash
        edited.grain_verified = True
        self.assertTrue(self.registry.needs_grain_check(edited))

    def test_changing_an_attribution_rule_forces_a_re_probe(self):
        """An attribution rule is part of the population, so it changes the rows."""
        v = self.registry.record_grain_check(self.tid, _view(), rows=10, keys=10)
        edited = _view()
        edited.grain_checked_hash = v.grain_checked_hash
        edited.grain_verified = True
        edited.attributions = [AttributionRule(column="service_line", grain=["session_id"])]
        self.assertTrue(self.registry.needs_grain_check(edited))


class TestCubesRefuseUnverifiedBases(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.tid = self.ctx.tenants.create_tenant("GrainCo").id
        self.registry = BaseViewRegistry(self.ctx.pipeline.brain)
        self.spec = CubeSpec(base_name="checkout_sessions", dimensions=["country"],
                             measures=[SUM_REVENUE])

    def tearDown(self):
        self.ctx.close()

    def test_a_cube_over_an_unverified_base_is_refused(self):
        """The whole reason the check moved: GROUP BY would have hidden this."""
        out = self.registry.compose_cube(_view(grain_verified=False), self.spec,
                                         _profiles(country=30))
        self.assertFalse(out.ok)
        self.assertIn("grain", out.error.lower())

    def test_a_cube_over_a_fanned_out_base_names_the_multiplication(self):
        view = self.registry.record_grain_check(self.tid, _view(), rows=1_300_000,
                                                keys=1_200_000)
        out = self.registry.compose_cube(view, self.spec, _profiles(country=30))
        self.assertFalse(out.ok)
        self.assertIn("multiplied", out.error.lower())

    def test_a_verified_base_composes_normally(self):
        view = self.registry.record_grain_check(self.tid, _view(), rows=10, keys=10)
        self.assertTrue(self.registry.compose_cube(view, self.spec, _profiles(country=30)).ok)

    def test_a_keyset_chunk_over_an_unverified_base_is_also_refused(self):
        """Fan-out multiplies an ID-grain page exactly as it multiplies a cube."""
        with self.assertRaises(ValueError):
            self.registry.compose_keyset_chunk(_view(grain_verified=False), self.spec, "", 100)


# ---------------------------------------------------------------------------
# the probe inside a turn
# ---------------------------------------------------------------------------
CUBE = ('{"base_view":"checkout_sessions","cube":{"dimensions":["country"],'
        '"measures":[{"name":"revenue","expr":"SUM(revenue)"}]}}')


class TestGrainProbeInTheTurn(unittest.TestCase):
    """The probe is cheap, but not free: a tenant asking twenty questions of one
    approved base should pay for it once, ever."""

    def setUp(self):
        from analytics_platform.schema_context import SchemaContext
        from tests.test_api import app_ctx
        from tests.test_stakeholder import MockLLM, SpyExecutor
        from analytics_platform.api import create_app

        self.ctx, self.base = app_ctx()
        self.tid = self.ctx.tenants.create_tenant("GrainCo").id
        create_app(self.ctx)
        self.svc = self.ctx.stakeholder
        self.spy = SpyExecutor()
        self.svc.executor = self.spy
        self.MockLLM = MockLLM
        self.ctx.tenants.add_datasource(self.tid, "Orders", DataSourceKind.DIRECT_DB,
                                        dialect="athena", tables=["orders"])
        self.registry = self.svc.base_views
        self.schema_ctx = SchemaContext(profiles=_profiles(country=30), rendered="R")
        node = self.registry.upsert(self.tid, _view(grain_verified=False), by="senior")
        brain = self.ctx.pipeline.brain(self.tid)
        brain.submit(node.id, by="junior")
        brain.approve(node.id, by="senior")

    def tearDown(self):
        self.svc.workspace.close_all()
        self.base.close()

    def plan(self, n=1):
        for _ in range(n):
            out = self.svc._plan_turn(self.MockLLM([CUBE]), self.tid, "c1", "q", [], [],
                                      schema_ctx=self.schema_ctx)
        return out

    def test_the_probe_runs_once_per_population_hash(self):
        self.plan(n=3)
        self.assertEqual(self.spy.probe_call_count, 1)

    def test_a_probed_base_composes_its_cube(self):
        plan = self.plan()
        self.assertEqual(plan.base_view.name, "checkout_sessions")
        self.assertTrue(plan.cube_sql.ok, getattr(plan.cube_sql, "error", ""))

    def test_editing_the_source_sql_forces_a_re_probe(self):
        self.plan()
        edited = self.registry.get(self.tid, "checkout_sessions", approved_only=False)
        # Stays inside the datasource allow-list on purpose: the probe goes
        # through QueryPolicy like any other query, so an edit that reached a
        # new table would be refused before it ever became a round trip.
        edited.source_sql = ("SELECT session_id, country, revenue FROM orders "
                             "WHERE is_test_traffic = false")
        self.registry.upsert(self.tid, edited, by="senior")
        self.plan()
        self.assertEqual(self.spy.probe_call_count, 2)

    def test_a_failed_probe_on_an_approved_base_falls_through_to_aggregate(self):
        self.spy.probe_returns(rows=1_300_000, keys=1_200_000)
        plan = self.plan()
        self.assertEqual(plan.path, "aggregate")
        self.assertTrue(any("not at the grain it claims" in c for c in plan.caveats),
                        plan.caveats)

    def test_the_caveat_names_both_counts(self):
        self.spy.probe_returns(rows=1_300_000, keys=1_200_000)
        caveat = next(c for c in self.plan().caveats if "grain it claims" in c)
        self.assertIn("1,300,000", caveat)
        self.assertIn("1,200,000", caveat)

    def test_an_unrunnable_probe_says_unverified_not_violated(self):
        """Unverified is not the same as violated, and conflating them would tell
        a user their data is wrong when the truth is that nobody looked."""
        self.spy.probe_fails = True
        plan = self.plan()
        self.assertEqual(plan.path, "aggregate")
        self.assertTrue(any("could not be verified" in c for c in plan.caveats),
                        plan.caveats)
        self.assertFalse(any("not at the grain it claims" in c for c in plan.caveats))

    def test_the_probe_result_updates_the_row_count_estimate(self):
        """A measured count, replacing the sampled floor the profiler could offer."""
        self.spy.probe_returns(rows=987_654, keys=987_654)
        self.plan()
        stored = self.registry.get(self.tid, "checkout_sessions", approved_only=False)
        self.assertEqual(stored.row_count_estimate, 987_654)

    def test_a_base_view_cannot_certify_its_own_grain(self):
        """Posting `grain_verified: true` would make the probe decorative."""
        from analytics_platform.api import create_app
        from tests.test_api import call
        app = create_app(self.ctx)
        call(app, "POST", "/knowledge/{tenant_id}/base-views", self.tid,
             type("Body", (), {"model_dump": lambda s: dict(
                 name="self_certified", grain=["session_id"],
                 source_sql="SELECT session_id FROM orders", dimension_columns=[],
                 measure_columns=[], attributions=[], time_column="",
                 row_count_estimate=0, description="", owner="", aliases=[],
                 by="analyst")})())
        stored = self.registry.get(self.tid, "self_certified", approved_only=False)
        self.assertFalse(stored.grain_verified)


class TestTheIdGrainPathStillChecksRows(unittest.TestCase):
    """A cube's grain is verified on the definition. When ID-grain rows really do
    come down -- a non-additive measure, Task 12's keyset walk -- the rows are
    there to be checked, so check them: if this fires, the base changed under the
    stored verification and neither artifact can be trusted."""

    def setUp(self):
        from analytics_platform.domain import TurnPlan
        from analytics_platform.schema_context import SchemaContext
        from tests.test_api import app_ctx
        from tests.test_stakeholder import SpyExecutor
        from analytics_platform.api import create_app

        self.ctx, self.base = app_ctx()
        self.tid = self.ctx.tenants.create_tenant("GrainCo").id
        create_app(self.ctx)
        self.svc = self.ctx.stakeholder
        self.spy = SpyExecutor()
        self.spy.columns = ["session_id", "revenue"]
        self.svc.executor = self.spy
        self.ctx.tenants.add_datasource(self.tid, "Orders", DataSourceKind.DIRECT_DB,
                                        dialect="athena", tables=["orders"])
        view = _view()
        spec = CubeSpec(base_name=view.name, dimensions=[], measures=[SUM_REVENUE])
        self.plan = TurnPlan(path="retrieve", base_view=view, base_view_approved=True,
                             cube=spec, grain=["session_id"])

    def tearDown(self):
        self.svc.workspace.close_all()
        self.base.close()

    def _fetch(self):
        return self.svc._fetch_keyset_chunks(self.tid, self.plan, "q")

    def test_a_clean_id_grain_extract_records_no_violation(self):
        self.spy.returns_pages(3)
        self._fetch()
        self.assertFalse(self.svc._grain_violated)

    def test_duplicate_keys_in_a_keyset_extract_are_recorded(self):
        self.spy.duplicate_keys = True
        self.spy.returns_pages(4)
        _, res = self._fetch()
        self.assertTrue(self.svc._grain_violated)
        self.assertTrue(any("double-counted" in w for w in res.warnings), res.warnings)


class TestTheProposeRoute(unittest.TestCase):
    def setUp(self):
        from tests.test_api import app_ctx
        from analytics_platform.api import create_app

        self.ctx, self.base = app_ctx()
        self.tid = self.ctx.tenants.create_tenant("AttrCo").id
        self.app = create_app(self.ctx)
        self.fake = FakeExecutor()
        self.fake.returns(_fanned_events())
        self.ctx.junior.executor = self.fake
        self.ctx.tenants.add_datasource(self.tid, "events", DataSourceKind.DIRECT_DB,
                                        dialect="athena", tables=["events"])
        self.ctx.junior.profile_tables(self.tid, ["events"])

    def tearDown(self):
        self.base.close()

    def test_the_route_proposes_drafts_for_review(self):
        from tests.test_api import call
        out = call(self.app, "POST", "/knowledge/{tenant_id}/attribution/propose",
                   self.tid, ["events"])
        self.assertTrue(out)
        self.assertTrue(all(n["status"] == ReviewStatus.CANDIDATE.value for n in out), out)
        self.assertTrue(any("service_line" in n["title"] for n in out))


if __name__ == "__main__":
    unittest.main()
