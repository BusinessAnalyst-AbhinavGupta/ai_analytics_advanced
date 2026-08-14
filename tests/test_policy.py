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


if __name__ == "__main__":
    unittest.main()