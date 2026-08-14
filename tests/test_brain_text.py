"""FTS5 query sanitisation: user questions become safe MATCH expressions."""
from __future__ import annotations

import unittest

from analytics_platform.brain.text import to_fts_query


class ToFtsQueryTest(unittest.TestCase):
    def test_tokens_are_quoted_and_ored(self):
        self.assertEqual(to_fts_query("checkout conversion"),
                         '"checkout" OR "conversion"')

    def test_stopwords_are_dropped(self):
        self.assertEqual(to_fts_query("what is the conversion"), '"conversion"')

    def test_single_character_tokens_are_dropped(self):
        self.assertEqual(to_fts_query("a b conversion"), '"conversion"')

    def test_punctuation_is_stripped(self):
        self.assertEqual(to_fts_query("why did conversion drop?"),
                         '"conversion" OR "drop"')

    def test_fts_operators_are_neutralised(self):
        # Bare AND/OR/NOT would be parsed as syntax; quoting makes them literals,
        # and they are stopwords so they drop out entirely.
        self.assertEqual(to_fts_query("revenue AND cost"), '"revenue" OR "cost"')

    def test_quotes_and_hyphens_do_not_leak(self):
        out = to_fts_query('the "user-churn" rate')
        self.assertEqual(out, '"user" OR "churn" OR "rate"')

    def test_empty_input_returns_empty(self):
        self.assertEqual(to_fts_query(""), "")

    def test_only_stopwords_returns_empty(self):
        self.assertEqual(to_fts_query("what is the"), "")

    def test_numbers_survive(self):
        self.assertEqual(to_fts_query("q3 2026 revenue"),
                         '"q3" OR "2026" OR "revenue"')

    def test_duplicate_tokens_collapse(self):
        self.assertEqual(to_fts_query("revenue revenue"), '"revenue"')


if __name__ == "__main__":
    unittest.main()
