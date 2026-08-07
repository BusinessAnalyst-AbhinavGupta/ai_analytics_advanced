"""Company Brain v2 migration — mapper unit tests.

The mapper is pure: snapshot dict -> `NodeSpec` drafts. No DB writes here
(the loader, tested separately, owns persistence).
"""
from __future__ import annotations

import os
import unittest

from analytics_platform.domain import NodeKind, ReviewStatus
from analytics_platform.migration.mapper import (
    SNAPSHOT_SOURCE,
    load_snapshot,
    plan_from_snapshot,
)

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


if __name__ == "__main__":
    unittest.main()