"""Task 7 -- the governed ID-grain population, and cubes composed over it.

Two questions asked of the same warehouse should be answerable from the same
rows, so that when their numbers are compared they either agree or the
disagreement is explainable. In a warehouse you fix that with CREATE VIEW. This
Athena account is read-only, so the view is a client-side construct: a stored,
hashed, governed SQL definition inlined verbatim as a CTE into every derived
query. Without it, triangulation is manual archaeology; with it, it is a hash
comparison.
"""
from __future__ import annotations

import unittest

from analytics_platform.base_view import BaseViewRegistry, reconcile
from analytics_platform.config import MAX_CUBE_CELLS, MAX_DIMENSION_CARDINALITY
from analytics_platform.domain import (AttributionRule, BaseView, ColumnProfile, CubeMeasure,
                                       CubeSpec, NodeKind, ReviewStatus)
from tests.helpers import make_ctx

SUM_REVENUE = CubeMeasure("revenue", "SUM(revenue)", True)


def _view(**kw):
    d = dict(name="checkout_sessions", grain=["session_id"],
             source_sql="SELECT session_id, country, device, revenue FROM orders "
                        "WHERE is_test_traffic = false",
             dimension_columns=["country", "device"], measure_columns=["revenue"],
             row_count_estimate=1_200_000,
             # Task 13: a cube over an unverified base is refused. Every fixture
             # here is about composition, so it starts from a probed base; the
             # refusal itself is exercised in tests/test_attribution.py.
             grain_verified=True)
    d.update(kw)
    return BaseView(**d)


def _profiles(**counts):
    return {c: ColumnProfile(column=c, dtype="object", distinct_count=n, null_fraction=0.0,
                             values=[], values_complete=False) for c, n in counts.items()}


def _spec(view, dimensions=("country",), measures=(SUM_REVENUE,), **kw):
    return CubeSpec(base_name=view.name, dimensions=list(dimensions),
                    measures=list(measures), **kw)


def out_measure(out, name):
    return next(m for m in out.measures if m.name == name)


