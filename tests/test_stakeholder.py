"""P6 — Stakeholder analyst tests (reuse approved, refresh, escalate, feedback)."""
from __future__ import annotations

import re
import sys
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from analytics_platform.api import (FeedbackIn, StakeholderIn, ConversationPatchIn,
                                    StorylineExportIn, create_app)
from analytics_platform.domain import AnswerMode, DataSourceKind, NodeKind
from analytics_platform.fixtures import WEEKLY_ORDER_SQL, build_retail_warehouse
from tests.test_api import app_ctx, call


def plan_resp():
    """The planning call _plan_turn makes on every turn (Task 11).

    These fixtures define no base view, so there is no population to resolve:
    the planner falls back to the aggregate path, which is exactly the behaviour
    the tests below were written against. Returning unparseable text is the
    honest way to say "no plan" without inventing a base view.
    """
    return MagicMock(text="", ok=True, tokens_in=0, tokens_out=0)


class TestStakeholder(unittest.TestCase):
    def setUp(self):
        self.ctx, self.base = app_ctx(warehouse=build_retail_warehouse())
        self.tid = self.ctx.tenants.create_tenant("StakeCo", retention_days=90).id
        self.app = create_app(self.ctx)
        self.ctx.tenants.add_datasource(self.tid, "Events", DataSourceKind.DIRECT_DB,
                                        dialect="athena", tables=["events"])
        self.ctx.pipeline.register_approved_query(
            self.tid, WEEKLY_ORDER_SQL, "monthly retail orders",
            "how many retail orders per month", by="admin")

    def tearDown(self):
        self.base.close()

    def test_reuse_approved_query_with_citation(self):
        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        self.assertEqual(res["answer_mode"], AnswerMode.REFRESHED_APPROVED_QUERY.value)
        self.assertEqual(res["status"], "ANSWERED")
        self.assertFalse(res["escalated"])
        self.assertEqual(len(res["citations"]), 1)
        self.assertEqual(res["citations"][0]["title"], "monthly retail orders")
        self.assertIn("monthly retail orders", res["answer"])

    def test_answer_creates_and_reuses_conversation(self):
        res1 = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        cid = res1["conversation_id"]
        self.assertTrue(cid)
        res2 = self.ctx.stakeholder.answer(self.tid, "and last month specifically?",
                                           conversation_id=cid)
        self.assertEqual(res2["conversation_id"], cid)
        conv = self.ctx.stakeholder.get_conversation(self.tid, cid)
        self.assertEqual(len(conv["messages"]), 2)

    def test_reuse_resolves_metabase_template_placeholder(self):
        # A stored query authored in Metabase's native editor can carry a
        # {{Date}}-style Field Filter tag -- valid inside Metabase's own
        # parameter UI, not valid raw SQL once reused verbatim outside it.
        templated_sql = (
            "SELECT date_format(CAST(created_at AS TIMESTAMP), '%Y-%m') AS month, "
            "COUNT(*) AS orders FROM events WHERE {{Date}} AND action = 'order' "
            "GROUP BY 1 ORDER BY 1 LIMIT 40"
        )
        self.ctx.pipeline.register_approved_query(
            self.tid, templated_sql, "templated retail orders",
            "how many templated retail orders per month", by="admin")
        res = self.ctx.stakeholder.answer(self.tid, "how many templated retail orders per month")
        self.assertEqual(res["status"], "ANSWERED")
        self.assertEqual(res["answer_mode"], AnswerMode.REFRESHED_APPROVED_QUERY.value)
        self.assertTrue(any("{{Date}}" in c and "no filter" in c for c in res["caveats"]),
                        res["caveats"])

    def test_approved_definition_falls_through(self):
        brain = self.ctx.pipeline.brain(self.tid)
        d = brain.create(NodeKind.DEFINITION, "gross margin",
                         summary="gross margin is revenue minus cost of goods sold")
        brain.submit(d.id, by="junior")
        brain.approve(d.id, by="senior")
        res = self.ctx.stakeholder.answer(self.tid, "gross margin")
        self.assertEqual(res["answer_mode"], AnswerMode.DIRECT_FROM_APPROVED_KNOWLEDGE.value)
        self.assertIn("gross margin", res["answer"])

    def test_high_risk_escalates(self):
        res = self.ctx.stakeholder.answer(self.tid, "list the personally identifiable info we store")
        self.assertTrue(res["escalated"])
        self.assertEqual(res["answer_mode"], AnswerMode.REQUIRES_SENIOR_REVIEW.value)

    def test_no_approved_knowledge_cannot_answer(self):
        res = self.ctx.stakeholder.answer(self.tid, "explain our warehouse picking policy in "
                                                    "minute detail")
        self.assertEqual(res["answer_mode"], AnswerMode.CANNOT_ANSWER.value)

    def test_feedback_and_quality(self):
        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        fb = self.ctx.stakeholder.record_feedback(self.tid, res["answer_id"], "sarah", "up")
        self.assertEqual(fb["rating"], "up")
        q = self.ctx.stakeholder.quality(self.tid)
        self.assertEqual(q["total_questions"], 1)
        self.assertEqual(q["feedback_count"], 1)
        self.assertEqual(q["acceptance_rate"], 1.0)
        self.assertEqual(q["reuse_count"], 1)

    def test_routes(self):
        res = call(self.app, "POST", "/stakeholder/{tenant_id}/answer", self.tid,
                   StakeholderIn(question="how many retail orders per month"))
        self.assertEqual(res["answer_mode"], AnswerMode.REFRESHED_APPROVED_QUERY.value)
        fb = call(self.app, "POST", "/stakeholder/{tenant_id}/feedback", self.tid,
                  FeedbackIn(answer_id=res["answer_id"], rating="up"))
        self.assertEqual(fb["rating"], "up")
        q = call(self.app, "GET", "/stakeholder/{tenant_id}/quality", self.tid)
        self.assertEqual(q["feedback_count"], 1)

    def test_answer_route_accepts_and_returns_conversation_id(self):
        res = call(self.app, "POST", "/stakeholder/{tenant_id}/answer", self.tid,
                   StakeholderIn(question="how many retail orders per month"))
        self.assertTrue(res["conversation_id"])

    def test_conversation_routes(self):
        res = call(self.app, "POST", "/stakeholder/{tenant_id}/answer", self.tid,
                   StakeholderIn(question="how many retail orders per month"))
        cid = res["conversation_id"]

        listed = call(self.app, "GET", "/stakeholder/{tenant_id}/conversations", self.tid)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], cid)

        got = call(self.app, "GET", "/stakeholder/{tenant_id}/conversations/{conversation_id}",
                   self.tid, cid)
        self.assertEqual(got["id"], cid)
        self.assertEqual(len(got["messages"]), 1)

        patched = call(self.app, "PATCH", "/stakeholder/{tenant_id}/conversations/{conversation_id}",
                       self.tid, cid, ConversationPatchIn(title="Renamed", starred=True))
        self.assertEqual(patched["title"], "Renamed")
        self.assertTrue(patched["starred"])

        deleted = call(self.app, "DELETE", "/stakeholder/{tenant_id}/conversations/{conversation_id}",
                       self.tid, cid)
        self.assertEqual(deleted["deleted"], cid)

    def test_get_missing_conversation_route_404s(self):
        with self.assertRaises(HTTPException):
            call(self.app, "GET", "/stakeholder/{tenant_id}/conversations/{conversation_id}",
                self.tid, "nope")

    def test_export_markdown_returns_a_markdown_document(self):
        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        cid = res["conversation_id"]
        aid = res["answer_id"]
        resp = call(self.app, "POST",
                   "/stakeholder/{tenant_id}/conversations/{conversation_id}/export",
                   self.tid, cid, StorylineExportIn(answer_ids=[aid], format="markdown"))
        self.assertEqual(resp.media_type, "text/markdown")
        self.assertIn("attachment", resp.headers["content-disposition"])
        self.assertIn("how many retail orders per month", resp.body.decode("utf-8"))

    def test_export_docx_returns_an_openxml_document(self):
        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        cid = res["conversation_id"]
        aid = res["answer_id"]
        resp = call(self.app, "POST",
                   "/stakeholder/{tenant_id}/conversations/{conversation_id}/export",
                   self.tid, cid, StorylineExportIn(answer_ids=[aid], format="docx"))
        self.assertEqual(resp.media_type,
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertIn("attachment", resp.headers["content-disposition"])
        self.assertTrue(resp.body.startswith(b"PK"))  # docx is a zip archive

    def test_cors_exposes_content_disposition_for_export_downloads(self):
        # The route tests above call the endpoint closure directly, so they never exercise
        # middleware. Content-Disposition is not a CORS-safelisted response header: without
        # expose_headers the browser hides it from fetch() and the frontend's filename
        # extraction silently falls back to a generic "storyline.md".
        cors = [m for m in self.app.user_middleware if m.cls is CORSMiddleware]
        self.assertEqual(len(cors), 1, "expected exactly one CORSMiddleware")
        self.assertIn("Content-Disposition", cors[0].kwargs["expose_headers"])

    def test_export_unknown_conversation_is_404(self):
        with self.assertRaises(HTTPException) as cm:
            call(self.app, "POST",
                "/stakeholder/{tenant_id}/conversations/{conversation_id}/export",
                self.tid, "does-not-exist",
                StorylineExportIn(answer_ids=["x"], format="markdown"))
        self.assertEqual(cm.exception.status_code, 404)

    def test_export_empty_answer_ids_is_400(self):
        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        cid = res["conversation_id"]
        with self.assertRaises(HTTPException) as cm:
            call(self.app, "POST",
                "/stakeholder/{tenant_id}/conversations/{conversation_id}/export",
                self.tid, cid, StorylineExportIn(answer_ids=[], format="markdown"))
        self.assertEqual(cm.exception.status_code, 400)

    def test_export_rejects_an_answer_id_from_a_different_conversation(self):
        """The plan's Global Constraint: answer_ids are validated against *this*
        conversation, not mere existence. A refactor that moved the check to a global
        stakeholder_answers lookup would pass every other test while enabling a
        cross-conversation read."""
        a = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        b = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        self.assertNotEqual(a["conversation_id"], b["conversation_id"])
        with self.assertRaises(HTTPException) as cm:
            call(self.app, "POST",
                 "/stakeholder/{tenant_id}/conversations/{conversation_id}/export",
                 self.tid, a["conversation_id"],
                 StorylineExportIn(answer_ids=[b["answer_id"]], format="markdown"))
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("unknown answer_id", str(cm.exception.detail))

    def test_export_unsupported_format_is_400(self):
        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        with self.assertRaises(HTTPException) as cm:
            call(self.app, "POST",
                 "/stakeholder/{tenant_id}/conversations/{conversation_id}/export",
                 self.tid, res["conversation_id"],
                 StorylineExportIn(answer_ids=[res["answer_id"]], format="pdf"))
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("unsupported format", str(cm.exception.detail))

    def test_export_with_a_non_ascii_title_succeeds_with_an_ascii_filename(self):
        """Starlette encodes response headers as latin-1 inside Response.__init__, and
        str.isalnum() is Unicode-aware, so a CJK/Cyrillic title used to survive the
        slug verbatim and blow up with UnicodeEncodeError before the response existed.
        Titles are auto-derived from the user's question, so this was reachable for
        any non-English analyst."""
        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        cid = res["conversation_id"]
        call(self.app, "PATCH", "/stakeholder/{tenant_id}/conversations/{conversation_id}",
             self.tid, cid, ConversationPatchIn(title="Q3 売上分析"))

        resp = call(self.app, "POST",
                    "/stakeholder/{tenant_id}/conversations/{conversation_id}/export",
                    self.tid, cid,
                    StorylineExportIn(answer_ids=[res["answer_id"]], format="markdown"))
        disposition = resp.headers["content-disposition"]
        # Latin-1 encodable (this is what actually raised before the fix).
        disposition.encode("latin-1")
        self.assertIn('filename="Q3.md"', disposition)
        self.assertIn("filename*=UTF-8''", disposition)
        # The RFC 5987 form carries the real title, percent-encoded.
        self.assertIn(quote("Q3 売上分析.md", safe=""), disposition)
        # The frontend's regex only reads the quoted form -- it must still match.
        self.assertEqual(re.search(r'filename="([^"]+)"', disposition).group(1), "Q3.md")

    def test_export_filename_slug_is_bounded(self):
        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        cid = res["conversation_id"]
        call(self.app, "PATCH", "/stakeholder/{tenant_id}/conversations/{conversation_id}",
             self.tid, cid, ConversationPatchIn(title="A" * 5000))
        resp = call(self.app, "POST",
                    "/stakeholder/{tenant_id}/conversations/{conversation_id}/export",
                    self.tid, cid,
                    StorylineExportIn(answer_ids=[res["answer_id"]], format="markdown"))
        name = re.search(r'filename="([^"]+)"', resp.headers["content-disposition"]).group(1)
        self.assertEqual(name, "A" * 60 + ".md")

    def test_export_docx_is_503_when_python_docx_is_unavailable(self):
        """python-docx is imported lazily inside render_docx, so a broken install
        disables the Word format alone instead of taking down the whole API."""
        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        cid = res["conversation_id"]
        with patch.dict(sys.modules, {"docx": None}):
            with self.assertRaises(HTTPException) as cm:
                call(self.app, "POST",
                     "/stakeholder/{tenant_id}/conversations/{conversation_id}/export",
                     self.tid, cid,
                     StorylineExportIn(answer_ids=[res["answer_id"]], format="docx"))
            self.assertEqual(cm.exception.status_code, 503)
            self.assertEqual(cm.exception.detail,
                             "docx export unavailable: python-docx not installed")
            # Markdown export still works with python-docx missing.
            ok = call(self.app, "POST",
                      "/stakeholder/{tenant_id}/conversations/{conversation_id}/export",
                      self.tid, cid,
                      StorylineExportIn(answer_ids=[res["answer_id"]], format="markdown"))
            self.assertEqual(ok.media_type, "text/markdown")

    def test_disabled_stakeholder_returns_graceful_message(self):
        self.ctx.tenants.set_analyst_config(self.tid, {"stakeholder": {"enabled": False}})
        res = self.ctx.stakeholder.answer(self.tid, "explain our warehouse picking policy")
        self.assertEqual(res["answer_mode"], AnswerMode.CANNOT_ANSWER.value)
        self.assertIn("disabled", res["answer"].lower())

    @patch("analytics_platform.stakeholder.make_role_client")
    def test_dynamic_llm_resolution(self, mock_make_role_client):
        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.return_value.text = '{"answer": "Synthesized stakeholder answer text."}'
        mock_llm.generate.return_value.tokens_in = 100
        mock_llm.generate.return_value.tokens_out = 50
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "stakeholder": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-haiku"}
        })

        res = self.ctx.stakeholder.answer(self.tid, "explain our warehouse picking policy")
        self.assertEqual(res["answer_mode"], AnswerMode.NEW_LOW_RISK_ANALYSIS.value)
        self.assertEqual(res["answer"], "Synthesized stakeholder answer text.")

        mock_make_role_client.assert_called_once()
        args, _ = mock_make_role_client.call_args
        self.assertIs(args[0], self.ctx.stakeholder.settings)
        self.assertEqual(args[1].provider, "openrouter")
        self.assertEqual(args[1].model, "anthropic/claude-3-haiku")

    @patch("analytics_platform.stakeholder.make_role_client")
    def test_failed_approved_query_falls_through_to_freeform_instead_of_denial(self, mock_make_role_client):
        """A question can semantically match an 'approved' query that is actually
        broken (stale column, wrong table, dialect mismatch) -- that is a bad
        retrieval hit, not proof the question is unanswerable. answer() must
        degrade further (skill-match, then freeform synthesis) rather than
        stopping at CANNOT_ANSWER the moment every matched approved query fails
        to execute."""
        self.ctx.pipeline.register_approved_query(
            self.tid, "SELECT * FROM this_table_does_not_exist", "broken gizmo report",
            "how many gizmo widgets were dispatched", by="admin")

        intent_resp = MagicMock(text="gizmo widgets", ok=True, tokens_in=0, tokens_out=0)
        no_sql_resp = MagicMock(text="", tokens_in=0, tokens_out=0)  # SQL synthesis declines
        skill_null_resp = MagicMock(
            text='{"skill_name": null, "reasoning": "no specialized skill fits"}',
            tokens_in=0, tokens_out=0)
        freeform_resp = MagicMock(
            text='{"answer": "Fallback synthesized answer after approved query failed."}',
            tokens_in=40, tokens_out=20)

        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [intent_resp, plan_resp(), no_sql_resp, skill_null_resp, freeform_resp]
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "stakeholder": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-haiku"}
        })

        res = self.ctx.stakeholder.answer(self.tid, "how many gizmo widgets were dispatched")
        self.assertNotEqual(res["answer_mode"], AnswerMode.CANNOT_ANSWER.value)
        self.assertEqual(res["answer_mode"], AnswerMode.NEW_LOW_RISK_ANALYSIS.value)
        self.assertEqual(res["status"], "ANSWERED")
        self.assertEqual(res["answer"], "Fallback synthesized answer after approved query failed.")

    @patch("analytics_platform.stakeholder.make_role_client")
    def test_irrelevant_definition_match_falls_through_to_skill_match(self, mock_make_role_client):
        """Brain retrieval is purely rank-based (RRF fusion, no absolute
        relevance threshold -- see brain/fusion.py) -- a DEFINITION node can
        rank best simply because nothing else in the corpus is closer, not
        because it actually addresses the question. Reciting it must not
        preempt a purpose-built skill that can actually answer: when the LLM
        is live, skill-matching gets first chance, and the definition is
        used only as a fallback if no skill fits."""
        brain = self.ctx.pipeline.brain(self.tid)
        d = brain.create(NodeKind.DEFINITION, "username_continue",
                         summary="username_continue is an unrelated login-form field flag")
        brain.submit(d.id, by="junior")
        d = brain.approve(d.id, by="senior")

        # Retrieval ranking is a separate, already-covered concern (Brain's
        # RRF fusion has no absolute relevance threshold -- see
        # brain/fusion.py); pin _retrieve's output directly so this test
        # deterministically exercises answer()'s handling of "a definition
        # got retrieved but is irrelevant", regardless of whether this
        # specific synthetic corpus would rank it best in a real search.
        self.ctx.stakeholder._retrieve = MagicMock(return_value=([], [d]))

        intent_resp = MagicMock(text="widget dropoff", ok=True, tokens_in=0, tokens_out=0)
        no_sql_resp = MagicMock(text="", tokens_in=0, tokens_out=0)  # SQL synthesis declines
        synth_resp = MagicMock(
            text='{"answer": "Real skill-computed drop-off analysis."}',
            tokens_in=15, tokens_out=10)

        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [intent_resp, plan_resp(), no_sql_resp, synth_resp]
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "stakeholder": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-haiku"}
        })

        fake_skill = MagicMock()
        fake_skill.meta.name = "fake-dropoff-skill"
        self.ctx.stakeholder.skill_engine = MagicMock()
        self.ctx.stakeholder.skill_engine.match.return_value = MagicMock(skill_name="fake-dropoff-skill")
        self.ctx.stakeholder.skill_engine.extract_params.return_value = ({}, False, "")
        self.ctx.stakeholder.skill_engine.execute.return_value = MagicMock(
            ok=True, error="", queries_run=["SELECT 1"],
            data_previews=[{"preview": [{"widgets_dropped": 42}]}])
        self.ctx.stakeholder.skill_registry.get_skill = MagicMock(return_value=fake_skill)

        res = self.ctx.stakeholder.answer(self.tid, "why are widgets dropping off before dispatch")
        self.assertEqual(res["answer_mode"], AnswerMode.SKILL_EXECUTED_ANALYSIS.value)
        self.assertEqual(res["status"], "ANSWERED")
        self.assertEqual(res["answer"], "Real skill-computed drop-off analysis.")

    @patch("analytics_platform.stakeholder.make_role_client")
    def test_approved_query_chart_synthesis_token_accounting(self, mock_make_role_client):
        # answer() now makes 3 LLM calls for this path: search-intent extraction,
        # a SQL-synthesis attempt, then chart synthesis. Give each a response shaped
        # like what that specific prompt would actually get back, so the mock
        # exercises the real call sequence instead of a single fixed reply that
        # happened to work before intent extraction existed.
        intent_resp = MagicMock(text="retail orders", ok=True, tokens_in=0, tokens_out=0)
        no_sql_resp = MagicMock(text="", tokens_in=0, tokens_out=0)  # "context insufficient"
        chart_resp = MagicMock(
            text='{"answer": "Reused answer", "chart_config": {"type": "BarChart"}}',
            tokens_in=200, tokens_out=80)

        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [intent_resp, plan_resp(), no_sql_resp, chart_resp]
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "stakeholder": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-haiku"}
        })

        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        self.assertEqual(res["answer_mode"], AnswerMode.REFRESHED_APPROVED_QUERY.value)
        self.assertIsNotNone(res["chart_config"])
        self.assertEqual(res["chart_config"]["type"], "BarChart")
        self.assertTrue(len(res["chart_data"]) > 0)
        self.assertGreater(res["cost"], 0.0)

    def test_extract_search_intent_passes_through_when_llm_not_configured(self):
        """No live LLM (default NullClient) -> intent extraction is a no-op, and
        retrieval still runs on the original question."""
        from analytics_platform.llm.client import NullClient
        intent = self.ctx.stakeholder._extract_search_intent(NullClient(), "how many retail orders per month")
        self.assertEqual(intent, "how many retail orders per month")

    @patch("analytics_platform.stakeholder.make_role_client")
    def test_synthesized_sql_answers_from_adapted_approved_context(self, mock_make_role_client):
        """A live LLM that returns real SQL from approved context takes the
        ADAPTED_APPROVED_QUERY path -- ad-hoc SQL, not the verbatim approved query."""
        intent_resp = MagicMock(text="retail orders", ok=True, tokens_in=0, tokens_out=0)
        synthesized_sql = (
            "```sql\n"
            "SELECT COUNT(*) AS orders FROM events WHERE action = 'order'\n"
            "```"
        )
        sql_resp = MagicMock(text=synthesized_sql, tokens_in=150, tokens_out=40)
        answer_resp = MagicMock(
            text='{"answer": "There were N orders.", "chart_config": {"type": "BarChart"}}',
            tokens_in=90, tokens_out=30)

        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [intent_resp, plan_resp(), sql_resp, answer_resp]
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "stakeholder": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-haiku"}
        })

        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        self.assertEqual(res["answer_mode"], AnswerMode.ADAPTED_APPROVED_QUERY.value)
        self.assertEqual(res["status"], "ANSWERED")
        self.assertEqual(len(res["queries_run"]), 1)
        # The executed SQL is the synthesized query as approved by QueryPolicy --
        # confirms the policy gate actually ran (it appends LIMIT when absent),
        # not just that some SQL executed.
        self.assertIn("SELECT COUNT(*) AS orders FROM events WHERE action = 'order'",
                      res["queries_run"][0])
        self.assertIn("LIMIT", res["queries_run"][0])
        self.assertEqual(len(res["citations"]), 1)
        self.assertEqual(res["citations"][0]["title"], "monthly retail orders")
        self.assertIn("dynamically generated SQL", res["caveats"])
        self.assertIsNotNone(res["chart_config"])
        self.assertTrue(len(res["chart_data"]) > 0)
        self.assertGreater(res["cost"], 0.0)

    @patch("analytics_platform.stakeholder.make_role_client")
    def test_successful_sql_synthesis_caches_the_resulting_dataframe(self, mock_make_role_client):
        """A successful synthesized-SQL turn should populate the per-conversation
        DataFrame cache so later tasks (compute-engine reuse) can read the result
        back without re-running the query."""
        intent_resp = MagicMock(text="retail orders", ok=True, tokens_in=0, tokens_out=0)
        synthesized_sql = (
            "```sql\n"
            "SELECT COUNT(*) AS orders FROM events WHERE action = 'order'\n"
            "```"
        )
        sql_resp = MagicMock(text=synthesized_sql, tokens_in=150, tokens_out=40)
        answer_resp = MagicMock(
            text='{"answer": "There were N orders.", "chart_config": {"type": "BarChart"}}',
            tokens_in=90, tokens_out=30)

        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [intent_resp, plan_resp(), sql_resp, answer_resp]
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "stakeholder": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-haiku"}
        })

        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month",
                                          conversation_id="")

        available = self.ctx.stakeholder.data_cache.list_available(self.tid, res["conversation_id"])
        self.assertEqual(len(available), 1)
        self.assertEqual(available[0]["label"], "df_1")

    @patch("analytics_platform.stakeholder.make_role_client")
    def test_sql_synthesis_repairs_after_policy_rejection(self, mock_make_role_client):
        """First attempt leaves in a Metabase {{Date}} placeholder (policy-rejected);
        the repair loop feeds that back to the LLM and succeeds on attempt 2,
        rather than giving up after the first bad query."""
        intent_resp = MagicMock(text="retail orders", ok=True, tokens_in=0, tokens_out=0)
        bad_sql_resp = MagicMock(
            text="```sql\nSELECT COUNT(*) AS orders FROM events WHERE {{Date}}\n```",
            tokens_in=50, tokens_out=15)
        fixed_sql_resp = MagicMock(
            text="```sql\nSELECT COUNT(*) AS orders FROM events WHERE action = 'order'\n```",
            tokens_in=60, tokens_out=20)
        answer_resp = MagicMock(text='{"answer": "There were N orders."}', tokens_in=90, tokens_out=30)

        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [intent_resp, plan_resp(), bad_sql_resp, fixed_sql_resp, answer_resp]
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "stakeholder": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-haiku"}
        })

        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        self.assertEqual(res["answer_mode"], AnswerMode.ADAPTED_APPROVED_QUERY.value)
        self.assertEqual(res["status"], "ANSWERED")
        self.assertNotIn("{{", res["queries_run"][0])
        self.assertIn("SELECT COUNT(*) AS orders FROM events WHERE action = 'order'",
                      res["queries_run"][0])
        # The repair prompt for attempt 2 must actually mention the rejection
        # reason, not just retry the same broken query blind.
        second_call_kwargs = mock_llm.generate.call_args_list[3].kwargs
        self.assertIn("{{Date}}", second_call_kwargs["prompt"])

    @patch("analytics_platform.stakeholder.make_role_client")
    def test_sql_synthesis_repairs_after_execution_failure(self, mock_make_role_client):
        """First attempt is valid SQL that fails at execution (bad table name);
        the repair loop feeds the execution error back and succeeds on attempt 2."""
        intent_resp = MagicMock(text="retail orders", ok=True, tokens_in=0, tokens_out=0)
        bad_table_resp = MagicMock(
            text="```sql\nSELECT COUNT(*) AS orders FROM nonexistent_table\n```",
            tokens_in=50, tokens_out=15)
        fixed_sql_resp = MagicMock(
            text="```sql\nSELECT COUNT(*) AS orders FROM events WHERE action = 'order'\n```",
            tokens_in=60, tokens_out=20)
        answer_resp = MagicMock(text='{"answer": "There were N orders."}', tokens_in=90, tokens_out=30)

        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [intent_resp, plan_resp(), bad_table_resp, fixed_sql_resp, answer_resp]
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "stakeholder": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-haiku"}
        })

        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        self.assertEqual(res["answer_mode"], AnswerMode.ADAPTED_APPROVED_QUERY.value)
        self.assertEqual(res["status"], "ANSWERED")
        self.assertIn("FROM events", res["queries_run"][0])

    @patch("analytics_platform.stakeholder.make_role_client")
    def test_sql_synthesis_stops_after_max_attempts_and_falls_back(self, mock_make_role_client):
        """A query that never becomes valid stops retrying at the cap (3 synthesis
        attempts) and falls back to verbatim reuse, rather than looping forever."""
        intent_resp = MagicMock(text="retail orders", ok=True, tokens_in=0, tokens_out=0)
        always_bad_resp = MagicMock(
            text="```sql\nSELECT COUNT(*) AS orders FROM events WHERE {{Date}}\n```",
            tokens_in=50, tokens_out=15)
        answer_resp = MagicMock(text='{"answer": "Reused answer"}', tokens_in=10, tokens_out=5)

        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        # intent + 3 synthesis attempts (all rejected) + final chart synthesis
        # on the verbatim-reuse fallback path
        mock_llm.generate.side_effect = [
            intent_resp, plan_resp(), always_bad_resp, always_bad_resp, always_bad_resp, answer_resp]
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "stakeholder": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-haiku"}
        })

        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        self.assertEqual(res["answer_mode"], AnswerMode.REFRESHED_APPROVED_QUERY.value)
        self.assertEqual(mock_llm.generate.call_count, 6)

    @patch("analytics_platform.stakeholder.make_role_client")
    def test_sql_synthesis_falls_back_when_llm_declines(self, mock_make_role_client):
        """An LLM that returns nothing for SQL synthesis (its documented
        'context insufficient' behaviour) falls through to reusing the
        approved query verbatim, not a crash or an empty answer."""
        intent_resp = MagicMock(text="retail orders", ok=True, tokens_in=0, tokens_out=0)
        no_sql_resp = MagicMock(text="", tokens_in=0, tokens_out=0)
        answer_resp = MagicMock(text='{"answer": "Reused answer"}', tokens_in=10, tokens_out=5)

        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [intent_resp, plan_resp(), no_sql_resp, answer_resp]
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "stakeholder": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-haiku"}
        })

        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        self.assertEqual(res["answer_mode"], AnswerMode.REFRESHED_APPROVED_QUERY.value)

    @patch("analytics_platform.stakeholder.make_role_client")
    def test_sql_synthesis_falls_back_when_policy_rejects_synthesized_sql(self, mock_make_role_client):
        """Synthesized SQL is LLM-authored and un-reviewed -- a write statement must
        be blocked by QueryPolicy (same read-only gate the structured pipeline
        applies) on every attempt, exhausting the repair loop and falling through
        to verbatim reuse, never reaching the executor."""
        intent_resp = MagicMock(text="retail orders", ok=True, tokens_in=0, tokens_out=0)
        unsafe_sql_resp = MagicMock(
            text="```sql\nDELETE FROM events WHERE 1=1\n```", tokens_in=50, tokens_out=10)
        answer_resp = MagicMock(text='{"answer": "Reused answer"}', tokens_in=10, tokens_out=5)

        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        # intent + 3 synthesis attempts (all the same unsafe write, all rejected)
        # + final chart synthesis on the verbatim-reuse fallback path
        mock_llm.generate.side_effect = [
            intent_resp, plan_resp(), unsafe_sql_resp, unsafe_sql_resp, unsafe_sql_resp, answer_resp]
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "stakeholder": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-haiku"}
        })

        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        self.assertEqual(res["answer_mode"], AnswerMode.REFRESHED_APPROVED_QUERY.value)
        self.assertNotIn("DELETE", " ".join(res["queries_run"]))
        self.assertEqual(mock_llm.generate.call_count, 6)

    @patch("analytics_platform.stakeholder.make_role_client")
    def test_sql_synthesis_falls_back_when_generated_sql_fails_execution(self, mock_make_role_client):
        """Synthesized SQL against the allow-listed 'events' table (passes policy)
        but references a column that doesn't exist -- a genuine execution-time
        failure, not a policy rejection. Fails on every attempt, exhausting the
        repair loop and falling through to the verbatim-reuse path."""
        intent_resp = MagicMock(text="retail orders", ok=True, tokens_in=0, tokens_out=0)
        bad_column_resp = MagicMock(
            text="```sql\nSELECT nonexistent_column FROM events\n```",
            tokens_in=50, tokens_out=10)
        answer_resp = MagicMock(text='{"answer": "Reused answer"}', tokens_in=10, tokens_out=5)

        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [
            intent_resp, plan_resp(), bad_column_resp, bad_column_resp, bad_column_resp, answer_resp]
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "stakeholder": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-haiku"}
        })

        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        self.assertEqual(res["answer_mode"], AnswerMode.REFRESHED_APPROVED_QUERY.value)
        self.assertEqual(mock_llm.generate.call_count, 6)

    def test_ensure_conversation_creates_then_reuses(self):
        svc = self.ctx.stakeholder
        cid = svc._ensure_conversation(self.tid, "", "how many retail orders per month")
        self.assertTrue(cid)
        again = svc._ensure_conversation(self.tid, cid, "a follow-up question")
        self.assertEqual(again, cid)

    def test_ensure_conversation_unknown_id_starts_new(self):
        svc = self.ctx.stakeholder
        cid = svc._ensure_conversation(self.tid, "not-a-real-id", "how many retail orders per month")
        self.assertNotEqual(cid, "not-a-real-id")

    def test_list_and_get_conversation(self):
        svc = self.ctx.stakeholder
        cid = svc._ensure_conversation(self.tid, "", "how many retail orders per month")
        convs = svc.list_conversations(self.tid)
        self.assertEqual(len(convs), 1)
        self.assertEqual(convs[0]["id"], cid)
        self.assertIn("title", convs[0])
        got = svc.get_conversation(self.tid, cid)
        self.assertEqual(got["id"], cid)
        self.assertEqual(got["messages"], [])

    def test_get_conversation_missing_returns_none(self):
        self.assertIsNone(self.ctx.stakeholder.get_conversation(self.tid, "nope"))

    def test_update_conversation_rename_and_star(self):
        svc = self.ctx.stakeholder
        cid = svc._ensure_conversation(self.tid, "", "q")
        updated = svc.update_conversation(self.tid, cid, title="Renamed", starred=True)
        self.assertEqual(updated["title"], "Renamed")
        self.assertTrue(updated["starred"])

    def test_delete_conversation(self):
        svc = self.ctx.stakeholder
        cid = svc._ensure_conversation(self.tid, "", "q")
        self.assertTrue(svc.delete_conversation(self.tid, cid))
        self.assertIsNone(svc.get_conversation(self.tid, cid))
        self.assertFalse(svc.delete_conversation(self.tid, cid))

    def _seed_reusable_cube(self, conversation_id, label="df_1"):
        """Approve a base view and cache a cube over it, so a follow-up turn can
        legitimately be served from the workspace.

        Before Task 11 any cached frame was a reuse candidate. Now a candidate
        must carry the same population_hash as the turn's plan, so a test that
        wants reuse has to establish a population first.
        """
        import pandas as pd
        from analytics_platform.domain import BaseView
        from analytics_platform.execution.extract_store import ExtractMeta

        registry = self.ctx.stakeholder.base_views
        view = BaseView(name="order_events", grain=["order_id"],
                        source_sql="SELECT order_id, revenue FROM events",
                        dimension_columns=[], measure_columns=["revenue"],
                        row_count_estimate=1000)
        node = registry.upsert(self.tid, view, by="senior")
        brain = self.ctx.pipeline.brain(self.tid)
        brain.submit(node.id, by="junior")
        brain.approve(node.id, by="senior")
        self.ctx.stakeholder.data_cache.put(
            self.tid, conversation_id, label, "orders",
            pd.DataFrame({"amount": [1, 2, 3], "revenue": [1, 2, 3]}),
            meta=ExtractMeta(label=label, grain=["order_id"],
                             columns=["amount", "revenue"], dimensions=[],
                             base_view="order_events",
                             population_hash=registry.population_hash(view),
                             row_count=3, created_at="2026-08-15T00:00:00Z"))
        return view

    def test_python_synthesis_repairs_after_policy_rejection(self):
        import pandas as pd
        self.ctx.stakeholder.data_cache.put(
            self.tid, "conv-1", "df_1", "orders", pd.DataFrame({"amount": [1, 2, 3]}))
        mock_llm = MagicMock()
        mock_llm.generate.side_effect = [
            MagicMock(text="```python\nimport os\nresult = 1\n```", tokens_in=10, tokens_out=5),
            MagicMock(text="```python\nresult = int(df_1['amount'].sum())\n```", tokens_in=10, tokens_out=5),
        ]

        code, exec_res, toks = self.ctx.stakeholder._synthesize_and_execute_python(
            mock_llm, self.tid, "conv-1", "what's the total amount", "df_1")

        self.assertIsNotNone(exec_res)
        self.assertTrue(exec_res.ok)
        self.assertEqual(exec_res.result_summary, 6)
        second_call_prompt = mock_llm.generate.call_args_list[1].kwargs["prompt"]
        self.assertIn("os", second_call_prompt)

    def test_python_synthesis_repairs_after_execution_failure(self):
        import pandas as pd
        self.ctx.stakeholder.data_cache.put(
            self.tid, "conv-1", "df_1", "orders", pd.DataFrame({"amount": [1, 2, 3]}))
        mock_llm = MagicMock()
        mock_llm.generate.side_effect = [
            MagicMock(text="```python\nresult = 1 / 0\n```", tokens_in=10, tokens_out=5),
            MagicMock(text="```python\nresult = int(df_1['amount'].sum())\n```", tokens_in=10, tokens_out=5),
        ]

        code, exec_res, toks = self.ctx.stakeholder._synthesize_and_execute_python(
            mock_llm, self.tid, "conv-1", "what's the total amount", "df_1")

        self.assertIsNotNone(exec_res)
        self.assertTrue(exec_res.ok)
        second_call_prompt = mock_llm.generate.call_args_list[1].kwargs["prompt"]
        self.assertIn("ZeroDivisionError", second_call_prompt)

    def test_python_synthesis_stops_after_max_attempts_and_returns_none(self):
        import pandas as pd
        self.ctx.stakeholder.data_cache.put(
            self.tid, "conv-1", "df_1", "orders", pd.DataFrame({"amount": [1, 2, 3]}))
        mock_llm = MagicMock()
        mock_llm.generate.return_value = MagicMock(
            text="```python\nresult = 1 / 0\n```", tokens_in=10, tokens_out=5)

        code, exec_res, toks = self.ctx.stakeholder._synthesize_and_execute_python(
            mock_llm, self.tid, "conv-1", "what's the total amount", "df_1", max_attempts=3)

        self.assertIsNone(exec_res)
        self.assertEqual(code, "")
        self.assertEqual(mock_llm.generate.call_count, 3)

    def test_synthesize_and_execute_python_returns_none_for_unknown_label(self):
        mock_llm = MagicMock()
        code, exec_res, toks = self.ctx.stakeholder._synthesize_and_execute_python(
            mock_llm, self.tid, "conv-1", "what's the total", "df_does_not_exist")
        self.assertIsNone(exec_res)
        mock_llm.generate.assert_not_called()

    @patch("analytics_platform.stakeholder.make_role_client")
    def test_answer_routes_to_python_when_cache_hit_and_records_python_cells(self, mock_make_role_client):
        # conversation_id must already exist in stakeholder_conversations for
        # _ensure_conversation to reuse it (an unknown id silently starts a
        # fresh conversation instead) -- create a real one first, same as
        # test_answer_creates_and_reuses_conversation does, then seed the
        # DataFrame cache under that real id.
        cid = self.ctx.stakeholder._ensure_conversation(self.tid, "", "seed conversation")
        self._seed_reusable_cube(cid)
        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [
            MagicMock(text='{"category": "metric_lookup"}', tokens_in=5, tokens_out=5),
            MagicMock(text=REUSE_PLAN, tokens_in=5, tokens_out=5),
            MagicMock(text="```python\nresult = int(df_1['amount'].sum())\n```", tokens_in=10, tokens_out=5),
            MagicMock(text='{"answer": "the total is 6"}', tokens_in=10, tokens_out=5),
        ]
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(
            self.tid, {"stakeholder": {"enabled": True, "provider": "mock", "model": "mock"}})
        res = self.ctx.stakeholder.answer(
            self.tid, "what's the total amount", conversation_id=cid)

        self.assertEqual(res["queries_run"], [])
        self.assertEqual(len(res["python_cells"]), 1)
        self.assertEqual(res["python_cells"][0]["df_label"], "df_1")
        self.assertEqual(res["python_cells"][0]["result_summary"], 6)
        self.assertEqual(res["conversation_id"], cid)

    @patch("analytics_platform.stakeholder.make_role_client")
    def test_get_conversation_includes_python_cells_after_reload(self, mock_make_role_client):
        cid = self.ctx.stakeholder._ensure_conversation(self.tid, "", "seed conversation")
        self._seed_reusable_cube(cid)
        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [
            MagicMock(text='{"category": "metric_lookup"}', tokens_in=5, tokens_out=5),
            MagicMock(text=REUSE_PLAN, tokens_in=5, tokens_out=5),
            MagicMock(text="```python\nresult = int(df_1['amount'].sum())\n```", tokens_in=10, tokens_out=5),
            MagicMock(text='{"answer": "the total is 6"}', tokens_in=10, tokens_out=5),
        ]
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(
            self.tid, {"stakeholder": {"enabled": True, "provider": "mock", "model": "mock"}})
        self.ctx.stakeholder.answer(self.tid, "what's the total amount", conversation_id=cid)

        conv = self.ctx.stakeholder.get_conversation(self.tid, cid)
        self.assertEqual(len(conv["messages"][0]["python_cells"]), 1)

    def test_multi_turn_conversation_second_turn_uses_cached_dataframe_not_new_sql(self):
        """End-to-end: turn 1 goes through real SQL synthesis (Task 4) and
        caches its DataFrame; turn 2 is routed by _choose_compute_path (Task 5)
        to run Python (Task 6) over that cached DataFrame instead of hitting
        the warehouse again. Drives both turns through the public answer()
        entry point -- no internal method is called directly to fake a step.

        The fixture warehouse's only table is `events` (see
        build_retail_warehouse()/WEEKLY_ORDER_SQL in analytics_platform/fixtures)
        and it has no `amount` column, so the synthesized SQL and Python below
        use `revenue`, the numeric column that actually exists there.
        """
        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [
            # Turn 1: search-intent extraction -> SQL synthesis -> answer synthesis
            # (same shape as test_successful_sql_synthesis_caches_the_resulting_dataframe).
            MagicMock(text="retail orders", ok=True, tokens_in=10, tokens_out=5),
            plan_resp(),
            MagicMock(text="```sql\nSELECT revenue FROM events WHERE action = 'order' LIMIT 10\n```",
                      tokens_in=20, tokens_out=10),
            MagicMock(text='{"answer": "here is the order data"}', tokens_in=15, tokens_out=8),
            # Turn 2: search-intent extraction -> planning(reuse) -> python synthesis -> answer synthesis.
            MagicMock(text='{"category": "metric_lookup"}', tokens_in=10, tokens_out=5),
            MagicMock(text=REUSE_PLAN, tokens_in=5, tokens_out=5),
            MagicMock(text="```python\nresult = int(df_1['revenue'].sum())\n```", tokens_in=10, tokens_out=5),
            MagicMock(text='{"answer": "the total is computed from what we already fetched"}',
                     tokens_in=10, tokens_out=5),
        ]
        with patch("analytics_platform.stakeholder.make_role_client", return_value=mock_llm):
            self.ctx.tenants.set_analyst_config(
                self.tid, {"stakeholder": {"enabled": True, "provider": "mock", "model": "mock"}})

            turn1 = self.ctx.stakeholder.answer(self.tid, "show me order amounts", conversation_id="")
            conv_id = turn1["conversation_id"]
            self.assertEqual(len(turn1["queries_run"]), 1)
            self._seed_reusable_cube(conv_id)   # see the note on that helper

            # Ground truth computed from the cache itself, not hard-coded --
            # this test must pass regardless of what the fixture warehouse's
            # actual order revenue values are.
            cached_df = self.ctx.stakeholder.data_cache.get(self.tid, conv_id, "df_1")
            expected_sum = int(cached_df["revenue"].sum())

            turn2 = self.ctx.stakeholder.answer(
                self.tid, "what's the total of that", conversation_id=conv_id)

        # Turn 2 answered via Python over the cache, not a fresh SQL execution.
        self.assertEqual(turn2["queries_run"], [])
        self.assertEqual(len(turn2["python_cells"]), 1)
        self.assertEqual(turn2["python_cells"][0]["result_summary"], expected_sum)
        self.assertEqual(turn2["conversation_id"], conv_id)

        conv = self.ctx.stakeholder.get_conversation(self.tid, conv_id)
        self.assertEqual(len(conv["messages"]), 2)
        self.assertEqual(conv["messages"][1]["python_cells"][0]["df_label"], "df_1")

    @patch("analytics_platform.stakeholder.make_role_client")
    def test_sql_turn_records_the_df_label_it_populated_in_the_cache(self, mock_make_role_client):
        """A turn that synthesizes+caches a DataFrame (same shape as
        test_successful_sql_synthesis_caches_the_resulting_dataframe) should
        also record that cache label on the answer itself -- Task 2's
        dependency tracking needs this join key."""
        intent_resp = MagicMock(text="retail orders", ok=True, tokens_in=0, tokens_out=0)
        synthesized_sql = (
            "```sql\n"
            "SELECT COUNT(*) AS orders FROM events WHERE action = 'order'\n"
            "```"
        )
        sql_resp = MagicMock(text=synthesized_sql, tokens_in=150, tokens_out=40)
        answer_resp = MagicMock(
            text='{"answer": "There were N orders."}', tokens_in=90, tokens_out=30)

        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [intent_resp, plan_resp(), sql_resp, answer_resp]
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "stakeholder": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-haiku"}
        })

        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month",
                                          conversation_id="")

        self.assertEqual(res["produced_df_label"], "df_1")
        conv = self.ctx.stakeholder.get_conversation(self.tid, res["conversation_id"])
        self.assertEqual(conv["messages"][0]["produced_df_label"], "df_1")

    @patch("analytics_platform.stakeholder.make_role_client")
    def test_python_turn_records_no_produced_df_label(self, mock_make_role_client):
        """A turn answered via Python-over-cache (same shape as
        test_answer_routes_to_python_when_cache_hit_and_records_python_cells)
        doesn't itself populate the DataFrame cache, so produced_df_label
        should stay empty."""
        cid = self.ctx.stakeholder._ensure_conversation(self.tid, "", "seed conversation")
        self._seed_reusable_cube(cid)
        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [
            MagicMock(text='{"category": "metric_lookup"}', tokens_in=5, tokens_out=5),
            MagicMock(text=REUSE_PLAN, tokens_in=5, tokens_out=5),
            MagicMock(text="```python\nresult = int(df_1['amount'].sum())\n```", tokens_in=10, tokens_out=5),
            MagicMock(text='{"answer": "the total is 6"}', tokens_in=10, tokens_out=5),
        ]
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(
            self.tid, {"stakeholder": {"enabled": True, "provider": "mock", "model": "mock"}})
        res = self.ctx.stakeholder.answer(
            self.tid, "what's the total amount", conversation_id=cid)

        self.assertEqual(res["produced_df_label"], "")

    def test_export_of_python_only_turn_pulls_in_its_sql_dependency(self):
        """End-to-end proof of the whole storyline chain: turn 1 synthesizes SQL
        and caches a DataFrame (recording produced_df_label per Task 1); turn 2
        answers via Python over that cached DataFrame. Exporting ONLY turn 2
        must still pull turn 1's SQL into the Code Appendix as a dependency
        (Task 2's assemble_storyline).

        Mock sequencing mirrors test_sql_turn_records_the_df_label_it_populated_in_the_cache
        for turn 1 (intent extraction -> SQL synthesis -> answer synthesis) and
        test_python_turn_records_no_produced_df_label for turn 2 (intent
        extraction -> path routing -> python synthesis -> answer synthesis).
        classify() is a pure keyword heuristic with no LLM call, so the first
        generate() in each turn is _extract_search_intent, not intent
        classification -- its return value is consumed as plain-text search
        query, not JSON. The fixture warehouse (build_retail_warehouse) only
        has an `events` table with a `revenue` column, not `orders`/`amount`.
        setUp already registers the "Events" datasource, so it is reused here
        rather than calling stakeholder.add_datasource (which does not exist).
        """
        import pandas as pd
        from analytics_platform.storyline import assemble_storyline, render_markdown

        mock_llm = MagicMock()
        mock_llm.name = "mock_gateway"
        mock_llm.generate.side_effect = [
            MagicMock(text="retail orders", ok=True, tokens_in=10, tokens_out=5),
            plan_resp(),
            MagicMock(text="```sql\nSELECT revenue FROM events WHERE action = 'order' LIMIT 10\n```",
                      tokens_in=20, tokens_out=10),
            MagicMock(text='{"answer": "here is the revenue"}', tokens_in=15, tokens_out=8),
        ]
        with patch("analytics_platform.stakeholder.make_role_client", return_value=mock_llm):
            self.ctx.tenants.set_analyst_config(
                self.tid, {"stakeholder": {"enabled": True, "provider": "mock", "model": "mock"}})
            turn1 = self.ctx.stakeholder.answer(self.tid, "what's the revenue", conversation_id="")

        conv_id = turn1["conversation_id"]
        self.assertEqual(turn1["produced_df_label"], "df_1")
        self._seed_reusable_cube(conv_id)      # see the note on that helper

        mock_llm.generate.side_effect = [
            MagicMock(text='{"category": "metric_lookup"}', tokens_in=5, tokens_out=5),
            MagicMock(text=REUSE_PLAN, tokens_in=5, tokens_out=5),
            MagicMock(text="```python\nresult = int(df_1['revenue'].sum())\n```",
                      tokens_in=10, tokens_out=5),
            MagicMock(text='{"answer": "the total is 6"}', tokens_in=10, tokens_out=5),
        ]
        with patch("analytics_platform.stakeholder.make_role_client", return_value=mock_llm):
            turn2 = self.ctx.stakeholder.answer(
                self.tid, "what's the total", conversation_id=conv_id)

        conv = self.ctx.stakeholder.get_conversation(self.tid, conv_id)
        content = assemble_storyline(conv, [turn2["answer_id"]])  # only the Python turn

        self.assertEqual([t.answer_id for t in content.turns], [turn2["answer_id"]])
        sql_entries = [e for e in content.code_appendix if e.kind == "sql"]
        self.assertEqual(len(sql_entries), 1)
        self.assertTrue(sql_entries[0].is_dependency)
        self.assertEqual(sql_entries[0].source_answer_id, turn1["answer_id"])
        self.assertEqual(content.unresolved_dependency_count, 0)

        # ...and that dependency actually reaches the rendered document. Without
        # this the chain stops at metadata and an assembly/rendering mismatch is
        # invisible.
        md = render_markdown(content)
        self.assertIn("SELECT revenue FROM events WHERE action = 'order' LIMIT 10", md)
        self.assertIn("included as a dependency of df_1", md)


