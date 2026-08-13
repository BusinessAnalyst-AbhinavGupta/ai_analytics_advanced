"""CP-13 tests: LLM enrichment is throttled to once-per-TTL per tenant, and the
daily LLM budget is persisted (survives new engine/app instances).

Uses a counting fake LLM — no network, no provider key.
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from analytics_platform.config import Settings
from analytics_platform.junior import JuniorEngine
from analytics_platform.llm.client import LLMResponse
from tests.helpers import make_ctx


class CountingLLM:
    """A live-looking LLM (name != "null") that counts calls and returns lines."""

    name = "gateway"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, system_prompt: str = "", **kwargs) -> LLMResponse:
        self.calls += 1
        return LLMResponse(text="LLM Q one\nLLM Q two", provider="openrouter",
                           model="test", tokens_out=8)


def _eng(ctx, ttl_minutes=60, daily_cap=20):
    tid = ctx.tenants.create_tenant("LlmCacheCo").id
    ctx.tenants.set_company_profile(tid, {
        "name": "LlmCacheCo",
        "targets": [{"name": "Grow orders", "category": "growth", "priority": 1}]})
    ctx.tenants.set_analyst_config(tid, {"junior_depth": 2})
    s = Settings(junior_llm_cache_ttl_minutes=ttl_minutes, llm_daily_cap=daily_cap)
    eng = JuniorEngine(ctx.stores, tenants=ctx.tenants, observability=ctx.obs,
                       settings=s)
    return tid, eng


class TestLlmEnrichmentThrottle(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()

    def tearDown(self):
        self.ctx.close()

    @patch("analytics_platform.junior.make_role_client")
    def test_enrichment_fires_once_per_ttl(self, mock_make_client):
        llm = CountingLLM()
        mock_make_client.return_value = llm
        tid, eng = _eng(self.ctx)
        # questions: second call within TTL must not re-hit the LLM
        eng.suggest_questions(tid)
        eng.suggest_questions(tid)
        self.assertEqual(llm.calls, 1)
        # hypotheses (depth 2): ditto
        eng.suggest_hypotheses(tid)
        eng.suggest_hypotheses(tid)
        self.assertEqual(llm.calls, 2)
        # cached results still include the LLM lines
        q = eng.suggest_questions(tid)
        self.assertTrue(any(s["source"] == "llm" for s in q["suggestions"]))

    @patch("analytics_platform.junior.make_role_client")
    def test_cache_shared_across_engine_instances(self, mock_make_client):
        """The API builds a fresh JuniorEngine per request; the cache must be
        process-wide so a second instance serves without another LLM call."""
        llm = CountingLLM()
        mock_make_client.return_value = llm
        tid, eng = _eng(self.ctx)
        eng.suggest_questions(tid)
        self.assertEqual(llm.calls, 1)
        llm2 = CountingLLM()
        mock_make_client.return_value = llm2
        eng2 = JuniorEngine(self.ctx.stores, tenants=self.ctx.tenants,
                            observability=self.ctx.obs, settings=eng.settings)
        eng2.suggest_questions(tid)
        self.assertEqual(llm2.calls, 0)  # served from the shared cache

    @patch("analytics_platform.junior.make_role_client")
    def test_cache_expires_after_ttl(self, mock_make_client):
        from analytics_platform import junior as junior_mod
        llm = CountingLLM()
        mock_make_client.return_value = llm
        tid, eng = _eng(self.ctx)
        eng.suggest_questions(tid)
        # force the process-wide cache entry stale
        key = eng._llm_cache_key(tid, "questions", 2)
        junior_mod._LLM_ENRICH_CACHE[key] = (time.time() - 7200, "stale")
        eng.suggest_questions(tid)
        self.assertEqual(llm.calls, 2)  # re-fired after expiry

    @patch("analytics_platform.junior.make_role_client")
    def test_daily_budget_persists_across_engine_instances(self, mock_make_client):
        llm = CountingLLM()
        mock_make_client.return_value = llm
        tid, eng = _eng(self.ctx, daily_cap=1)
        eng.suggest_questions(tid)          # fires; budget spent (1/1)
        self.assertEqual(llm.calls, 1)
        # a brand-new engine instance (e.g. after an app restart) still sees the cap
        fresh = CountingLLM()
        mock_make_client.return_value = fresh
        _, eng2 = _eng(self.ctx, daily_cap=1)  # same store
        # reuse the SAME tenant so the persisted budget key matches
        eng2.suggest_questions(tid)         # stale cache (fresh instance) -> tries to fire
        self.assertEqual(fresh.calls, 0)    # blocked by persisted daily budget
        # budget counter persisted in scheduler_state (control plane: it's one
        # file, not one per tenant -- the tenant_id lives inside the key)
        key = f"llm_daily:{tid}:{time.strftime('%Y-%m-%d', time.gmtime())}"
        row = self.ctx.stores.control.query_one(
            "SELECT value FROM scheduler_state WHERE key=?", (key,))
        self.assertEqual(int(float(row["value"])), 1)


if __name__ == "__main__":
    unittest.main()