"""LLM client seam tests (offline; the static gateway is patched, no network)."""
from __future__ import annotations

import unittest
from unittest import mock

import core.llm_gateway as _gateway_mod
from analytics_platform.llm.client import GatewayClient, NullClient, make_client


class _Settings:
    def __init__(self, llm_provider: str = "openrouter", llm_model: str = "deepseek/deepseek-v4-flash-0731", ollama_base_url: str = "http://localhost:11434"):
        self.llm_provider = llm_provider
        self.llm_model = llm_model
        self.ollama_base_url = ollama_base_url

    def effective_api_key(self):
        return ""


class TestMakeClient(unittest.TestCase):
    def test_null_provider_returns_null_client(self):
        for p in ("null", "", "None"):
            self.assertIsInstance(make_client(p), NullClient)

    def test_provider_returns_gateway_client(self):
        self.assertIsInstance(make_client("openrouter"), GatewayClient)

    def test_make_role_client(self):
        s = _Settings(llm_provider="openrouter", llm_model="global-model")
        from analytics_platform.domain import AnalystAI
        role_ai = AnalystAI(role="junior", enabled=True, provider="ollama", model="llama3")
        from analytics_platform.llm.client import GatewayClient, make_role_client
        client = make_role_client(s, role_ai)
        self.assertIsInstance(client, GatewayClient)
        self.assertEqual(client.provider, "ollama")
        self.assertEqual(client.model, "llama3")

    def test_make_role_client_disabled(self):
        s = _Settings(llm_provider="openrouter", llm_model="global-model")
        from analytics_platform.domain import AnalystAI
        role_ai = AnalystAI(role="junior", enabled=False, provider="ollama", model="llama3")
        from analytics_platform.llm.client import NullClient, make_role_client
        client = make_role_client(s, role_ai)
        self.assertIsInstance(client, NullClient)

    def test_make_role_client_fallback_to_settings(self):
        s = _Settings(llm_provider="openrouter", llm_model="global-model")
        from analytics_platform.domain import AnalystAI
        role_ai = AnalystAI(role="junior", enabled=True, provider="", model="")
        from analytics_platform.llm.client import GatewayClient, make_role_client
        client = make_role_client(s, role_ai)
        self.assertIsInstance(client, GatewayClient)
        self.assertEqual(client.provider, "openrouter")
        self.assertEqual(client.model, "global-model")

    def test_make_role_client_none_role(self):
        s = _Settings(llm_provider="openrouter", llm_model="global-model")
        from analytics_platform.llm.client import GatewayClient, make_role_client
        client = make_role_client(s, None)
        self.assertIsInstance(client, GatewayClient)
        self.assertEqual(client.provider, "openrouter")
        self.assertEqual(client.model, "global-model")

    def test_make_role_client_null_provider(self):
        s = _Settings(llm_provider="openrouter", llm_model="global-model")
        from analytics_platform.domain import AnalystAI
        role_ai = AnalystAI(role="junior", enabled=True, provider="null", model="llama3")
        from analytics_platform.llm.client import NullClient, make_role_client
        client = make_role_client(s, role_ai)
        self.assertIsInstance(client, NullClient)

    def test_make_role_client_hydrates_api_key_from_settings(self):
        s = _Settings(llm_provider="openrouter", llm_model="global-model")
        s.effective_api_key = lambda: "secret-key-123"
        from analytics_platform.domain import AnalystAI
        role_ai = AnalystAI(role="junior", enabled=True, provider="openrouter", model="deepseek")
        from analytics_platform.llm.client import GatewayClient, make_role_client
        client = make_role_client(s, role_ai)
        self.assertIsInstance(client, GatewayClient)
        self.assertEqual(client.api_key, "secret-key-123")


