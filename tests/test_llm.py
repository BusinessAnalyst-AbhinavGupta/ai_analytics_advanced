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