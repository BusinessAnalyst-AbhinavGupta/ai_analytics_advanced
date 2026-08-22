"""The trace sink: what it writes, what it clips, and what it refuses to break.

The single most important assertion in this file is
`test_a_failing_write_does_not_raise`. Tracing is an observability feature
bolted onto the answer path; the moment it can take a turn down it has cost
more than it is worth.
"""
from __future__ import annotations

import json
import tempfile
import unittest

from analytics_platform.database import TENANT_SCHEMA, Store
from analytics_platform import tracing


class TraceSinkTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(f"{self._tmp.name}/t.db", schema=TENANT_SCHEMA)
        self.sink = tracing.TraceSink(self.store, "tnt_x", "trace-1")

    def tearDown(self):
        self._tmp.cleanup()

    def rows(self):
        return self.store.query_all(
            "SELECT trace_id, seq, stage, kind, payload, tokens_in, ok "
            "FROM llm_traces ORDER BY seq")

    def test_record_writes_a_row_with_an_incrementing_seq(self):
        self.sink.record("llm", {"prompt": "a"})
        self.sink.record("llm", {"prompt": "b"})
        rows = self.rows()
        self.assertEqual([r["seq"] for r in rows], [1, 2])
        self.assertEqual([r["trace_id"] for r in rows], ["trace-1", "trace-1"])
        self.assertEqual(json.loads(rows[0]["payload"])["prompt"], "a")

    def test_record_stamps_the_current_stage(self):
        token = tracing.set_stage("planning")
        try:
            self.sink.record("llm", {"prompt": "a"})
        finally:
            tracing.reset_stage(token)
        self.assertEqual(self.rows()[0]["stage"], "planning")

    def test_stage_defaults_to_unattributed(self):
        self.sink.record("llm", {"prompt": "a"})
        self.assertEqual(self.rows()[0]["stage"], "unattributed")

    def test_long_fields_are_clipped_and_marked(self):
        sink = tracing.TraceSink(self.store, "tnt_x", "trace-1", max_field=10)
        sink.record("llm", {"prompt": "x" * 50})
        payload = json.loads(self.rows()[0]["payload"])
        self.assertEqual(payload["prompt"], "x" * 10)
        self.assertTrue(payload["prompt_truncated"])
        self.assertEqual(payload["prompt_len"], 50)

    def test_short_fields_are_not_marked_truncated(self):
        self.sink.record("llm", {"prompt": "short"})
        payload = json.loads(self.rows()[0]["payload"])
        self.assertNotIn("prompt_truncated", payload)

    def test_a_failing_write_does_not_raise(self):
        self.store.conn.close()          # every write from here on will throw
        self.sink.record("llm", {"prompt": "a"})   # must not raise

    def test_module_level_record_is_a_noop_without_a_sink(self):
        tracing.record("llm", {"prompt": "a"})     # must not raise
        self.assertEqual(self.rows(), [])

    def test_module_level_record_uses_the_active_sink(self):
        token = tracing.use_sink(self.sink)
        try:
            tracing.record("llm", {"prompt": "a"})
        finally:
            tracing.reset_sink(token)
        self.assertEqual(len(self.rows()), 1)
