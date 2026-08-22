"""Reading a turn back.

A trace is only worth writing if you can find it from the answer that bothered
you, which is why this reads by answer_id and not by trace_id: the answer is the
thing a person actually has in front of them.
"""
from __future__ import annotations

from fastapi import HTTPException

from tests.test_api import call
from tests.test_answer_stream import _StreamCase

TEMPLATE = "/tenants/{tenant_id}/answers/{answer_id}/trace"


class TraceEndpointTest(_StreamCase):
    def answer_once(self):
        self.approve_base()
        out, _ = self.first_turn()
        return out["answer_id"]

    def test_returns_the_records_for_that_answer(self):
        answer_id = self.answer_once()
        body = call(self.app, "GET", TEMPLATE, self.tid, answer_id)
        self.assertEqual(body["answer_id"], answer_id)
        self.assertTrue(body["trace_id"])
        self.assertTrue(body["records"])
        self.assertIn(body["records"][0]["kind"], ("llm", "retrieval"))

    def test_records_come_back_in_sequence(self):
        answer_id = self.answer_once()
        body = call(self.app, "GET", TEMPLATE, self.tid, answer_id)
        seqs = [r["seq"] for r in body["records"]]
        self.assertEqual(seqs, sorted(seqs))

    def test_the_payload_is_parsed_not_a_json_string(self):
        answer_id = self.answer_once()
        body = call(self.app, "GET", TEMPLATE, self.tid, answer_id)
        self.assertIsInstance(body["records"][0]["payload"], dict)

    def test_stage_filter_narrows_the_result(self):
        answer_id = self.answer_once()
        body = call(self.app, "GET", TEMPLATE, self.tid, answer_id, "planning")
        self.assertTrue(body["records"])
        self.assertTrue(all(r["stage"] == "planning" for r in body["records"]))

    def test_unknown_answer_is_404(self):
        self.answer_once()
        with self.assertRaises(HTTPException) as caught:
            call(self.app, "GET", TEMPLATE, self.tid, "nope")
        self.assertEqual(caught.exception.status_code, 404)

    def test_unknown_tenant_is_404(self):
        answer_id = self.answer_once()
        with self.assertRaises(HTTPException) as caught:
            call(self.app, "GET", TEMPLATE, "tnt_nope", answer_id)
        self.assertEqual(caught.exception.status_code, 404)
