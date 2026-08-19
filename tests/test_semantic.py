"""Task 6 -- the typed semantic layer.

A schema tells the LLM that funnel_events.status exists and holds 'completed'.
It does not tell it that *conversion rate* means completed/eligible, is measured
at session grain, is validly sliced by country/device/channel/date, and excludes
test traffic. Without that the analyst produces queries that are technically
valid and analytically wrong -- and a wrong number delivered confidently is
worse than no number.
"""
from __future__ import annotations

import unittest

from analytics_platform.domain import NodeKind, ReviewStatus, SemanticDimension, SemanticMetric
from analytics_platform.semantic import SemanticLayer
from tests.helpers import make_ctx


def _metric(name="conversion_rate", **kw):
    d = dict(name=name, definition="completed_applications / eligible_applications",
             grain=["session_id"], dimensions=["country", "device", "channel", "date"],
             source_tables=["funnel_events"], filters=["is_test_traffic = false"],
             caveats=["excludes test traffic"], aliases=["CVR", "conversion"],
             freshness="daily, T+1", owner="growth")
    d.update(kw)
    return SemanticMetric(**d)


class _SemanticCase(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.tid = self.ctx.tenants.create_tenant("SemanticCo").id
        self.layer = SemanticLayer(self.ctx.pipeline.brain)

    def tearDown(self):
        self.ctx.close()

    def approve(self, node):
        brain = self.ctx.pipeline.brain(self.tid)
        brain.submit(node.id, by="junior")
        brain.approve(node.id, by="senior")
        return node

    def approved_conversion_metric(self, **kw):
        return self.approve(self.layer.upsert_metric(self.tid, _metric(**kw), by="senior"))


class TestSemanticStorage(_SemanticCase):
    def test_upsert_then_read_roundtrips(self):
        self.layer.upsert_metric(self.tid, _metric(), by="senior")
        got = self.layer.metrics(self.tid, approved_only=False)[0]
        self.assertEqual(got.grain, ["session_id"])
        self.assertEqual(got.filters, ["is_test_traffic = false"])
        self.assertEqual(got.aliases, ["CVR", "conversion"])

    def test_a_new_metric_is_created_unapproved(self):
        """A metric definition silently reshapes every answer that touches it, so
        it passes review first -- the same rule as every other Brain node."""
        node = self.layer.upsert_metric(self.tid, _metric(), by="senior")
        self.assertEqual(node.status, ReviewStatus.CANDIDATE)

    def test_draft_metrics_are_excluded_by_default(self):
        self.layer.upsert_metric(self.tid, _metric(), by="senior")
        self.assertEqual(self.layer.metrics(self.tid), [])
        self.assertEqual(len(self.layer.metrics(self.tid, approved_only=False)), 1)

    def test_upsert_updates_in_place_rather_than_duplicating(self):
        self.layer.upsert_metric(self.tid, _metric(), by="senior")
        self.layer.upsert_metric(self.tid, _metric(definition="c / d"), by="senior")
        all_metrics = self.layer.metrics(self.tid, approved_only=False)
        self.assertEqual(len(all_metrics), 1)
        self.assertEqual(all_metrics[0].definition, "c / d")

    def test_an_upsert_over_an_approved_metric_keeps_it_approved(self):
        """Editing an approved definition must not silently demote it out of every
        prompt -- but it must not sneak past review either; it stays approved and
        the reviewer sees the new payload."""
        self.approved_conversion_metric()
        self.layer.upsert_metric(self.tid, _metric(definition="x / y"), by="senior")
        approved = self.layer.metrics(self.tid)
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0].definition, "x / y")

    def test_metrics_are_stored_as_metric_nodes(self):
        self.layer.upsert_metric(self.tid, _metric(), by="senior")
        titles = {n.title for n in self.ctx.pipeline.brain(self.tid).all(kind=NodeKind.METRIC)}
        self.assertIn("Metric: conversion_rate", titles)

    def test_dimensions_roundtrip_too(self):
        self.layer.upsert_dimension(
            self.tid, SemanticDimension(name="country", column="country",
                                        source_tables=["funnel_events"],
                                        description="billing country"), by="senior")
        got = self.layer.dimensions(self.tid, approved_only=False)[0]
        self.assertEqual(got.column, "country")
        self.assertEqual(got.source_tables, ["funnel_events"])

    def test_a_malformed_payload_is_skipped_not_raised(self):
        """A hand-edited node must not take the chat down."""
        self.ctx.pipeline.brain(self.tid).create(
            NodeKind.METRIC, "Metric: broken", payload={"name": 5, "grain": "not a list"},
            status=ReviewStatus.APPROVED)
        self.assertEqual(self.layer.metrics(self.tid), [])


