"""Tests for SkillEngine.extract_params's required-parameter contract."""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

from analytics_platform.skills.engine import SkillEngine, _required_params
from analytics_platform.skills.registry import SkillBundle, SkillMetaData


def make_skill(sql: str) -> SkillBundle:
    meta = SkillMetaData(name="fake-skill", description="a fake skill for testing", skill_dir="fake-skill")
    return SkillBundle(meta=meta, instructions="do the thing", sql_templates={"01_query.sql": sql})


class TestRequiredParams(unittest.TestCase):
    def test_collects_dollar_and_braced_tokens_in_first_seen_order(self):
        sql = "SELECT * FROM t WHERE natco = '$natco_code' AND days <= ${lookback_days} AND x = '$natco_code'"
        skill = make_skill(sql)
        self.assertEqual(_required_params(skill), ["natco_code", "lookback_days"])

    def test_no_tokens_yields_empty_list(self):
        skill = make_skill("SELECT 1")
        self.assertEqual(_required_params(skill), [])


class TestExtractParams(unittest.TestCase):
    def setUp(self):
        self.engine = SkillEngine()
        self.skill = make_skill(
            "SELECT * FROM t WHERE natco_code = '$natco_code' "
            "AND event_date >= date_add('day', -$lookback_days, CURRENT_DATE)")

    def test_missing_required_param_degrades_to_clarification_instead_of_silent_gap(self):
        """If the LLM fills some but not all of the templates' required $keys,
        and doesn't flag it itself, extract_params must still surface a
        clarification request -- letting execute() run would leave a literal
        unsubstituted $token in the SQL and fail with an opaque warehouse
        syntax error instead of a clear ask."""
        llm = MagicMock()
        llm.generate.return_value = MagicMock(text=json.dumps({
            "params": {"natco_code": "de"},  # lookback_days missing
            "needs_clarification": False,
            "clarification_question": "",
        }))

        params, needs_clarif, clarif_q = self.engine.extract_params("how many orders in DE", self.skill, llm)
        self.assertTrue(needs_clarif)
        self.assertIn("lookback_days", clarif_q)
        self.assertEqual(params, {"natco_code": "de"})

    def test_all_required_params_present_passes_through_cleanly(self):
        llm = MagicMock()
        llm.generate.return_value = MagicMock(text=json.dumps({
            "params": {"natco_code": "de", "lookback_days": 30},
            "needs_clarification": False,
            "clarification_question": "",
        }))

        params, needs_clarif, clarif_q = self.engine.extract_params("orders in DE last 30 days", self.skill, llm)
        self.assertFalse(needs_clarif)
        self.assertEqual(clarif_q, "")
        self.assertEqual(params, {"natco_code": "de", "lookback_days": 30})

    def test_prompt_names_required_keys_verbatim(self):
        llm = MagicMock()
        llm.generate.return_value = MagicMock(text=json.dumps({
            "params": {"natco_code": "de", "lookback_days": 30},
            "needs_clarification": False,
            "clarification_question": "",
        }))
        self.engine.extract_params("orders in DE last 30 days", self.skill, llm)
        sys_prompt = llm.generate.call_args.kwargs["system_prompt"]
        self.assertIn("natco_code", sys_prompt)
        self.assertIn("lookback_days", sys_prompt)


if __name__ == "__main__":
    unittest.main()
