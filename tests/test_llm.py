"""LLM client seam tests (offline; the static gateway is patched, no network)."""
from __future__ import annotations

import unittest
from unittest import mock

import core.llm_gateway as _gateway_mod
from analytics_platform.llm.client import (GatewayClient, NullClient,
                                           make_client, make_client_from)


class _Settings:
    llm_provider = "openrouter"
    llm_model = "deepseek/deepseek-v4-flash-0731"
    ollama_base_url = "http://localhost:11434"

    def effective_api_key(self):
        return ""


class TestMakeClient(unittest.TestCase):
    def test_null_provider_returns_null_client(self):
        for p in ("null", "", "None"):
            self.assertIsInstance(make_client(p), NullClient)

    def test_provider_returns_gateway_client(self):
        self.assertIsInstance(make_client("openrouter"), GatewayClient)

    def test_from_settings_live(self):
        self.assertIsInstance(make_client_from(_Settings()), GatewayClient)

    def test_from_settings_null(self):
        s = _Settings()
        s.llm_provider = "null"
        self.assertIsInstance(make_client_from(s), NullClient)


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