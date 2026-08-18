"""Task 8 -- putting the semantics, the base views, and the real schema in front
of the LLM.

Everything Tasks 5, 6, and 7 wrote to the Brain is useless until something puts
it in a prompt. This is the task that closes the "generic queries" gap: the
query-writing LLM previously got title+summary text and nothing else -- no
column list, no types, no idea what values a column actually contains -- so it
inferred column names from whatever example query happened to be retrieved, and
invented filter literals.

Three blocks in a fixed order: business meaning first, then the populations that
meaning may be measured over, then the physical layout underneath. When they
disagree, semantics beat base views beat schema beat the retrieved examples.
"""
from __future__ import annotations

import tempfile
import unittest

import pandas as pd

from analytics_platform.base_view import BaseViewRegistry
from analytics_platform.domain import (BaseView, ColumnProfile, DataSourceKind, NodeKind,
                                       ReviewStatus, SemanticDimension, SemanticMetric)
from analytics_platform.execution.base import QueryResult
from analytics_platform.junior import JuniorEngine
from analytics_platform.schema_context import MAX_CONTEXT_TABLES, SchemaContextBuilder
from analytics_platform.semantic import SemanticLayer
from tests.helpers import make_ctx


class FakeNode:
    def __init__(self, title="", payload=None, summary=""):
        self.title = title
        self.payload = payload or {}
        self.summary = summary


class SpyJunior:
    """Stands in for JuniorEngine: records what got profiled, serves canned
    profiles, and can be made to fail."""

    def __init__(self, profiles=None, catalog=None, fail=False):
        self._profiles = profiles or {}
        self._catalog = catalog or {}
        self.fail = fail
        self.profile_calls = []

    def get_catalog(self, tenant_id):
        return {"tables": [{"table": t, "columns": c, "types": ["object"] * len(c)}
                           for t, c in self._catalog.items()]}

    def get_column_profiles(self, tenant_id, table):
        return list(self._profiles.get(table, []))

    def get_profile_payload(self, tenant_id, table):
        if table not in self._profiles:
            return {}
        return {"table": table, "row_count_estimate": self._catalog_rows(table),
                "columns": []}

    def _catalog_rows(self, table):
        return {"orders": 1_240_000, "funnel_events": 900_000}.get(table, 1_000)

    def profile_tables(self, tenant_id, tables=None, force=False):
        self.profile_calls.append(tuple(tables or []))
        if self.fail:
            raise RuntimeError("ACCESS_DENIED")
        for t in tables or []:
            self._profiles.setdefault(t, [])
        return {t: self._profiles[t] for t in tables or []}


def _profile(column, distinct, values=(), complete=False, dtype="object",
             null_fraction=0.0, **kw):
    return ColumnProfile(column=column, dtype=dtype, distinct_count=distinct,
                         null_fraction=null_fraction, values=list(values),
                         values_complete=complete, **kw)


ORDERS_PROFILES = [
    _profile("order_id", 1_240_000),
    _profile("status", 3, ["COMPLETED", "CANCELLED", "REFUNDED"], complete=True),
    _profile("country", 28, [f"C{i}" for i in range(28)], complete=True),
    _profile("service_line", 3, ["mobile", "fixed", "ott"], complete=True,
             fanout_by_key={"session_id": 0.06}),
    _profile("city", 300, [f"city_{i}" for i in range(20)], complete=False,
             null_fraction=0.02),
    _profile("revenue", 900, dtype="float64", min_value="0.0", max_value="48500.0"),
    _profile("order_date", 900, dtype="datetime64[ns]",
             min_value="2024-01-01", max_value="2026-08-14"),
]