class TestResolve(_SemanticCase):
    def test_resolve_matches_on_name(self):
        self.approved_conversion_metric()
        res = self.layer.resolve(self.tid, "what is the conversion_rate in Germany?")
        self.assertEqual([m.name for m in res.metrics], ["conversion_rate"])

    def test_resolve_matches_on_alias(self):
        self.approved_conversion_metric()
        res = self.layer.resolve(self.tid, "how did CVR trend in Germany?")
        self.assertEqual([m.name for m in res.metrics], ["conversion_rate"])

    def test_matching_is_case_insensitive_and_punctuation_tolerant(self):
        self.approved_conversion_metric()
        self.assertTrue(self.layer.resolve(self.tid, "cvr, by device?").metrics)

    def test_a_substring_is_not_a_match(self):
        """Whole-token matching only: 'discovery' must not match the alias 'CVR'
        and 'reconversion' must not match 'conversion'."""
        self.approved_conversion_metric()
        self.assertEqual(self.layer.resolve(self.tid, "reconversion discovery").metrics, [])

    def test_resolve_collects_required_filters(self):
        self.approved_conversion_metric()
        res = self.layer.resolve(self.tid, "conversion by device")
        self.assertEqual(res.required_filters, ["is_test_traffic = false"])

    def test_resolve_collects_caveats(self):
        self.approved_conversion_metric()
        self.assertIn("excludes test traffic",
                      self.layer.resolve(self.tid, "conversion by device").caveats)

    def test_a_draft_metric_never_steers_an_answer(self):
        self.layer.upsert_metric(self.tid, _metric(), by="senior")   # not approved
        self.assertEqual(self.layer.resolve(self.tid, "conversion by device").metrics, [])

    def test_an_undefined_measure_becomes_an_unresolved_term(self):
        self.approved_conversion_metric()
        res = self.layer.resolve(self.tid, "what is our churn rate?")
        self.assertIn("churn", res.unresolved_terms)

    def test_a_defined_measure_is_not_reported_as_unresolved(self):
        self.approved_conversion_metric()
        res = self.layer.resolve(self.tid, "what is our conversion rate?")
        self.assertEqual(res.unresolved_terms, [])

    def test_resolve_surfaces_the_dimensions_a_matched_metric_declares(self):
        self.approved_conversion_metric()
        self.approve(self.layer.upsert_dimension(
            self.tid, SemanticDimension(name="country", column="country",
                                        source_tables=["funnel_events"]), by="senior"))
        res = self.layer.resolve(self.tid, "conversion by country")
        self.assertIn("country", [d.name for d in res.dimensions])

    def test_resolve_on_an_empty_layer_is_not_an_error(self):
        res = self.layer.resolve(self.tid, "revenue by country")
        self.assertEqual(res.metrics, [])
        self.assertEqual(res.required_filters, [])


class TestRender(_SemanticCase):
    def test_render_marks_filters_as_mandatory(self):
        self.approved_conversion_metric()
        r = self.layer.render(self.layer.resolve(self.tid, "conversion by country"))
        self.assertIn("ALWAYS APPLY: is_test_traffic = false", r)
        self.assertIn("Grain      : session_id", r)

    def test_render_names_the_aliases_so_the_llm_can_match_the_users_words(self):
        self.approved_conversion_metric()
        r = self.layer.render(self.layer.resolve(self.tid, "CVR by country"))
        self.assertIn("aliases: CVR, conversion", r)

    def test_render_states_that_semantics_override_the_models_assumptions(self):
        self.approved_conversion_metric()
        r = self.layer.render(self.layer.resolve(self.tid, "conversion"))
        self.assertIn("BUSINESS SEMANTICS", r)
        self.assertIn("override your own assumptions", r)

    def test_render_forbids_inventing_a_definition(self):
        self.approved_conversion_metric()
        r = self.layer.render(self.layer.resolve(self.tid, "conversion"))
        self.assertIn("Do not invent a definition", r)

    def test_an_empty_resolution_renders_nothing_rather_than_an_empty_heading(self):
        self.assertEqual(self.layer.render(self.layer.resolve(self.tid, "revenue")), "")

    def test_render_lists_an_unresolved_term_so_it_reaches_the_answer(self):
        self.approved_conversion_metric()
        r = self.layer.render(self.layer.resolve(self.tid, "conversion and churn"))
        self.assertIn("churn", r)
        self.assertIn("NO APPROVED DEFINITION", r)


if __name__ == "__main__":
    unittest.main()