class TestGatewayClient(unittest.TestCase):
    def _fake_generate(self, resp):
        return lambda **kw: resp

    def test_string_response_adapts(self):
        with mock.patch.object(_gateway_mod.LLMGateway, "generate",
                               self._fake_generate("plain text")):
            r = GatewayClient(provider="p", model="m").generate("hi")
        self.assertEqual(r.text, "plain text")
        self.assertEqual(r.provider, "p")
        self.assertTrue(r.ok)

    def test_dict_response_adapts(self):
        payload = {"text": "t", "provider": "pp", "model": "mm",
                   "tokens_in": 10, "tokens_out": 20, "ok": True}
        with mock.patch.object(_gateway_mod.LLMGateway, "generate",
                               self._fake_generate(payload)):
            r = GatewayClient().generate("hi")
        self.assertEqual(r.text, "t")
        self.assertEqual(r.tokens_in, 10)
        self.assertEqual(r.tokens_out, 20)

    def test_static_only_invariant(self):
        """GatewayClient must use the class-level static generate, never an instance."""
        with mock.patch.object(_gateway_mod.LLMGateway, "__init__", return_value=None) as init:
            GatewayClient(provider="p")
        init.assert_not_called()


if __name__ == "__main__":
    unittest.main()

# --------------------------------------------------------------------------- #
# Reasoning-model responses
#
# A reasoning model can come back with an EMPTY `content` and put everything it
# produced under `reasoning_details` -- which OpenRouter sends as a list of
# typed blocks, not a string. The gateway used to fall back with
# `str(reasoning_details)`, so the caller received the Python repr of a list of
# dicts: "[{'type': 'reasoning.text', 'text': '...'}]". Nothing downstream can
# parse that. It is what silently dropped every turn plan onto the ungoverned
# aggregate path while the log showed the model had chosen correctly.
# --------------------------------------------------------------------------- #
class TestReasoningText(unittest.TestCase):
    def extract(self, msg):
        return _gateway_mod._reasoning_text(msg)

    def test_typed_blocks_yield_their_text_not_a_python_repr(self):
        out = self.extract({"reasoning_details": [
            {"type": "reasoning.text", "text": '{"base_view":"checkout_sessions"}',
             "index": 0, "format": "unknown"}]})
        self.assertEqual(out, '{"base_view":"checkout_sessions"}')

    def test_several_blocks_are_joined_in_order(self):
        out = self.extract({"reasoning_details": [
            {"type": "reasoning.text", "text": "first"},
            {"type": "reasoning.text", "text": "second"}]})
        self.assertEqual(out, "first\nsecond")

    def test_a_summary_block_is_used_when_it_carries_no_text(self):
        self.assertEqual(
            self.extract({"reasoning_details": [{"type": "reasoning.summary",
                                                 "summary": "summarised"}]}),
            "summarised")

    def test_the_plain_string_form_is_used_when_there_are_no_blocks(self):
        self.assertEqual(self.extract({"reasoning": "plain reasoning"}),
                         "plain reasoning")

    def test_blocks_win_over_the_plain_string(self):
        """The blocks are the full trace; `reasoning` is the same thing flattened."""
        self.assertEqual(
            self.extract({"reasoning_details": [{"text": "from blocks"}],
                          "reasoning": "from string"}),
            "from blocks")

    def test_an_empty_block_list_falls_back_to_the_plain_string(self):
        self.assertEqual(self.extract({"reasoning_details": [],
                                       "reasoning": "plain"}), "plain")

    def test_unusable_blocks_fall_back_to_the_plain_string(self):
        self.assertEqual(
            self.extract({"reasoning_details": [{"type": "reasoning.encrypted"}],
                          "reasoning": "plain"}), "plain")

    def test_a_message_with_no_reasoning_at_all_yields_empty(self):
        self.assertEqual(self.extract({"content": ""}), "")

    def test_a_non_string_reasoning_field_does_not_leak_a_repr(self):
        self.assertEqual(self.extract({"reasoning": {"unexpected": "shape"}}), "")