class TestConversationSchema(unittest.TestCase):
    def test_conversation_table_and_column_exist(self):
        ctx, base = app_ctx(warehouse=build_retail_warehouse())
        tid = ctx.tenants.create_tenant("SchemaCo", retention_days=90).id
        store = ctx.stores.for_tenant(tid)
        # table exists and is queryable
        rows = store.query_all("SELECT * FROM stakeholder_conversations WHERE tenant_id=?", (tid,))
        self.assertEqual(rows, [])
        # new column exists on the pre-existing table
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(stakeholder_answers)").fetchall()}
        self.assertIn("conversation_id", cols)
        base.close()


if __name__ == "__main__":
    unittest.main()

# --------------------------------------------------------------------------- #
# Task 11 -- _plan_turn: choose the population, state the cube
# --------------------------------------------------------------------------- #
class MockLLM:
    """A live-looking client returning canned text, counting calls."""

    name = "gateway"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.prompts = []
        self.system_prompts = []

    def generate(self, prompt="", system_prompt="", **kw):
        from analytics_platform.llm.client import LLMResponse
        self.calls += 1
        self.prompts.append(prompt)
        self.system_prompts.append(system_prompt)
        if not self.responses:
            raise AssertionError("the LLM was called more times than the test allows")
        return LLMResponse(text=self.responses.pop(0), tokens_in=5, tokens_out=5)

    @property
    def last_prompt(self):
        return self.prompts[-1] if self.prompts else ""

    @property
    def last_system_prompt(self):
        return self.system_prompts[-1] if self.system_prompts else ""

    @property
    def exhausted(self):
        return not self.responses


