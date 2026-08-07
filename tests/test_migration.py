"""Company Brain v2 migration — mapper unit tests.

The mapper is pure: snapshot dict -> `NodeSpec` drafts. No DB writes here
(the loader, tested separately, owns persistence).
"""
from __future__ import annotations

import os
import unittest

from analytics_platform.domain import NodeKind, ReviewStatus
from analytics_platform.migration.loader import migrate_from_snapshot, migrate_specs
from analytics_platform.migration.mapper import (
    SNAPSHOT_SOURCE,
    load_snapshot,
    plan_from_snapshot,
)
from tests.helpers import make_ctx

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "snapshot_seed.json")


class TestMigrationMapper(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = load_snapshot(FIXTURE)
        cls.specs = plan_from_snapshot(cls.snapshot)

    def test_maps_expected_total(self):
        # 2 queries + 4 derived definitions + 2 idioms + 3 rules
        # + 2 stages + 1 table = 14 specs; all default to CANDIDATE
        self.assertEqual(len(self.specs), 14)
        self.assertTrue(all(s.status == ReviewStatus.CANDIDATE for s in self.specs))

    def test_golden_queries_are_query_nodes_with_sql_and_source(self):
        qs = [s for s in self.specs if s.kind == NodeKind.QUERY]
        self.assertEqual(len(qs), 2)
        for s in qs:
            self.assertTrue(s.payload["sql"])
            self.assertIn(s.payload["dialect"].lower(), ("aws athena / presto", "athena"))
            self.assertTrue(s.source_ref.startswith(f"{SNAPSHOT_SOURCE}#golden_queries/"))
            self.assertEqual(s.evidence_ref, s.payload["via"])

    def test_reasoning_enriched_from_intents_by_card_id(self):
        q28369 = next(s for s in self.specs
                      if s.kind == NodeKind.QUERY and s.payload["via"] == "28369")
        self.assertEqual(q28369.payload["reasoning_summary"],
                         "Segments sessions by error recovery.")
        self.assertEqual(q28369.payload["journey_stage"], "Shipping")

    def test_derives_business_definitions_from_filters(self):
        defs = [s for s in self.specs
                if s.kind == NodeKind.DEFINITION and "#definition" in s.source_ref]
        self.assertEqual(len(defs), 4)  # 2 (query 28369) + 2 (query 109)
        columns = {d.payload["column"] for d in defs}
        self.assertTrue({"identifiers_page_name", "attr_error_type", "action", "status"}
                        <= columns)

    def test_derive_definitions_can_be_disabled(self):
        specs = plan_from_snapshot(self.snapshot, derive_definitions=False)
        self.assertFalse(any("#definition" in s.source_ref for s in specs))
        self.assertEqual(len([s for s in specs if s.kind == NodeKind.QUERY]), 2)

    def test_idioms_map_to_idiom_kind(self):
        idioms = [s for s in self.specs if s.kind == NodeKind.IDIOM]
        self.assertEqual(len(idioms), 2)
        self.assertTrue(all(s.payload["sql_skeleton"] for s in idioms))
        self.assertTrue(all(s.source_ref.startswith(f"{SNAPSHOT_SOURCE}#idioms/")
                            for s in idioms))

    def test_rules_map_to_business_rule_kind(self):
        rules = [s for s in self.specs if s.kind == NodeKind.BUSINESS_RULE]
        self.assertEqual(len(rules), 3)
        self.assertTrue(all(s.payload["rule_type"] for s in rules))

    def test_stages_and_tables_map_to_definition_kind(self):
        stages = [s for s in self.specs
                  if s.kind == NodeKind.DEFINITION and s.source_ref.startswith(
                      f"{SNAPSHOT_SOURCE}#stages/")]
        tables = [s for s in self.specs
                  if s.kind == NodeKind.DEFINITION and s.source_ref.startswith(
                      f"{SNAPSHOT_SOURCE}#tables/")]
        self.assertEqual(len(stages), 2)
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].payload["column_count"], 44)

    def test_source_refs_unique(self):
        refs = [s.source_ref for s in self.specs]
        self.assertEqual(len(refs), len(set(refs)))


class TestMigrationLoader(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.tid = self.ctx.tenants.create_tenant("MigrateCo").id
        self.brain = self.ctx.pipeline.brain(self.tid)

    def tearDown(self):
        self.ctx.close()

    def test_migrates_all_as_candidate_with_counts(self):
        summary = migrate_from_snapshot(self.brain, FIXTURE)
        self.assertEqual(summary["imported"], 14)
        self.assertEqual(summary["skipped"], 0)
        nodes = self.brain.all(limit=500)
        self.assertEqual(len(nodes), 14)
        self.assertTrue(all(n.status == ReviewStatus.CANDIDATE for n in nodes))
        self.assertEqual(summary["by_kind"][NodeKind.QUERY.value], 2)
        self.assertEqual(summary["by_kind"][NodeKind.IDIOM.value], 2)
        self.assertEqual(summary["by_kind"][NodeKind.BUSINESS_RULE.value], 3)

    def test_idempotent_rerun(self):
        migrate_from_snapshot(self.brain, FIXTURE)
        summary = migrate_from_snapshot(self.brain, FIXTURE)
        self.assertEqual(summary["imported"], 0)
        self.assertEqual(summary["skipped"], 14)
        self.assertEqual(len(self.brain.all(limit=500)), 14)

    def test_provenance_and_confidence(self):
        migrate_from_snapshot(self.brain, FIXTURE)
        q = next(n for n in self.brain.all(limit=500)
                 if n.kind == NodeKind.QUERY and n.payload.get("via") == "28369")
        self.assertEqual(q.created_by, "migration")
        self.assertTrue(q.source_ref.startswith(
            f"{SNAPSHOT_SOURCE}#golden_queries/"))
        self.assertEqual(q.evidence_ref, "28369")
        self.assertEqual(q.confidence.get("source"), 1.0)
        self.assertEqual(q.payload["journey_stage"], "Shipping")

    def test_tenant_scoped(self):
        other = self.ctx.tenants.create_tenant("OtherCorp").id
        migrate_from_snapshot(self.brain, FIXTURE)
        other_brain = self.ctx.pipeline.brain(other)
        self.assertEqual(len(other_brain.all(limit=500)), 0)

    def test_nothing_auto_approved(self):
        migrate_from_snapshot(self.brain, FIXTURE)
        self.assertEqual(len(self.brain.all(status=ReviewStatus.APPROVED,
                                            limit=500)), 0)

    def test_derive_definitions_off(self):
        summary = migrate_from_snapshot(self.brain, FIXTURE,
                                        derive_definitions=False)
        self.assertEqual(summary["imported"], 10)  # 14 total - 4 derived defs

    def test_real_snapshot_migration_gated(self):
        real = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "extracted_data", "knowledge_graph_snapshot.json")
        if not os.path.exists(real):
            self.skipTest("real snapshot not present")
        summary = migrate_from_snapshot(self.brain, real)
        expected = len(plan_from_snapshot(load_snapshot(real)))
        self.assertEqual(summary["imported"], expected)
        self.assertEqual(summary["skipped"], 0)
        nodes = self.brain.all(limit=10000)
        self.assertEqual(sum(1 for n in nodes if n.kind == NodeKind.QUERY), 158)
        self.assertTrue(all(n.status == ReviewStatus.CANDIDATE for n in nodes))


if __name__ == "__main__":
    unittest.main()