class _ContextCase(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.tid = self.ctx.tenants.create_tenant("ContextCo").id
        self.semantic = SemanticLayer(self.ctx.pipeline.brain)
        self.registry = BaseViewRegistry(self.ctx.pipeline.brain)
        self.junior = SpyJunior(profiles={"orders": ORDERS_PROFILES},
                                catalog={"orders": [p.column for p in ORDERS_PROFILES]})
        self.builder = SchemaContextBuilder(self.junior, self.ctx.pipeline.brain,
                                            self.ctx.settings, self.semantic, self.registry)

    def tearDown(self):
        self.ctx.close()

    def approve(self, node):
        brain = self.ctx.pipeline.brain(self.tid)
        brain.submit(node.id, by="junior")
        brain.approve(node.id, by="senior")
        return node

    def approved_metric(self):
        return self.approve(self.semantic.upsert_metric(self.tid, SemanticMetric(
            name="conversion_rate", definition="completed / eligible",
            grain=["session_id"], dimensions=["country"],
            source_tables=["funnel_events"], filters=["is_test_traffic = false"],
            caveats=["excludes test traffic"], aliases=["CVR", "conversion"]), by="senior"))

    def approved_base_view(self, **kw):
        d = dict(name="checkout_sessions", grain=["session_id"],
                 source_sql="SELECT session_id, country FROM funnel_events",
                 dimension_columns=["country"], measure_columns=["revenue"],
                 row_count_estimate=1_200_000, description="test traffic excluded")
        d.update(kw)
        return self.approve(self.registry.upsert(self.tid, BaseView(**d), by="senior"))

    def build(self, question="revenue by country", query_nodes=(), defn_nodes=(), **kw):
        return self.builder.build(self.tid, question, list(query_nodes),
                                  list(defn_nodes), **kw)


class TestTableSelection(_ContextCase):
    def test_tables_come_from_retrieved_query_sql(self):
        node = FakeNode(payload={"sql": "SELECT * FROM sessions JOIN orders USING (order_id)"})
        tables = self.builder.relevant_tables(self.tid, "q", [node], [])
        self.assertTrue({"sessions", "orders"} <= set(tables), tables)

    def test_tables_come_from_definition_node_titles(self):
        tables = self.builder.relevant_tables(self.tid, "q", [], [FakeNode(title="Table: refunds")])
        self.assertIn("refunds", tables)

    def test_tables_named_in_the_question_are_picked_up(self):
        self.assertIn("orders", self.builder.relevant_tables(self.tid, "how many orders?", [], []))

    def test_a_matched_metrics_source_table_survives_the_cap(self):
        """A matched metric's source table is the one table the query certainly
        needs, so it is never dropped by the cap."""
        self.approved_metric()
        many = [FakeNode(payload={"sql": f"SELECT * FROM filler_{i}"}) for i in range(20)]
        ctx = self.build("conversion by country", query_nodes=many)
        self.assertIn("funnel_events", [t["table"] for t in ctx.tables])

    def test_a_base_views_own_tables_survive_the_cap(self):
        """A base view shown as selectable while its schema was invisible is the
        worst of both."""
        self.approved_base_view()
        many = [FakeNode(payload={"sql": f"SELECT * FROM filler_{i}"}) for i in range(20)]
        ctx = self.build("conversion by country", query_nodes=many)
        self.assertIn("funnel_events", [t["table"] for t in ctx.tables])

    def test_table_selection_is_capped_and_says_so(self):
        """An LLM that thinks it has seen every table will confidently join
        against one it was never shown."""
        many = [FakeNode(payload={"sql": f"SELECT * FROM filler_{i}"}) for i in range(20)]
        ctx = self.build(query_nodes=many)
        self.assertEqual(len(ctx.tables), MAX_CONTEXT_TABLES)
        self.assertIn("truncated", ctx.rendered.lower())

    def test_an_uncapped_selection_does_not_claim_truncation(self):
        self.assertNotIn("truncated", self.build().rendered.lower())


class TestInlineProfiling(_ContextCase):
    def test_missing_profile_triggers_the_junior_inline(self):
        self.junior._profiles.pop("orders")
        ctx = self.build("revenue by country")
        self.assertEqual(self.junior.profile_calls, [("orders",)])
        self.assertEqual(ctx.profiled_now, ["orders"])

    def test_an_already_profiled_table_is_not_reprofiled(self):
        self.build("revenue by country")
        self.assertEqual(self.junior.profile_calls, [])

    def test_profile_if_missing_false_does_not_call_the_junior(self):
        self.junior._profiles.pop("orders")
        self.build("q", profile_if_missing=False)
        self.assertEqual(self.junior.profile_calls, [])

    def test_unprofiled_table_is_reported_not_raised(self):
        """A table that cannot be profiled must never take the chat down."""
        self.junior._profiles.pop("orders")
        self.junior.fail = True
        ctx = self.build("revenue by country")
        self.assertEqual(ctx.unprofiled, ["orders"])
        self.assertTrue(ctx.rendered)          # still renders columns/types


class TestProfilesOutput(_ContextCase):
    def test_profiles_are_flattened_for_the_cube_guard(self):
        """Task 7's cell guard and Task 11's planner both need distinct_count per
        candidate dimension; flatten it once here rather than re-reading nodes."""
        ctx = self.build("revenue by country")
        self.assertEqual(ctx.profiles["country"].distinct_count, 28)

    def test_a_column_name_collision_keeps_the_larger_tables_profile(self):
        """A silently-wrong cardinality is exactly what the guard cannot survive."""
        self.junior._profiles["tiny"] = [_profile("country", 2)]
        self.junior._catalog["tiny"] = ["country"]
        ctx = self.build("orders and tiny by country")
        self.assertEqual(ctx.profiles["country"].distinct_count, 28)   # orders is bigger
        self.assertTrue(any("country" in n for n in ctx.collisions), ctx.collisions)


class TestRenderedSchema(_ContextCase):
    def test_rendered_block_marks_a_complete_value_list_as_exhaustive(self):
        r = self.build().rendered
        self.assertIn("ALL VALUES: 'COMPLETED', 'CANCELLED', 'REFUNDED'", r)

    def test_rendered_block_marks_a_capped_list_as_not_exhaustive(self):
        self.assertIn("not exhaustive", self.build().rendered)

    def test_a_capped_list_says_how_many_it_is_showing_of_how_many(self):
        r = self.build().rendered
        self.assertIn("TOP 20 OF ~300", r)

    def test_identifier_columns_are_tagged(self):
        """A heuristic hint for the grain planner."""
        self.assertIn("[identifier]", self.build().rendered)

    def test_a_numeric_column_shows_its_true_range(self):
        self.assertIn("range 0.0 .. 48500.0", self.build().rendered)

    def test_a_fanned_out_column_is_flagged_as_needing_attribution(self):
        r = self.build().rendered
        self.assertIn("FAN-OUT", r)
        self.assertIn("service_line", r)
        self.assertIn("needs attribution", r)

    def test_a_clean_column_is_not_flagged(self):
        self.assertNotIn("FAN-OUT: 0%", self.build().rendered)

    def test_the_rules_forbid_inventing_a_column_or_a_literal(self):
        r = self.build().rendered
        self.assertIn("Never invent a value", r)
        self.assertIn("say so instead of inventing one", r)

    def test_the_rules_forbid_group_by_as_an_attribution_strategy(self):
        """Adding a fanned-out column to GROUP BY silently changes the grain and
        double-counts every multi-value key."""
        self.assertIn("never by adding it to GROUP BY", self.build().rendered)


class TestBlockOrder(_ContextCase):
    def test_the_three_blocks_render_in_priority_order(self):
        self.approved_metric()
        self.approved_base_view()
        r = self.build("conversion by country").rendered
        self.assertLess(r.index("BUSINESS SEMANTICS"), r.index("BASE VIEWS"))
        self.assertLess(r.index("BASE VIEWS"), r.index("DATABASE SCHEMA"))
        self.assertIn("ALWAYS APPLY: is_test_traffic = false", r)

    def test_an_approved_base_view_renders_as_approved(self):
        self.approved_base_view()
        self.assertIn("[APPROVED]", self.build("conversion by country").rendered)

    def test_a_draft_base_view_renders_as_provisional(self):
        self.registry.upsert(self.tid, BaseView(
            name="guest_checkouts", grain=["guest_id"],
            source_sql="SELECT guest_id FROM guests"), by="planner")
        r = self.build("guest revenue").rendered
        self.assertIn("[DRAFT", r)

    def test_approved_base_views_come_before_drafts(self):
        self.approved_base_view()
        self.registry.upsert(self.tid, BaseView(
            name="guest_checkouts", grain=["guest_id"],
            source_sql="SELECT guest_id FROM guests"), by="planner")
        r = self.build("q").rendered
        self.assertLess(r.index("checkout_sessions"), r.index("guest_checkouts"))

    def test_a_tenant_with_no_base_views_still_builds(self):
        """Day one: no base view exists yet. The turn must proceed and say so."""
        ctx = self.build("revenue by country")
        self.assertEqual(ctx.base_views, [])
        self.assertIn("propose one at ID grain", ctx.rendered)

    def test_the_semantics_block_is_omitted_when_nothing_matched(self):
        self.assertNotIn("BUSINESS SEMANTICS", self.build("revenue by country").rendered)

    def test_the_resolution_is_carried_out_for_the_caveats(self):
        """Task 14 turns unresolved_terms into a visible caveat."""
        self.approved_metric()
        ctx = self.build("what is our churn rate?")
        self.assertIn("churn", ctx.semantics.unresolved_terms)


if __name__ == "__main__":
    unittest.main()
