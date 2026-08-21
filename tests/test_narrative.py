"""Plan B Task 3 -- turning selected turns into a document rather than a pile.

`assemble_storyline` already collects the right *material*. What it cannot do is
narrate: eight selected turns export as eight disconnected question-and-answer
blocks, the same truncation caveat repeated six times, and no through-line. This
module's job is the connective tissue.

The tests below are mostly about what the narrator is NOT allowed to do. A
storyline is the artefact a stakeholder carries into a meeting to defend a
number, so a fabricated figure in it is worse than no storyline at all, and a
caveat quietly dropped during summarisation is worse still. Hence:

  * every section must trace to real answer_ids, or it is dropped;
  * every number in the prose must already appear in the source turns;
  * caveats are unioned in code and never routed through the model;
  * any failure degrades to the un-narrated document instead of raising.
"""
from __future__ import annotations

import unittest

from analytics_platform.llm.client import LLMResponse
from analytics_platform.narrative import NarratedSection, NarratedStoryline, narrate
from analytics_platform.storyline import StorylineContent, StorylineTurn, render_markdown

TRUNCATED = "extract truncated at 1,000,000 rows -- totals may be understated"


def turn(answer_id, question, answer, facts=(), caveats=()):
    return StorylineTurn(answer_id=answer_id, question=question, answer=answer,
                         facts=list(facts), caveats=list(caveats),
                         created_at="2026-08-15T10:00:00Z")


def content_3_turns():
    return StorylineContent(
        conversation_title="Q3 checkout review",
        turns=[
            turn("a1", "what is revenue by country?",
                 "Revenue was 412,003 in DE and 98,120 in US.",
                 facts=["DE leads revenue"], caveats=[TRUNCATED]),
            turn("a2", "break that down by device",
                 "iOS contributed 61.4% of DE revenue.", caveats=[TRUNCATED]),
            turn("a3", "why did US fall?",
                 "US fell after the 12 August pricing change.", caveats=[TRUNCATED]),
        ])


class StubLLM:
    """Returns canned text; records that it was called at most once."""

    name = "gateway"

    def __init__(self, text="", raises=False):
        self.text = text
        self.raises = raises
        self.calls = 0

    def generate(self, prompt="", system_prompt="", **kw):
        self.calls += 1
        if self.raises:
            raise RuntimeError("the model gateway is down")
        return LLMResponse(text=self.text, tokens_in=10, tokens_out=10)


GOOD = """{"title": "Q3 checkout review",
 "executive_summary": "Revenue concentrated in DE at 412,003. US declined after the 12 August pricing change.",
 "sections": [
   {"heading": "DE carries revenue", "body": "DE contributed 412,003, with iOS at 61.4%.",
    "answer_ids": ["a1", "a2"]},
   {"heading": "The US decline", "body": "US revenue of 98,120 fell after 12 August.",
    "answer_ids": ["a3"]}]}"""


class TestNarrate(unittest.TestCase):
    def test_narrate_returns_sections_with_answer_ids(self):
        n = narrate(content_3_turns(), StubLLM(GOOD))
        self.assertTrue(n.ok, n.error)
        self.assertTrue(n.sections)
        self.assertTrue(all(s.answer_ids for s in n.sections))

    def test_the_executive_summary_survives(self):
        n = narrate(content_3_turns(), StubLLM(GOOD))
        self.assertIn("412,003", n.executive_summary)

    def test_every_referenced_answer_id_is_real(self):
        """A section citing a turn that is not in the export is a hallucinated
        provenance claim, and provenance is the whole product here."""
        bogus = GOOD.replace('["a3"]', '["a3", "a99"]')
        n = narrate(content_3_turns(), StubLLM(bogus))
        known = {t.answer_id for t in content_3_turns().turns}
        for s in n.sections:
            self.assertTrue(set(s.answer_ids) <= known, s.answer_ids)

    def test_a_section_citing_nothing_real_is_dropped(self):
        only_bogus = GOOD.replace('["a3"]', '["a99"]')
        n = narrate(content_3_turns(), StubLLM(only_bogus))
        self.assertEqual([s.heading for s in n.sections], ["DE carries revenue"])

    def test_a_fabricated_number_drops_the_section(self):
        """The figure 47.3 appears nowhere in the source turns."""
        lying = GOOD.replace("US revenue of 98,120 fell", "US revenue fell 47.3%")
        n = narrate(content_3_turns(), StubLLM(lying))
        self.assertTrue(all("47.3" not in s.body for s in n.sections))
        self.assertEqual([s.heading for s in n.sections], ["DE carries revenue"])

    def test_a_number_that_is_in_the_source_is_kept(self):
        """The guard must not be so blunt that it eats every real figure."""
        n = narrate(content_3_turns(), StubLLM(GOOD))
        bodies = " ".join(s.body for s in n.sections)
        self.assertIn("412,003", bodies)
        self.assertIn("61.4", bodies)

    def test_an_llm_failure_degrades_instead_of_raising(self):
        n = narrate(content_3_turns(), StubLLM(raises=True))
        self.assertFalse(n.ok)
        self.assertTrue(n.error)

    def test_unparseable_output_degrades_instead_of_raising(self):
        n = narrate(content_3_turns(), StubLLM("I'm afraid I can't do that."))
        self.assertFalse(n.ok)
        self.assertTrue(n.error)

    def test_a_dead_llm_is_not_called_at_all(self):
        class Null:
            name = "null"

            def generate(self, *a, **k):
                raise AssertionError("narrate called a null client")

        n = narrate(content_3_turns(), Null())
        self.assertFalse(n.ok)


