"""P6 — Stakeholder analyst tests (reuse approved, refresh, escalate, feedback)."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from analytics_platform.api import FeedbackIn, StakeholderIn, ConversationPatchIn, create_app
from analytics_platform.domain import AnswerMode, DataSourceKind, NodeKind
from analytics_platform.fixtures import WEEKLY_ORDER_SQL, build_retail_warehouse
from tests.test_api import app_ctx, call


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
        mock_llm.generate.side_effect = [intent_resp, no_sql_resp, skill_null_resp, freeform_resp]
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
        mock_llm.generate.side_effect = [intent_resp, no_sql_resp, synth_resp]
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
        mock_llm.generate.side_effect = [intent_resp, no_sql_resp, chart_resp]
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
        mock_llm.generate.side_effect = [intent_resp, sql_resp, answer_resp]
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
        mock_llm.generate.side_effect = [intent_resp, sql_resp, answer_resp]
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
        mock_llm.generate.side_effect = [intent_resp, bad_sql_resp, fixed_sql_resp, answer_resp]
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
        second_call_kwargs = mock_llm.generate.call_args_list[2].kwargs
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
        mock_llm.generate.side_effect = [intent_resp, bad_table_resp, fixed_sql_resp, answer_resp]
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
            intent_resp, always_bad_resp, always_bad_resp, always_bad_resp, answer_resp]
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "stakeholder": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-haiku"}
        })

        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        self.assertEqual(res["answer_mode"], AnswerMode.REFRESHED_APPROVED_QUERY.value)
        self.assertEqual(mock_llm.generate.call_count, 5)

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
        mock_llm.generate.side_effect = [intent_resp, no_sql_resp, answer_resp]
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
            intent_resp, unsafe_sql_resp, unsafe_sql_resp, unsafe_sql_resp, answer_resp]
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "stakeholder": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-haiku"}
        })

        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        self.assertEqual(res["answer_mode"], AnswerMode.REFRESHED_APPROVED_QUERY.value)
        self.assertNotIn("DELETE", " ".join(res["queries_run"]))
        self.assertEqual(mock_llm.generate.call_count, 5)

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
            intent_resp, bad_column_resp, bad_column_resp, bad_column_resp, answer_resp]
        mock_make_role_client.return_value = mock_llm

        self.ctx.tenants.set_analyst_config(self.tid, {
            "stakeholder": {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-3-haiku"}
        })

        res = self.ctx.stakeholder.answer(self.tid, "how many retail orders per month")
        self.assertEqual(res["answer_mode"], AnswerMode.REFRESHED_APPROVED_QUERY.value)
        self.assertEqual(mock_llm.generate.call_count, 5)

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

    def test_choose_compute_path_defaults_to_sql_when_nothing_cached(self):
        mock_llm = MagicMock()
        path, label = self.ctx.stakeholder._choose_compute_path(
            mock_llm, self.tid, "no-such-conversation", "how many orders")
        self.assertEqual(path, "sql")
        self.assertEqual(label, "")
        mock_llm.generate.assert_not_called()  # no point asking if nothing's cached

    def test_choose_compute_path_returns_python_when_llm_says_so_and_label_exists(self):
        import pandas as pd
        self.ctx.stakeholder.data_cache.put(
            self.tid, "conv-1", "df_1", "orders by month", pd.DataFrame({"a": [1, 2]}))
        mock_llm = MagicMock()
        mock_llm.generate.return_value = MagicMock(
            text='{"path": "python", "df_label": "df_1"}', tokens_in=5, tokens_out=5)

        path, label = self.ctx.stakeholder._choose_compute_path(
            mock_llm, self.tid, "conv-1", "what's the total")

        self.assertEqual(path, "python")
        self.assertEqual(label, "df_1")

    def test_choose_compute_path_falls_back_to_sql_on_unknown_label(self):
        import pandas as pd
        self.ctx.stakeholder.data_cache.put(
            self.tid, "conv-1", "df_1", "orders by month", pd.DataFrame({"a": [1, 2]}))
        mock_llm = MagicMock()
        mock_llm.generate.return_value = MagicMock(
            text='{"path": "python", "df_label": "df_does_not_exist"}', tokens_in=5, tokens_out=5)

        path, label = self.ctx.stakeholder._choose_compute_path(
            mock_llm, self.tid, "conv-1", "what's the total")

        self.assertEqual(path, "sql")
        self.assertEqual(label, "")

    def test_choose_compute_path_falls_back_to_sql_on_malformed_llm_response(self):
        import pandas as pd
        self.ctx.stakeholder.data_cache.put(
            self.tid, "conv-1", "df_1", "orders by month", pd.DataFrame({"a": [1, 2]}))
        mock_llm = MagicMock()
        mock_llm.generate.return_value = MagicMock(text="not json at all", tokens_in=0, tokens_out=0)

        path, label = self.ctx.stakeholder._choose_compute_path(
            mock_llm, self.tid, "conv-1", "what's the total")

        self.assertEqual(path, "sql")
        self.assertEqual(label, "")

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