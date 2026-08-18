"""Policy engine tests."""
from __future__ import annotations

import unittest

from analytics_platform.config import PolicySettings
from analytics_platform.execution.policy import QueryPolicy, resolve_template_placeholders


class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.policy = QueryPolicy(PolicySettings())

    def test_allows_read_only_select(self):
        d = self.policy.validate("SELECT color, COUNT(*) n FROM shirt GROUP BY 1")
        self.assertTrue(d.allowed, d.reasons)

    def test_appends_limit_when_absent(self):
        d = self.policy.validate("SELECT * FROM shirt")
        self.assertTrue(d.allowed)
        self.assertIn("LIMIT", d.approved_sql.upper())

    def test_blocks_dml(self):
        for stmt in ("UPDATE shirt SET x=1", "DELETE FROM shirt", "INSERT INTO shirt VALUES (1)"):
            d = self.policy.validate(stmt)
            self.assertFalse(d.allowed)
            self.assertTrue(any("read-only" in r.lower() or "blocked" in r.lower()
                                for r in d.reasons), d.reasons)

    def test_blocks_multi_statement(self):
        d = self.policy.validate("SELECT 1; DROP TABLE shirt;").allowed
        self.assertFalse(d)

    def test_blocks_unlisted_table_when_allowlist_given(self):
        d = self.policy.validate("SELECT * FROM secrets", allowed_tables=["public.shirt"])
        self.assertFalse(d.allowed)
        self.assertTrue(any("allow-list" in r for r in d.reasons), d.reasons)

    def test_allows_listed_table(self):
        d = self.policy.validate("SELECT * FROM shirt", allowed_tables=["public.shirt"])
        self.assertTrue(d.allowed, d.reasons)

    def test_blocks_unresolved_template_placeholder(self):
        # {{Date}}-style syntax is a Metabase UI convention, not valid SQL --
        # LLM-synthesized queries sometimes copy it from example context.
        d = self.policy.validate("SELECT * FROM shirt WHERE {{Date}} AND color = 'red'")
        self.assertFalse(d.allowed)
        self.assertTrue(any("{{Date}}" in r for r in d.reasons), d.reasons)

    def test_allows_literal_curly_braces_outside_template_syntax(self):
        # Sanity check the placeholder regex isn't so broad it flags ordinary
        # SQL that happens to contain braces (e.g. a JSON literal).
        d = self.policy.validate("SELECT * FROM shirt WHERE meta = '{}' ")
        self.assertTrue(d.allowed, d.reasons)


class TestResolveTemplatePlaceholders(unittest.TestCase):
    def test_substitutes_field_filter_with_permissive_condition(self):
        sql, found = resolve_template_placeholders("SELECT * FROM shirt WHERE {{Date}} AND color = 'red'")
        self.assertEqual(found, ["{{Date}}"])
        self.assertEqual(sql, "SELECT * FROM shirt WHERE 1=1 AND color = 'red'")

    def test_no_placeholder_returns_sql_unchanged(self):
        sql, found = resolve_template_placeholders("SELECT * FROM shirt")
        self.assertEqual(sql, "SELECT * FROM shirt")
        self.assertEqual(found, [])

    def test_substitutes_multiple_placeholders(self):
        sql, found = resolve_template_placeholders(
            "SELECT * FROM shirt WHERE {{Date}} AND {{osname}}")
        self.assertEqual(found, ["{{Date}}", "{{osname}}"])
        self.assertEqual(sql, "SELECT * FROM shirt WHERE 1=1 AND 1=1")