class TestCaveats(unittest.TestCase):
    def test_repeated_caveats_are_merged(self):
        self.assertEqual(len(narrate(content_3_turns(), StubLLM(GOOD)).caveats), 1)

    def test_caveats_come_from_the_turns_not_from_the_model(self):
        """Deliberately stronger than the plan asked for. Caveats are the part a
        reader is entitled to see, so they are unioned in code and the model is
        never given the chance to drop one while summarising."""
        c = content_3_turns()
        c.turns[1].caveats = ["'churn' is not a defined metric for this company"]
        n = narrate(c, StubLLM(GOOD))
        self.assertEqual(len(n.caveats), 2)
        self.assertTrue(any("churn" in x for x in n.caveats))

    def test_caveats_survive_an_llm_failure(self):
        n = narrate(content_3_turns(), StubLLM(raises=True))
        self.assertEqual(len(n.caveats), 1)

    def test_whitespace_variants_are_one_caveat(self):
        c = content_3_turns()
        c.turns[1].caveats = ["extract   truncated at 1,000,000 rows -- totals may be understated"]
        self.assertEqual(len(narrate(c, StubLLM(GOOD)).caveats), 1)


class TestRendererFallback(unittest.TestCase):
    def test_renderers_fall_back_when_narrative_is_absent(self):
        c = content_3_turns()
        md = render_markdown(c)
        self.assertIn(c.turns[0].question, md)

    def test_markdown_uses_the_narrative_when_present(self):
        c = content_3_turns()
        c.narrative = narrate(c, StubLLM(GOOD))
        md = render_markdown(c)
        self.assertIn(c.narrative.executive_summary, md)
        self.assertIn("DE carries revenue", md)

    def test_a_failed_narrative_falls_back_to_the_turn_by_turn_document(self):
        """Narration is an enhancement, never a dependency."""
        c = content_3_turns()
        c.narrative = narrate(c, StubLLM(raises=True))
        md = render_markdown(c)
        self.assertIn(c.turns[0].question, md)

    def test_the_narrated_document_still_carries_provenance(self):
        c = content_3_turns()
        c.narrative = narrate(c, StubLLM(GOOD))
        md = render_markdown(c)
        self.assertIn("a1", md)

    def test_the_narrated_document_still_carries_the_code_appendix(self):
        from analytics_platform.storyline import CodeAppendixEntry
        c = content_3_turns()
        c.code_appendix = [CodeAppendixEntry(label="df_1", kind="sql",
                                             code="SELECT 1", source_answer_id="a1",
                                             is_dependency=False)]
        c.narrative = narrate(c, StubLLM(GOOD))
        md = render_markdown(c)
        self.assertIn("SELECT 1", md)

    def test_the_narrated_document_shows_the_caveats(self):
        c = content_3_turns()
        c.narrative = narrate(c, StubLLM(GOOD))
        self.assertIn("truncated", render_markdown(c))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# The export route: narration is opt-in, and never a precondition for exporting.
# ---------------------------------------------------------------------------

from analytics_platform.api import StorylineExportIn, create_app  # noqa: E402

from tests.test_api import app_ctx, call  # noqa: E402


class TestExportRoute(unittest.TestCase):
    def setUp(self):
        self.ctx, self.base = app_ctx()
        self.tid = self.ctx.tenants.create_tenant("NarrCo").id
        self.app = create_app(self.ctx)
        self.svc = self.ctx.stakeholder
        self.cid = self.svc._ensure_conversation(self.tid, "", "narrative")
        out = self.svc.answer(self.tid, "what is revenue by country?",
                              conversation_id=self.cid)
        self.aid = out["answer_id"]

    def tearDown(self):
        self.base.close()

    def _export(self, **kw):
        return call(self.app, "POST",
                    "/stakeholder/{tenant_id}/conversations/{conversation_id}/export",
                    self.tid, self.cid,
                    StorylineExportIn(answer_ids=[self.aid], **kw))

    def test_narrate_defaults_to_off(self):
        self.assertIs(StorylineExportIn(answer_ids=["a1"]).narrate, False)

    def test_export_without_narration_still_works(self):
        r = self._export(format="markdown")
        self.assertTrue(r.body)

    def test_export_with_narration_still_produces_a_document(self):
        """There is no live model in the test context, so narrate() reports
        ok=False -- and the export must still succeed, un-narrated."""
        r = self._export(format="markdown", narrate=True)
        self.assertTrue(r.body)
        self.assertIn(b"revenue by country", r.body)
