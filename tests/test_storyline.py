import unittest

from analytics_platform.storyline import (
    StorylineContent, StorylineTurn, CodeAppendixEntry, assemble_storyline, render_markdown,
    render_docx,
)


def _msg(answer_id, question="Q", answer="A", facts=None, caveats=None,
         queries_run=None, python_cells=None, produced_df_label=""):
    return {
        "answer_id": answer_id, "question": question, "answer": answer,
        "facts": facts or [], "caveats": caveats or [],
        "queries_run": queries_run or [], "python_cells": python_cells or [],
        "produced_df_label": produced_df_label, "created_at": "2026-08-15T00:00:00Z",
    }


class TestAssembleStoryline(unittest.TestCase):
    def test_only_selected_turns_appear_in_order(self):
        conv = {"id": "c1", "title": "Test", "messages": [
            _msg("a1", question="First"), _msg("a2", question="Second"),
            _msg("a3", question="Third"),
        ]}
        content = assemble_storyline(conv, ["a3", "a1"])
        self.assertEqual([t.question for t in content.turns], ["First", "Third"])

    def test_empty_selection_yields_empty_content(self):
        conv = {"id": "c1", "title": "Test", "messages": [_msg("a1")]}
        content = assemble_storyline(conv, [])
        self.assertEqual(content.turns, [])
        self.assertEqual(content.code_appendix, [])
        self.assertEqual(content.estimated_tokens, 0)
        self.assertFalse(content.over_budget)

    def test_selected_sql_turn_adds_its_query_as_non_dependency_appendix_entry(self):
        conv = {"id": "c1", "title": "Test", "messages": [
            _msg("a1", queries_run=["SELECT 1"], produced_df_label="df_1"),
        ]}
        content = assemble_storyline(conv, ["a1"])
        self.assertEqual(len(content.code_appendix), 1)
        entry = content.code_appendix[0]
        self.assertEqual(entry.kind, "sql")
        self.assertEqual(entry.code, "SELECT 1")
        self.assertEqual(entry.source_answer_id, "a1")
        self.assertFalse(entry.is_dependency)

    def test_selected_python_turn_pulls_in_unselected_producing_turn_as_dependency(self):
        conv = {"id": "c1", "title": "Test", "messages": [
            _msg("a1", question="SQL turn", queries_run=["SELECT revenue FROM events"],
                 produced_df_label="df_1"),
            _msg("a2", question="Python turn",
                 python_cells=[{"code": "result = df_1['revenue'].sum()",
                                "df_label": "df_1", "result_summary": 6}]),
        ]}
        content = assemble_storyline(conv, ["a2"])  # a1 NOT selected
        self.assertEqual([t.question for t in content.turns], ["Python turn"])
        kinds = {(e.kind, e.source_answer_id, e.is_dependency) for e in content.code_appendix}
        self.assertIn(("python", "a2", False), kinds)
        self.assertIn(("sql", "a1", True), kinds)

    def test_dependency_turn_that_is_also_selected_is_not_double_marked(self):
        conv = {"id": "c1", "title": "Test", "messages": [
            _msg("a1", queries_run=["SELECT 1"], produced_df_label="df_1"),
            _msg("a2", python_cells=[{"code": "result = 1", "df_label": "df_1",
                                      "result_summary": 1}]),
        ]}
        content = assemble_storyline(conv, ["a1", "a2"])
        sql_entries = [e for e in content.code_appendix if e.kind == "sql"]
        self.assertEqual(len(sql_entries), 1)
        self.assertFalse(sql_entries[0].is_dependency)

    def test_token_estimate_and_budget_flag(self):
        conv = {"id": "c1", "title": "Test", "messages": [
            _msg("a1", question="Q" * 100, answer="A" * 100),
        ]}
        content = assemble_storyline(conv, ["a1"])
        self.assertGreater(content.estimated_tokens, 0)
        self.assertFalse(content.over_budget)

        big_conv = {"id": "c1", "title": "Test", "messages": [
            _msg("a1", answer="x" * 250_000),
        ]}
        big_content = assemble_storyline(big_conv, ["a1"])
        self.assertTrue(big_content.over_budget)

    def test_render_markdown_includes_turns_and_dependency_annotated_appendix(self):
        content = StorylineContent(
            conversation_title="Q3 Funnel Review",
            turns=[StorylineTurn(answer_id="a1", question="Why did signups drop?",
                                  answer="Signups dropped 12% after the consent page.",
                                  facts=["computed via SQL"], caveats=[],
                                  created_at="2026-08-15T00:00:00Z")],
            code_appendix=[
                CodeAppendixEntry(label="df_1", kind="sql", code="SELECT 1",
                                  source_answer_id="a0", is_dependency=True),
                CodeAppendixEntry(label="df_1", kind="python", code="result = 1",
                                  source_answer_id="a1", is_dependency=False),
            ],
            estimated_tokens=42, over_budget=False,
        )
        md = render_markdown(content)
        self.assertIn("# Q3 Funnel Review", md)
        self.assertIn("Why did signups drop?", md)
        self.assertIn("Signups dropped 12%", md)
        self.assertIn("```sql", md)
        self.assertIn("```python", md)
        self.assertIn("(included as a dependency of df_1)", md)

    def test_render_docx_produces_a_valid_document_with_turns_and_appendix(self):
        import io
        import docx

        content = StorylineContent(
            conversation_title="Q3 Funnel Review",
            turns=[StorylineTurn(answer_id="a1", question="Why did signups drop?",
                                  answer="Signups dropped 12% after the consent page.",
                                  facts=[], caveats=[], created_at="2026-08-15T00:00:00Z")],
            code_appendix=[CodeAppendixEntry(label="df_1", kind="sql", code="SELECT 1",
                                             source_answer_id="a1", is_dependency=False)],
            estimated_tokens=10, over_budget=False,
        )
        data = render_docx(content)
        self.assertIsInstance(data, bytes)
        doc = docx.Document(io.BytesIO(data))
        full_text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("Q3 Funnel Review", full_text)
        self.assertIn("Why did signups drop?", full_text)
        self.assertIn("Signups dropped 12%", full_text)
        self.assertIn("Code Appendix", full_text)
        self.assertIn("SELECT 1", full_text)