class _RegistryCase(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.tid = self.ctx.tenants.create_tenant("BaseCo").id
        self.registry = BaseViewRegistry(self.ctx.pipeline.brain)

    def tearDown(self):
        self.ctx.close()

    def approve(self, node):
        brain = self.ctx.pipeline.brain(self.tid)
        brain.submit(node.id, by="junior")
        brain.approve(node.id, by="senior")
        return node


class TestTheTwoHashes(_RegistryCase):
    def test_projection_does_not_change_the_population_hash(self):
        """Question B adds a column. Same rows, so the same population."""
        v = _view()
        p = _profiles(country=30, device=4)
        a = self.registry.compose_cube(v, _spec(v, ["country"]), p)
        b = self.registry.compose_cube(v, _spec(v, ["country", "device"]), p)
        self.assertEqual(a.population_hash, b.population_hash)
        self.assertNotEqual(a.projection_hash, b.projection_hash)

    def test_a_slice_filter_does_not_change_the_population_hash(self):
        """A filtered to Germany and B unfiltered must reconcile: one is a slice
        of the other, and a slice is not a different population."""
        v = _view()
        p = _profiles(country=30)
        unfiltered = self.registry.compose_cube(v, _spec(v), p)
        filtered = self.registry.compose_cube(
            v, _spec(v, filters={"country": ["Germany"]}), p)
        self.assertEqual(unfiltered.population_hash, filtered.population_hash)

    def test_a_time_window_does_not_change_the_population_hash(self):
        v = _view()
        p = _profiles(country=30)
        a = self.registry.compose_cube(v, _spec(v), p)
        b = self.registry.compose_cube(
            v, _spec(v, time_column="order_date", time_start="2026-08-01",
                     time_end="2026-08-31"), p)
        self.assertEqual(a.population_hash, b.population_hash)

    def test_a_different_source_sql_changes_the_population_hash(self):
        self.assertNotEqual(
            self.registry.population_hash(_view()),
            self.registry.population_hash(_view(source_sql="SELECT session_id FROM orders")))

    def test_whitespace_and_comments_do_not_change_the_population_hash(self):
        a = _view(source_sql="SELECT session_id FROM orders WHERE is_test_traffic = false")
        b = _view(source_sql="-- the population\nSELECT   session_id\n  FROM orders\n"
                             "  WHERE is_test_traffic = false\n")
        self.assertEqual(self.registry.population_hash(a), self.registry.population_hash(b))

    def test_a_block_comment_does_not_change_the_population_hash(self):
        a = _view(source_sql="SELECT session_id FROM orders")
        b = _view(source_sql="/* who owns this */ SELECT session_id FROM orders")
        self.assertEqual(self.registry.population_hash(a), self.registry.population_hash(b))

    def test_literal_casing_does_change_the_population_hash(self):
        """'mobile' is not 'Mobile'. Canonicalisation must never lowercase."""
        a = _view(source_sql="SELECT session_id FROM orders WHERE service_line = 'mobile'")
        b = _view(source_sql="SELECT session_id FROM orders WHERE service_line = 'Mobile'")
        self.assertNotEqual(self.registry.population_hash(a), self.registry.population_hash(b))

    def test_a_different_attribution_ranking_changes_the_population_hash(self):
        """The whole reason attribution lives in the base: two rankings applied to
        the same sessions are two populations, and two defensible-looking,
        mutually contradictory numbers."""
        r1 = AttributionRule(column="service_line", grain=["session_id"],
                             strategy="highest_intent",
                             priority_values=["mobile", "fixed", "ott"])
        r2 = AttributionRule(column="service_line", grain=["session_id"],
                             strategy="highest_intent",
                             priority_values=["ott", "fixed", "mobile"])
        self.assertNotEqual(self.registry.population_hash(_view(attributions=[r1])),
                            self.registry.population_hash(_view(attributions=[r2])))

    def test_the_order_rules_are_listed_in_does_not_change_the_hash(self):
        """The rule list is a set; the ranking *inside* a rule is the semantics."""
        a = AttributionRule(column="service_line", grain=["session_id"], strategy="latest")
        b = AttributionRule(column="category", grain=["session_id"], strategy="latest")
        self.assertEqual(self.registry.population_hash(_view(attributions=[a, b])),
                         self.registry.population_hash(_view(attributions=[b, a])))

    def test_grain_order_does_not_change_the_population_hash(self):
        self.assertEqual(self.registry.population_hash(_view(grain=["session_id", "dt"])),
                         self.registry.population_hash(_view(grain=["dt", "session_id"])))

    def test_the_projection_hash_ignores_column_order(self):
        self.assertEqual(self.registry.projection_hash(["b", "a"]),
                         self.registry.projection_hash(["a", "b"]))

    def test_a_hash_is_a_hex_digest_not_a_python_hash(self):
        """It is persisted and compared across processes, so it must be stable."""
        h = self.registry.population_hash(_view())
        self.assertEqual(len(h), 64)
        self.assertEqual(h, self.registry.population_hash(_view()))


class TestCubeComposition(_RegistryCase):
    def test_the_base_is_inlined_verbatim_as_a_cte(self):
        v = _view()
        out = self.registry.compose_cube(v, _spec(v), _profiles(country=30))
        self.assertTrue(out.ok, out.error)
        self.assertTrue(out.sql.startswith("WITH base AS ("))
        self.assertIn(v.source_sql, out.sql)      # byte for byte, not paraphrased
        self.assertIn("GROUP BY 1", out.sql)

    def test_the_group_by_is_ordinal_and_matches_the_select_positionally(self):
        v = _view()
        out = self.registry.compose_cube(
            v, _spec(v, ["country", "device"]), _profiles(country=30, device=4))
        self.assertIn("GROUP BY 1, 2", out.sql)

    def test_a_cube_with_no_dimensions_emits_no_group_by(self):
        v = _view()
        out = self.registry.compose_cube(v, _spec(v, []), {})
        self.assertTrue(out.ok, out.error)
        self.assertNotIn("GROUP BY", out.sql)

    def test_slice_filters_are_emitted_above_the_base(self):
        v = _view()
        out = self.registry.compose_cube(
            v, _spec(v, filters={"country": ["Germany", "France"]}), _profiles(country=30))
        body = out.sql.split("FROM base", 1)[1]
        self.assertIn("country IN ('Germany', 'France')", body)

    def test_a_quote_in_a_filter_literal_is_escaped(self):
        v = _view()
        out = self.registry.compose_cube(
            v, _spec(v, filters={"country": ["Côte d'Ivoire"]}), _profiles(country=30))
        self.assertIn("'Côte d''Ivoire'", out.sql)

    def test_a_time_window_is_emitted_as_a_between(self):
        v = _view()
        out = self.registry.compose_cube(
            v, _spec(v, time_column="order_date", time_start="2026-08-01",
                     time_end="2026-08-31"), _profiles(country=30))
        self.assertIn("order_date BETWEEN DATE '2026-08-01' AND DATE '2026-08-31'", out.sql)

    def test_an_unknown_dimension_is_refused_rather_than_guessed(self):
        """Reaching around the base into a column it does not carry produces a
        number that cannot be reconciled with anything."""
        v = _view()
        out = self.registry.compose_cube(v, _spec(v, ["nonesuch"]), _profiles(nonesuch=3))
        self.assertFalse(out.ok)
        self.assertIn("nonesuch", out.error)

    def test_an_identifier_shaped_filter_column_is_refused(self):
        v = _view()
        out = self.registry.compose_cube(v, _spec(v, filters={"nonesuch": ["x"]}),
                                         _profiles(country=30))
        self.assertFalse(out.ok)

    def test_composition_never_touches_an_executor(self):
        """Composition and execution stay separate so this file is testable
        without a warehouse, and so composed SQL can be hashed, logged, and shown
        to a human before anyone runs it."""
        import analytics_platform.base_view as mod
        self.assertNotIn("executor", mod.__dict__)


class TestAdditivity(_RegistryCase):
    def test_avg_is_stored_as_a_sum_and_a_count(self):
        """Averaging averages is wrong the moment the cube is rolled up."""
        v = _view()
        out = self.registry.compose_cube(
            v, _spec(v, measures=[CubeMeasure("revenue", "AVG(revenue)", True)]),
            _profiles(country=30))
        self.assertIn("SUM(revenue) AS revenue_sum", out.sql)
        self.assertIn("COUNT(revenue) AS revenue_count", out.sql)
        self.assertNotIn("AVG(", out.sql)
        m = out_measure(out, "revenue")
        self.assertIs(m.additive, True)
        self.assertEqual(m.read_expr, "revenue_sum / NULLIF(revenue_count, 0)")

    def test_the_rewrite_happens_even_when_the_caller_claims_otherwise(self):
        """compose_cube performs the rewrite itself rather than trusting the
        caller -- an AVG column in a cube manifest is a bug this file must not
        be able to emit."""
        v = _view()
        out = self.registry.compose_cube(
            v, _spec(v, measures=[CubeMeasure("r", "avg(revenue)", additive=False)]),
            _profiles(country=30))
        self.assertNotIn("avg(", out.sql.lower())
        self.assertIs(out_measure(out, "r").additive, True)

    def test_count_distinct_is_marked_non_additive(self):
        v = _view()
        out = self.registry.compose_cube(
            v, _spec(v, measures=[CubeMeasure("users", "COUNT(DISTINCT user_id)", True)]),
            _profiles(country=30))
        self.assertTrue(out.ok, out.error)
        self.assertEqual(out.non_additive, ["users"])

    def test_a_percentile_is_marked_non_additive(self):
        v = _view()
        out = self.registry.compose_cube(
            v, _spec(v, measures=[CubeMeasure("p90", "APPROX_PERCENTILE(revenue, 0.9)", True)]),
            _profiles(country=30))
        self.assertEqual(out.non_additive, ["p90"])

    def test_sum_and_count_star_are_additive(self):
        v = _view()
        out = self.registry.compose_cube(
            v, _spec(v, measures=[SUM_REVENUE, CubeMeasure("n", "COUNT(*)", True)]),
            _profiles(country=30))
        self.assertEqual(out.non_additive, [])

    def test_min_and_max_are_additive(self):
        v = _view()
        out = self.registry.compose_cube(
            v, _spec(v, measures=[CubeMeasure("first_seen", "MIN(order_date)", True),
                                  CubeMeasure("last_seen", "MAX(order_date)", True)]),
            _profiles(country=30))
        self.assertEqual(out.non_additive, [])

    def test_a_non_additive_cube_is_still_valid_at_its_own_grain(self):
        """Task 10 is what forbids reusing it at a coarser one -- not this guard."""
        v = _view()
        out = self.registry.compose_cube(
            v, _spec(v, measures=[CubeMeasure("users", "COUNT(DISTINCT user_id)", True)]),
            _profiles(country=30))
        self.assertTrue(out.ok)


class TestTheCardinalityGuard(_RegistryCase):
    def test_a_cube_that_would_explode_is_refused_with_the_culprit_named(self):
        v = _view(dimension_columns=["country", "device", "city"])
        out = self.registry.compose_cube(
            v, _spec(v, ["country", "device", "city"]),
            _profiles(country=30, device=4, city=4_000))          # 480,000 cells
        self.assertFalse(out.ok)
        self.assertEqual(out.offending_dimensions, ["city"])
        self.assertIn(str(MAX_CUBE_CELLS), out.error)

    def test_the_estimate_is_capped_by_the_bases_own_row_count(self):
        """A cube cannot produce more cells than the base has rows."""
        v = _view(row_count_estimate=90_000, dimension_columns=["a", "b", "c"])
        out = self.registry.compose_cube(
            v, _spec(v, ["a", "b", "c"], [CubeMeasure("n", "COUNT(*)", True)]),
            _profiles(a=100, b=100, c=100))
        self.assertTrue(out.ok, out.error)
        self.assertEqual(out.estimated_cells, 90_000)

    def test_a_high_cardinality_column_is_never_a_dimension(self):
        """Those are keys and free text, not dimensions."""
        v = _view(dimension_columns=["session_id"])
        out = self.registry.compose_cube(
            v, _spec(v, ["session_id"], [CubeMeasure("n", "COUNT(*)", True)]),
            _profiles(session_id=1_200_000))
        self.assertFalse(out.ok)
        self.assertIn("session_id", out.offending_dimensions)

    def test_the_dimension_cardinality_ceiling_is_read_from_config(self):
        v = _view(dimension_columns=["city"])
        out = self.registry.compose_cube(
            v, _spec(v, ["city"], [CubeMeasure("n", "COUNT(*)", True)]),
            _profiles(city=MAX_DIMENSION_CARDINALITY + 1))
        self.assertFalse(out.ok)

    def test_an_unprofiled_dimension_is_refused_and_warned(self):
        """Absent profiles are absent, never defaulted to zero -- an unprofiled
        column must not be able to sneak a 10M-cell cube past the guard, so the
        guard fails closed rather than certifying a cube it cannot size."""
        v = _view(dimension_columns=["country", "mystery"])
        out = self.registry.compose_cube(
            v, _spec(v, ["country", "mystery"], [CubeMeasure("n", "COUNT(*)", True)]),
            _profiles(country=30))
        self.assertFalse(out.ok)
        self.assertTrue(any("mystery" in w and "not profiled" in w for w in out.warnings),
                        out.warnings)

    def test_max_cube_cells_is_deliberately_above_the_transport_ceiling(self):
        """Different numbers on purpose: MAX_CUBE_CELLS answers 'is this cube
        worth composing at all', max_transport_rows answers 'what fits in one
        round trip'. A cube between them is legal and gets keyset-paged."""
        from analytics_platform.config import PolicySettings
        self.assertGreater(MAX_CUBE_CELLS, PolicySettings().max_transport_rows)


class TestKeysetPagination(_RegistryCase):
    def test_keyset_chunk_uses_a_cursor_not_an_offset(self):
        """Never OFFSET: Athena rescans from the top on every page, which is
        quadratic and, on a changing table, silently skips and duplicates rows."""
        v = _view()
        sql = self.registry.compose_keyset_chunk(
            v, _spec(v, [], []), last_seen="s_004999", chunk_rows=50_000)
        self.assertIn("session_id > 's_004999'", sql)
        self.assertIn("ORDER BY session_id", sql)
        self.assertIn("LIMIT 50000", sql)
        self.assertNotIn("OFFSET", sql.upper())

    def test_the_first_keyset_chunk_has_no_cursor_predicate(self):
        v = _view()
        sql = self.registry.compose_keyset_chunk(v, _spec(v, [], []), last_seen="",
                                                 chunk_rows=50_000)
        head = sql.split("FROM base", 1)[1].split("ORDER BY", 1)[0]
        self.assertNotIn(">", head)

    def test_a_composite_key_uses_a_row_value_comparison(self):
        v = _view(grain=["session_id", "event_id"])
        sql = self.registry.compose_keyset_chunk(
            v, _spec(v, [], []), last_seen=["s_1", "e_9"], chunk_rows=1_000)
        self.assertIn("(session_id, event_id) > ('s_1', 'e_9')", sql)
        self.assertIn("ORDER BY session_id, event_id", sql)

    def test_a_cursor_literal_is_escaped(self):
        v = _view()
        sql = self.registry.compose_keyset_chunk(v, _spec(v, [], []),
                                                 last_seen="o'brien", chunk_rows=10)
        self.assertIn("'o''brien'", sql)

    def test_the_keys_can_be_the_cube_dimensions_instead_of_the_grain(self):
        """Task 12 pages a large cube over its dimension tuple with the same
        ordering discipline -- one function, not a second SQL writer."""
        v = _view()
        sql = self.registry.compose_keyset_chunk(
            v, _spec(v, ["country", "device"]), last_seen=["DE", "ios"],
            chunk_rows=50_000, keys=["country", "device"])
        self.assertIn("(country, device) > ('DE', 'ios')", sql)
        self.assertIn("GROUP BY 1, 2", sql)

    def test_the_slice_still_applies_to_a_page(self):
        v = _view()
        sql = self.registry.compose_keyset_chunk(
            v, _spec(v, [], [], filters={"country": ["Germany"]}),
            last_seen="", chunk_rows=10)
        self.assertIn("country IN ('Germany')", sql)


class TestGovernance(_RegistryCase):
    def draft_view(self, **kw):
        return self.registry.upsert(self.tid, _view(**kw), by="planner")

    def test_a_new_base_view_is_created_unapproved(self):
        self.assertEqual(self.draft_view().status, ReviewStatus.CANDIDATE)

    def test_draft_base_views_are_excluded_by_default(self):
        self.draft_view()
        self.assertEqual(self.registry.all(self.tid), [])
        self.assertEqual(len(self.registry.all(self.tid, approved_only=False)), 1)

    def test_get_resolves_a_draft_only_when_asked(self):
        self.draft_view()
        self.assertIsNone(self.registry.get(self.tid, "checkout_sessions"))
        self.assertIsNotNone(
            self.registry.get(self.tid, "checkout_sessions", approved_only=False))

    def test_upsert_updates_in_place_rather_than_duplicating(self):
        self.registry.upsert(self.tid, _view(), by="senior")
        self.registry.upsert(self.tid, _view(description="clearer"), by="senior")
        self.assertEqual(len(self.registry.all(self.tid, approved_only=False)), 1)

    def test_an_approved_view_survives_a_reread_with_its_attributions(self):
        node = self.registry.upsert(self.tid, _view(attributions=[
            AttributionRule(column="service_line", grain=["session_id"],
                            strategy="highest_intent",
                            priority_values=["mobile", "fixed", "ott"])]), by="senior")
        self.approve(node)
        got = self.registry.get(self.tid, "checkout_sessions")
        self.assertEqual(got.attributions[0].priority_values, ["mobile", "fixed", "ott"])
        self.assertIsInstance(got.attributions[0], AttributionRule)

    def test_base_views_are_definition_nodes_with_a_stable_title(self):
        self.draft_view()
        titles = {n.title for n in self.ctx.pipeline.brain(self.tid).all(kind=NodeKind.DEFINITION)}
        self.assertIn("Base View: checkout_sessions", titles)

    def test_a_malformed_base_view_payload_is_skipped_not_raised(self):
        self.ctx.pipeline.brain(self.tid).create(
            NodeKind.DEFINITION, "Base View: broken",
            payload={"name": "broken", "grain": "session_id"},
            status=ReviewStatus.APPROVED)
        self.assertEqual(self.registry.all(self.tid), [])


class TestRender(_RegistryCase):
    def test_render_marks_a_draft_as_provisional(self):
        self.registry.upsert(self.tid, _view(), by="planner")
        r = self.registry.render(self.registry.all(self.tid, approved_only=False), self.tid)
        self.assertIn("[DRAFT", r)
        self.assertIn("provisional", r.lower())

    def test_render_marks_an_approved_view_as_approved(self):
        self.approve(self.registry.upsert(self.tid, _view(), by="senior"))
        self.assertIn("[APPROVED]",
                      self.registry.render(self.registry.all(self.tid), self.tid))

    def test_render_lists_the_attribution_so_a_reader_sees_it(self):
        node = self.registry.upsert(self.tid, _view(attributions=[
            AttributionRule(column="service_line", grain=["session_id"],
                            strategy="highest_intent",
                            priority_values=["mobile", "fixed", "ott"])]), by="senior")
        self.approve(node)
        r = self.registry.render(self.registry.all(self.tid), self.tid).lower()
        self.assertIn("highest intent", r)
        self.assertIn("mobile", r)

    def test_render_tells_the_planner_to_name_exactly_one_base(self):
        self.approve(self.registry.upsert(self.tid, _view(), by="senior"))
        r = self.registry.render(self.registry.all(self.tid), self.tid)
        self.assertIn("exactly ONE base view", r)
        self.assertIn("do NOT write the base", r.replace("You ", ""))

    def test_render_with_no_views_still_tells_the_planner_what_to_do(self):
        """Day one: no base view exists yet. The turn must proceed and say so."""
        r = self.registry.render([])
        self.assertIn("propose one at ID grain", r)


class TestReconcile(_RegistryCase):
    def test_same_population_and_equal_values_reconcile(self):
        r = reconcile("h1", 1_234.0, "h1", 1_234.0, measure="revenue")
        self.assertTrue(r.same_population)
        self.assertTrue(r.agrees)

    def test_same_population_but_different_values_is_a_real_disagreement(self):
        r = reconcile("h1", 1_234.0, "h1", 1_200.0, measure="revenue")
        self.assertTrue(r.same_population)
        self.assertFalse(r.agrees)
        self.assertIn("revenue", r.explanation)

    def test_different_populations_cannot_be_compared_at_all(self):
        r = reconcile("h1", 1_234.0, "h2", 1_234.0, measure="revenue")
        self.assertFalse(r.same_population)
        self.assertFalse(r.agrees)
        self.assertIn("different", r.explanation.lower())

    def test_an_answer_with_no_population_reconciles_with_nothing(self):
        """The aggregate escape path. This is a real, expected state -- the user
        deserves the reason, not a number."""
        r = reconcile("", 1_234.0, "h1", 1_234.0, measure="revenue")
        self.assertFalse(r.same_population)
        self.assertIn("no base view", r.explanation.lower())

    def test_a_float_wobble_within_tolerance_still_agrees(self):
        self.assertTrue(reconcile("h1", 1_234.0, "h1", 1_234.0000000001,
                                  measure="revenue").agrees)

    def test_the_explanation_carries_both_values_for_a_human(self):
        r = reconcile("h1", 1_234.0, "h1", 1_200.0, measure="revenue")
        self.assertIn("1,234", r.explanation)
        self.assertIn("1,200", r.explanation)


if __name__ == "__main__":
    unittest.main()