REUSE_PLAN = ('{"base_view":"order_events","cube":{"dimensions":[],'
              '"measures":[{"name":"revenue","expr":"SUM(revenue)"}],"filters":{}},'
              '"analysis":"python"}')

CUBE = ('{"base_view":"checkout_sessions","cube":{"dimensions":["device"],'
        '"measures":[{"name":"revenue","expr":"SUM(revenue)"}],"filters":{}},'
        '"analysis":"workspace_sql"}')

TOO_WIDE = ('{"base_view":"checkout_sessions","cube":{"dimensions":["country","device","city"],'
            '"measures":[{"name":"n","expr":"COUNT(*)"}]}}')


class TestPlanTurn(unittest.TestCase):
    def setUp(self):
        from analytics_platform.domain import BaseView, ColumnProfile
        from analytics_platform.schema_context import SchemaContext

        self.ctx, self.base = app_ctx(warehouse=build_retail_warehouse())
        self.tid = self.ctx.tenants.create_tenant("PlanCo").id
        self.app = create_app(self.ctx)       # backfills ctx.stakeholder
        self.svc = self.ctx.stakeholder
        self.registry = self.svc.base_views

        self.view = BaseView(
            name="checkout_sessions", grain=["session_id"],
            source_sql="SELECT session_id, country, device, city, service_line, revenue "
                       "FROM orders WHERE is_test_traffic = false",
            dimension_columns=["country", "device", "city", "service_line"],
            measure_columns=["revenue"], row_count_estimate=1_200_000,
            description="test traffic excluded")
        self.profiles = {
            c: ColumnProfile(column=c, dtype="object", distinct_count=n,
                             null_fraction=0.0, values=[], values_complete=False)
            for c, n in (("country", 30), ("device", 4), ("city", 4_000),
                         ("service_line", 3))}
        self.schema_ctx = SchemaContext(profiles=self.profiles, rendered="RENDERED")

    def tearDown(self):
        self.svc.workspace.close_all()
        self.base.close()

    def approve_view(self, view=None):
        node = self.registry.upsert(self.tid, view or self.view, by="senior")
        brain = self.ctx.pipeline.brain(self.tid)
        brain.submit(node.id, by="junior")
        brain.approve(node.id, by="senior")
        return node

    def plan(self, llm, question="q", conversation_id="c1", schema_ctx=None):
        return self.svc._plan_turn(llm, self.tid, conversation_id, question, [], [],
                                   schema_ctx=schema_ctx or self.schema_ctx)

    # -- the division of labour ------------------------------------------------
    def test_the_verdict_not_the_llm_sets_the_path(self):
        """The LLM names the population and the cut; the DataManager decides
        whether the workspace already covers it."""
        import pandas as pd
        from analytics_platform.execution.extract_store import ExtractMeta
        self.approve_view()
        pop = self.registry.population_hash(self.view)
        self.svc.data_cache.put(
            self.tid, "c1", "df_1", "sessions by device and country",
            pd.DataFrame({"device": ["ios"], "country": ["DE"], "revenue": [1]}),
            meta=ExtractMeta(label="df_1", grain=["session_id"],
                             columns=["device", "country", "revenue"],
                             dimensions=["device", "country"], population_hash=pop,
                             base_view="checkout_sessions", row_count=1,
                             created_at="2026-08-15T00:00:00Z"))
        plan = self.plan(MockLLM([CUBE]), "break that down by device")
        self.assertEqual(plan.path, "reuse")
        self.assertEqual(plan.df_label, "df_1")
        self.assertEqual(plan.analysis, "workspace_sql")

    def test_the_population_hash_comes_from_the_base_not_the_llm(self):
        """An LLM-supplied hash would let a bad plan claim a reconcilability it
        does not have."""
        self.approve_view()
        plan = self.plan(MockLLM([CUBE]))
        self.assertEqual(plan.requirement.population_hash,
                         self.registry.population_hash(self.view))

    def test_a_cube_the_workspace_cannot_cover_becomes_retrieve(self):
        self.approve_view()
        llm = MockLLM([CUBE])
        plan = self.plan(llm, "how did revenue trend?")
        self.assertEqual(plan.path, "retrieve")
        self.assertEqual(plan.grain, ["session_id"])
        self.assertEqual(llm.calls, 1)   # regression guard: the old router returned early

    def test_the_planner_is_called_even_with_an_empty_workspace(self):
        """The key difference from the old router, which short-circuited to 'sql'
        whenever nothing was cached -- which is why two SQLs and zero Python
        appeared in the reported run."""
        self.approve_view()
        llm = MockLLM([CUBE])
        self.plan(llm)
        self.assertEqual(llm.calls, 1)

    def test_an_approved_base_is_not_marked_provisional(self):
        self.approve_view()
        self.assertTrue(self.plan(MockLLM([CUBE])).base_view_approved)

    # -- failure handling ------------------------------------------------------
    def test_an_unknown_base_view_name_is_a_parse_failure(self):
        self.approve_view()
        plan = self.plan(MockLLM(['{"base_view":"nope","cube":{"dimensions":[]}}']))
        self.assertEqual(plan.path, "aggregate")
        self.assertIsNone(plan.base_view)

    def test_plan_turn_falls_back_to_aggregate_on_garbage(self):
        """Deliberately NOT retrieve: with no resolved base there is no population
        to retrieve over, and inventing one produces an unreconcilable number
        silently. The aggregate path at least declares itself."""
        p = self.plan(MockLLM(["not json at all"]))
        self.assertEqual(p.path, "aggregate")
        self.assertIsNone(p.base_view)

    def test_plan_turn_falls_back_to_aggregate_when_the_llm_errors(self):
        class Boom:
            name = "gateway"

            def generate(self, *a, **k):
                raise RuntimeError("gateway down")

        self.assertEqual(self.plan(Boom()).path, "aggregate")

    def test_aggregate_only_overrides_the_verdict(self):
        self.approve_view()
        llm = MockLLM(['{"base_view":"checkout_sessions","cube":{"dimensions":[],'
                       '"measures":[{"name":"revenue","expr":"SUM(revenue)"}]},'
                       '"aggregate_only":true,"analysis":"python"}'])
        self.assertEqual(self.plan(llm, "total revenue?").path, "aggregate")

    # -- the cell guard --------------------------------------------------------
    def test_a_cube_over_the_cell_limit_is_re_prompted_with_the_culprit(self):
        """The guard refuses; the LLM is told which dimension is the problem, not
        just 'no'. Silently dropping one on the model's behalf would answer a
        question nobody asked."""
        self.approve_view()
        llm = MockLLM([TOO_WIDE,
                       '{"base_view":"checkout_sessions","cube":{"dimensions":["country","device"],'
                       '"measures":[{"name":"n","expr":"COUNT(*)"}]}}'])
        plan = self.plan(llm)
        self.assertEqual(llm.calls, 2)
        self.assertIn("city", llm.prompts[1])
        self.assertEqual(plan.cube.dimensions, ["country", "device"])
        self.assertTrue(plan.cube_sql.ok)

    def test_a_cube_that_cannot_be_shrunk_falls_back_to_aggregate(self):
        self.approve_view()
        self.assertEqual(self.plan(MockLLM([TOO_WIDE, TOO_WIDE])).path, "aggregate")

    # -- proposing a base view -------------------------------------------------
    def test_a_proposed_base_view_is_stored_as_draft_and_marked_provisional(self):
        llm = MockLLM(['{"base_view":"guest_checkouts","propose_base_view":'
                       '{"name":"guest_checkouts","grain":["guest_id"],'
                       '"source_sql":"SELECT guest_id, country FROM guests",'
                       '"dimension_columns":["country"],'
                       '"measure_columns":["revenue"]},'
                       '"cube":{"dimensions":["country"],"measures":[]}}'])
        plan = self.plan(llm, "guest revenue by country")
        self.assertEqual(plan.base_view.name, "guest_checkouts")
        self.assertIs(plan.base_view_approved, False)
        self.assertIsNotNone(
            self.registry.get(self.tid, "guest_checkouts", approved_only=False))
        self.assertIsNone(self.registry.get(self.tid, "guest_checkouts"))   # not approved

    def test_a_proposal_carries_a_provisional_caveat(self):
        llm = MockLLM(['{"base_view":"guest_checkouts","propose_base_view":'
                       '{"name":"guest_checkouts","grain":["guest_id"],'
                       '"source_sql":"SELECT guest_id, country FROM guests",'
                       '"dimension_columns":["country"],"measure_columns":[]},'
                       '"cube":{"dimensions":["country"],"measures":[]}}'])
        plan = self.plan(llm, "guest revenue by country")
        self.assertTrue(any("provisional" in c for c in plan.caveats), plan.caveats)

    def test_an_approved_view_is_preferred_over_a_draft_of_the_same_name(self):
        self.approve_view()
        self.registry.upsert(self.tid, self.view, by="planner")   # touches the same node
        self.assertTrue(self.plan(MockLLM([CUBE])).base_view_approved)

    # -- measures --------------------------------------------------------------
    def test_avg_is_accepted_and_arrives_additive(self):
        self.approve_view()
        llm = MockLLM(['{"base_view":"checkout_sessions","cube":{"dimensions":["country"],'
                       '"measures":[{"name":"revenue","expr":"AVG(revenue)"}]}}'])
        plan = self.plan(llm, "average revenue by country")
        self.assertEqual(plan.cube_sql.non_additive, [])
        self.assertIn("revenue_sum", plan.cube_sql.sql)
        self.assertNotIn("AVG(", plan.cube_sql.sql)

    def test_a_distinct_count_is_flagged_non_additive_on_the_plan(self):
        self.approve_view()
        llm = MockLLM(['{"base_view":"checkout_sessions","cube":{"dimensions":["country"],'
                       '"measures":[{"name":"users","expr":"COUNT(DISTINCT user_id)"}]}}'])
        self.assertEqual(self.plan(llm).cube_sql.non_additive, ["users"])

    # -- attribution is inherited, never restated ------------------------------
    def test_attribution_on_an_existing_base_is_inherited_not_restated(self):
        """Two questions applying two rankings to the same sessions is exactly the
        failure the base exists to prevent, so an attributions array the planner
        emits over an existing base is ignored."""
        from analytics_platform.domain import AttributionRule, BaseView
        import dataclasses
        attributed = dataclasses.replace(self.view, attributions=[AttributionRule(
            column="service_line", grain=["session_id"], strategy="highest_intent",
            priority_values=["mobile", "fixed", "ott"], source="brain")])
        self.approve_view(attributed)
        llm = MockLLM(['{"base_view":"checkout_sessions","cube":{"dimensions":["service_line"],'
                       '"measures":[]},"attributions":[{"column":"service_line",'
                       '"grain":["session_id"],"strategy":"latest"}]}'])
        plan = self.plan(llm)
        self.assertEqual(plan.attributions, [])                             # ignored
        self.assertEqual(plan.base_view.attributions[0].strategy, "highest_intent")

    def test_a_proposed_base_view_may_carry_attribution_rules(self):
        llm = MockLLM(['''{"base_view":"events_by_session","propose_base_view":{
            "name":"events_by_session","grain":["session_id"],
            "source_sql":"SELECT session_id, service_line FROM events",
            "dimension_columns":["service_line"],"measure_columns":[]},
            "cube":{"dimensions":["service_line"],"measures":[]},
            "attributions":[{"column":"service_line","grain":["session_id"],
                             "strategy":"highest_intent",
                             "priority_values":["mobile","fixed","ott"],
                             "tiebreakers":["event_count DESC","log_time DESC"],
                             "source":"brain"}]}'''])
        plan = self.plan(llm)
        self.assertEqual(plan.base_view.attributions[0].priority_values,
                         ["mobile", "fixed", "ott"])

    def test_a_proposed_base_with_a_fanned_out_column_and_no_rule_gets_a_default(self):
        """service_line fans out and the proposal ignored it. Carrying it onto the
        grain silently would double-count, so a most_frequent rule is synthesized
        and marked as a default rather than a business decision."""
        from analytics_platform.domain import ColumnProfile
        from analytics_platform.schema_context import SchemaContext
        profiles = dict(self.profiles)
        profiles["service_line"] = ColumnProfile(
            column="service_line", dtype="object", distinct_count=3, null_fraction=0.0,
            values=["mobile", "fixed", "ott"], values_complete=True,
            fanout_by_key={"session_id": 0.06})
        ctx = SchemaContext(profiles=profiles, rendered="RENDERED")
        llm = MockLLM(['{"base_view":"events_by_session","propose_base_view":{'
                       '"name":"events_by_session","grain":["session_id"],'
                       '"source_sql":"SELECT session_id, service_line FROM events",'
                       '"dimension_columns":["service_line"],"measure_columns":[]},'
                       '"cube":{"dimensions":["service_line"],"measures":[]}}'])
        plan = self.plan(llm, schema_ctx=ctx)
        rule = next(a for a in plan.base_view.attributions if a.column == "service_line")
        self.assertEqual(rule.strategy, "most_frequent")
        self.assertEqual(rule.source, "default")

    # -- the prompt ------------------------------------------------------------
    def test_plan_turn_prompt_carries_the_rendered_context(self):
        self.approve_view()
        llm = MockLLM([CUBE])
        self.plan(llm)
        self.assertIn("RENDERED", llm.last_system_prompt + llm.last_prompt)

    def test_the_verdict_reason_survives_onto_the_plan(self):
        import pandas as pd
        from analytics_platform.execution.extract_store import ExtractMeta
        self.approve_view()
        pop = self.registry.population_hash(self.view)
        self.svc.data_cache.put(
            self.tid, "c1", "df_1", "by device",
            pd.DataFrame({"device": ["ios"], "revenue": [1]}),
            meta=ExtractMeta(label="df_1", grain=["session_id"],
                             columns=["device", "revenue"], dimensions=["device"],
                             population_hash=pop, base_view="checkout_sessions",
                             row_count=1, created_at="2026-08-15T00:00:00Z"))
        self.assertIn("df_1", self.plan(MockLLM([CUBE])).verdict.reason)