class TestTransportCeiling(unittest.TestCase):
    """Task 4 -- three independent 50k caps had to be made to agree, and one of
    them turned out not to be a cap at all."""

    def setUp(self):
        self.policy = QueryPolicy(PolicySettings())

    def test_a_per_request_row_limit_is_injected(self):
        d = self.policy.validate(
            "SELECT session_id, revenue FROM orders WHERE dt >= '2026-01-01'",
            row_limit=50_000, dialect="athena")
        self.assertTrue(d.allowed, d.reasons)
        self.assertIn("LIMIT 50000", d.approved_sql)

    def test_default_path_still_limits_to_50000(self):
        d = self.policy.validate(
            "SELECT country, SUM(revenue) FROM orders WHERE dt >= '2026-01-01' GROUP BY country",
            dialect="athena")
        self.assertIn("LIMIT 50000", d.approved_sql)

    def test_a_request_above_the_transport_ceiling_is_refused(self):
        """No caller may ask a single round trip for more than the transport
        carries. Above this, use a cube (Task 7) or keyset chunks (Task 12)."""
        d = self.policy.validate("SELECT session_id FROM orders",
                                 row_limit=1_000_000, dialect="athena")
        self.assertFalse(d.allowed)
        self.assertTrue(any("transport" in r.lower() for r in d.reasons), d.reasons)

    def test_the_ceiling_is_read_from_settings_not_hardcoded(self):
        """Task 4 Step 3 may well lower this after measuring the real boundary."""
        policy = QueryPolicy(PolicySettings(max_transport_rows=1_000))
        self.assertFalse(policy.validate("SELECT a FROM orders", row_limit=5_000).allowed)
        self.assertTrue(policy.validate("SELECT a FROM orders", row_limit=1_000).allowed)

    def test_exactly_the_ceiling_is_allowed(self):
        d = self.policy.validate("SELECT a FROM orders", row_limit=50_000, dialect="athena")
        self.assertTrue(d.allowed, d.reasons)

    def test_a_cte_query_still_gets_a_limit_injected(self):
        """Every composed cube starts `WITH base AS (`. The policy's injection is
        the single place that decides the warehouse-side bound, so if it skipped
        CTEs the cube path would reach Athena completely unbounded."""
        sql = ("WITH base AS (SELECT session_id, country, revenue FROM orders)\n"
               "SELECT country, SUM(revenue) AS revenue FROM base GROUP BY 1")
        d = self.policy.validate(sql, row_limit=50_000, dialect="athena")
        self.assertTrue(d.allowed, d.reasons)
        self.assertIn("LIMIT 50000", d.approved_sql)

    def test_a_cte_alias_is_not_treated_as_a_table(self):
        """Every composed cube is `WITH base AS (...) SELECT ... FROM base`. If
        `base` counted as a table it would fail every allow-list, which would
        block the entire cube path."""
        sql = ("WITH base AS (SELECT session_id, revenue FROM orders)\n"
               "SELECT SUM(revenue) AS revenue FROM base")
        d = self.policy.validate(sql, allowed_tables=["orders"], dialect="athena")
        self.assertTrue(d.allowed, d.reasons)
        self.assertEqual(self.policy.referenced_tables, ["orders"])

    def test_a_real_table_inside_a_cte_is_still_checked(self):
        sql = ("WITH base AS (SELECT * FROM secrets)\n"
               "SELECT * FROM base")
        d = self.policy.validate(sql, allowed_tables=["orders"], dialect="athena")
        self.assertFalse(d.allowed)
        self.assertTrue(any("secrets" in r for r in d.reasons), d.reasons)

    def test_a_cte_query_that_already_has_a_limit_is_left_alone(self):
        sql = ("WITH base AS (SELECT session_id FROM orders)\n"
               "SELECT session_id FROM base ORDER BY session_id LIMIT 100")
        d = self.policy.validate(sql, row_limit=50_000, dialect="athena")
        self.assertTrue(d.allowed, d.reasons)
        self.assertEqual(d.approved_sql.upper().count("LIMIT"), 1)

    def test_extract_chunk_rows_may_not_exceed_the_transport_ceiling(self):
        s = PolicySettings()
        self.assertLessEqual(s.extract_chunk_rows, s.max_transport_rows)

    def test_the_materialised_ceiling_is_far_above_one_round_trip(self):
        """raw_extract_row_limit bounds a cube summed across chunks, never a
        single round trip -- conflating them is what this task exists to stop."""
        s = PolicySettings()
        self.assertGreater(s.raw_extract_row_limit, s.max_transport_rows)


if __name__ == "__main__":
    unittest.main()