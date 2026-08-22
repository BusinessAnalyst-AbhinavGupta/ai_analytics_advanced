"""The wrapper is transparent, and it is total.

Transparent: `generate` returns the inner client's exact response object, so no
caller can tell it is there. Total: it records every call, including the ones a
retry loop makes, because the point of tracing at the boundary rather than the
call site is that nobody has to remember to trace.
"""
from __future__ import annotations

import json
import tempfile
import unittest

from analytics_platform import tracing
from analytics_platform.database import TENANT_SCHEMA, Store
from analytics_platform.llm.client import LLMResponse
from analytics_platform.llm.tracing import TracingLLMClient


class _Inner:
    def __init__(self, response=None, raises=False):
        self.response = response or LLMResponse(
            text="hello", provider="p", model="m", tokens_in=7, tokens_out=3)
        self.raises = raises
        self.calls = []

    def generate(self, prompt, system_prompt="", *, temperature=0.0, **kw):
        self.calls.append((prompt, system_prompt, temperature))
        if self.raises:
            raise RuntimeError("gateway down")
        return self.response


class TracingClientTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(f"{self._tmp.name}/t.db", schema=TENANT_SCHEMA)
        self.sink = tracing.TraceSink(self.store, "tnt_x", "trace-1")
        self._token = tracing.use_sink(self.sink)

    def tearDown(self):
        tracing.reset_sink(self._token)
        self._tmp.cleanup()

    def payloads(self):
        return [json.loads(r["payload"]) for r in self.store.query_all(
            "SELECT payload FROM llm_traces ORDER BY seq")]

    def test_returns_the_inner_response_object_unchanged(self):
        inner = _Inner()
        got = TracingLLMClient(inner).generate("q", "sys", temperature=0.2)
        self.assertIs(got, inner.response)

    def test_passes_arguments_through_untouched(self):
        inner = _Inner()
        TracingLLMClient(inner).generate("q", "sys", temperature=0.2)
        self.assertEqual(inner.calls, [("q", "sys", 0.2)])

    def test_records_the_prompt_and_the_verbatim_response(self):
        TracingLLMClient(_Inner()).generate("q", "sys")
        p = self.payloads()[0]
        self.assertEqual(p["prompt"], "q")
        self.assertEqual(p["system_prompt"], "sys")
        self.assertEqual(p["response_text"], "hello")
        self.assertEqual(p["model"], "m")

    def test_records_every_call_including_retries(self):
        client = TracingLLMClient(_Inner())
        for _ in range(3):
            client.generate("q")
        self.assertEqual(len(self.payloads()), 3)

    def test_a_raising_inner_client_still_records_and_still_raises(self):
        client = TracingLLMClient(_Inner(raises=True))
        with self.assertRaises(RuntimeError):
            client.generate("q")
        p = self.payloads()[0]
        self.assertFalse(p["ok"])
        self.assertIn("gateway down", p["error"])

    def test_no_sink_means_no_records_and_no_error(self):
        tracing.reset_sink(self._token)
        self._token = tracing.use_sink(None)
        got = TracingLLMClient(_Inner()).generate("q")
        self.assertEqual(got.text, "hello")
        self.assertEqual(self.payloads(), [])


class PassthroughTest(unittest.TestCase):
    def test_unknown_attributes_reach_the_inner_client(self):
        """`_llm_live` sniffs `client.name`; if the wrapper hid it, every turn
        would think the LLM was offline and take the non-LLM path."""
        class _Named:
            name = "gateway"

            def generate(self, *a, **kw):
                return LLMResponse(text="")
        self.assertEqual(TracingLLMClient(_Named()).name, "gateway")
