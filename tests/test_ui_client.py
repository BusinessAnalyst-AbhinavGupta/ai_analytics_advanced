"""Offline tests for the thin API client the Streamlit UI uses."""
from __future__ import annotations

import unittest
from unittest import mock

from requests import HTTPError

from analytics_platform.ui_client import APIClient


class _Resp:
    def __init__(self, data, status=200):
        self._data = data
        self._status = status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self._status >= 400:
            raise HTTPError(str(self._status))


class TestAPIClient(unittest.TestCase):
    def _patch(self, data=None, status=200):
        return mock.patch("analytics_platform.ui_client.requests.request",
                          return_value=_Resp(data or {}, status))

    def test_list_tenants(self):
        with self._patch([{"tenant_id": "t1"}]) as m:
            out = APIClient("http://x").list_tenants()
        self.assertEqual(out, [{"tenant_id": "t1"}])
        self.assertEqual(m.call_args[0][0], "GET")
        self.assertEqual(m.call_args[0][1], "http://x/tenants")

    def test_create_tenant_posts_name(self):
        with self._patch({"tenant_id": "t9"}) as m:
            out = APIClient("http://x").create_tenant("Acme")
        self.assertEqual(out, {"tenant_id": "t9"})
        self.assertEqual(m.call_args[1]["json"], {"name": "Acme"})

    def test_junior_stage_sends_limit(self):
        with self._patch({"stage": 1}) as m:
            out = APIClient("http://x").junior_stage("t1", limit=50)
        self.assertEqual(out["stage"], 1)
        kwargs = m.call_args[1]
        url = m.call_args[0][1]  # requests.request(url was *args[1])
        self.assertEqual(kwargs["params"], {"limit": 50})
        self.assertTrue(url.endswith("/junior/t1/stage"), url)

    def test_triage_approve_posts_ids(self):
        with self._patch({"approved": ["n1"]}) as m:
            out = APIClient("http://x").triage_approve("t1", ["n1"], by="senior",
                                                        notes="checked")
        self.assertEqual(out, {"approved": ["n1"]})
        self.assertEqual(m.call_args[1]["json"],
                         {"ids": ["n1"], "by": "senior", "notes": "checked"})

    def test_http_error_propagates(self):
        with self._patch(status=404):
            with self.assertRaises(HTTPError):
                APIClient("http://x").triage_summary("nope")

    def test_triage_dedupe_posts_keep_drop(self):
        with self._patch({"superseded": ["b"], "rejected": ["c"]}) as m:
            out = APIClient("http://x").triage_dedupe("t1", keep="a",
                                                       drop=["b", "c"],
                                                       by="senior",
                                                       notes="dedupe group")
        self.assertEqual(out, {"superseded": ["b"], "rejected": ["c"]})
        self.assertEqual(m.call_args[0][0], "POST")
        self.assertEqual(m.call_args[0][1], "http://x/triage/t1/dedupe")
        self.assertEqual(m.call_args[1]["json"],
                         {"keep": "a", "drop": ["b", "c"],
                          "by": "senior", "notes": "dedupe group"})


    def test_get_tenant_returns_profile(self):
        with self._patch({"tenant": {"id": "t1"}, "profile": {"name": "Biz"}}) as m:
            out = APIClient("http://x").get_tenant("t1")
        self.assertEqual(out["profile"]["name"], "Biz")
        self.assertEqual(m.call_args[0][0], "GET")
        self.assertEqual(m.call_args[0][1], "http://x/tenants/t1")

    def test_set_profile_posts_business_context(self):
        with self._patch({"tenant_id": "t1", "profile": {"name": "Biz"}}) as m:
            out = APIClient("http://x").set_profile("t1", {"name": "Biz", "targets": []})
        self.assertEqual(out["profile"]["name"], "Biz")
        self.assertEqual(m.call_args[0][0], "PUT")
        self.assertEqual(m.call_args[0][1], "http://x/tenants/t1/company-profile")
        self.assertEqual(m.call_args[1]["json"], {"name": "Biz", "targets": []})

    def test_list_datasources(self):
        with self._patch([{"name": "Events", "tables": ["t1"]}]) as m:
            out = APIClient("http://x").list_datasources("t1")
        self.assertEqual(out[0]["tables"], ["t1"])
        self.assertEqual(m.call_args[0][1], "http://x/tenants/t1/datasources")

    def test_add_datasource_posts(self):
        with self._patch({"datasource_id": "ds1"}) as m:
            out = APIClient("http://x").add_datasource("t1", "Events", dialect="athena",
                                                       tables=["t1", "t2"])
        self.assertEqual(out["datasource_id"], "ds1")
        self.assertEqual(m.call_args[0][0], "POST")
        self.assertEqual(m.call_args[0][1], "http://x/tenants/t1/datasources")
        self.assertEqual(m.call_args[1]["json"],
                         {"name": "Events", "kind": "direct_db", "dialect": "athena",
                          "tables": ["t1", "t2"], "connected": True})

    def test_profile_history_fetches_versions(self):
        with self._patch([{"version": 2, "changed_by": "x", "snapshot": {}}]) as m:
            out = APIClient("http://x").profile_history("t1", limit=50)
        self.assertEqual(out[0]["version"], 2)
        self.assertEqual(m.call_args[0][0], "GET")
        self.assertEqual(m.call_args[0][1], "http://x/tenants/t1/company-profile/history")
        self.assertEqual(m.call_args[1]["params"], {"limit": 50})

    # -- Phase 9 observability client ------------------------------------
    def test_observability_status(self):
        with self._patch({"retention_days": 30, "purge": {}}) as m:
            out = APIClient("http://x").observability_status()
        self.assertEqual(out["retention_days"], 30)
        self.assertEqual(m.call_args[0][0], "GET")
        self.assertEqual(m.call_args[0][1], "http://x/observability/status")

    def test_observability_logs(self):
        with self._patch({"logs": [{"status": 200}]}) as m:
            out = APIClient("http://x").observability_logs(tenant="t1", limit=50)
        self.assertEqual(out["logs"][0]["status"], 200)
        self.assertEqual(m.call_args[0][1], "http://x/observability/logs")
        self.assertEqual(m.call_args[1]["params"], {"tenant_id": "t1", "limit": 50})

    def test_observability_purge(self):
        with self._patch({"ran": True, "expired_rows": 0}) as m:
            out = APIClient("http://x").observability_purge()
        self.assertEqual(out["ran"], True)
        self.assertEqual(m.call_args[0][0], "POST")
        self.assertEqual(m.call_args[0][1], "http://x/observability/purge")

    def test_observability_junior_run(self):
        with self._patch({"ran": False, "reason": "outside_window"}) as m:
            out = APIClient("http://x").observability_junior_run(tenant="t1")
        self.assertEqual(out["reason"], "outside_window")
        self.assertEqual(m.call_args[0][0], "POST")
        self.assertEqual(m.call_args[0][1], "http://x/observability/junior/run")
        self.assertEqual(m.call_args[1]["params"], {"tenant_id": "t1"})


class TestUIAPIConnectivity(unittest.TestCase):
    """Requirement 5.4: every front-end control is wired to an API client method."""

    def test_all_ui_client_calls_exist_on_apiclient(self):
        """Every `_client().<method>(` used in standalone_ui.py must exist on APIClient,
        so no control can silently dangle (no-op from a missing method)."""
        import re as _re
        import pathlib

        src = pathlib.Path(__file__).resolve().parents[1] / "standalone_ui.py"
        text = src.read_text()
        methods = set(_re.findall(r"_client\(\)\.(\w+)\(", text))
        self.assertTrue(len(methods) >= 10, f"expected many UI endpoints, got {methods}")
        missing = sorted(m for m in methods if not hasattr(APIClient, m))
        self.assertEqual(missing, [], f"UI uses undeclared APIClient methods: {missing}")


if __name__ == "__main__":
    unittest.main()