"""Thin Standalone-Platform UI (Streamlit as an API client).

Runs against the standalone FastAPI (the plan's "Streamlit as a thin API
client"; React/Next later, §5). Pure client: talks only via analytics_platform.
ui_client.APIClient -> the running API. No logic duplicated here.

Run:
  .venv/bin/python -m analytics_platform serve 8000          # API in one terminal
  .venv/bin/streamlit run standalone_ui.py                    # this UI (port 8501)

Set ANALYTICS_API_URL to override the API base (default http://localhost:8000).
"""
from __future__ import annotations

import os

import streamlit as st

from analytics_platform.ui_client import APIClient

st.set_page_config(page_title="Standalone Platform", layout="wide")

BASE_URL = os.environ.get("ANALYTICS_API_URL", "http://localhost:8000")


def _client() -> APIClient:
    if "client" not in st.session_state:
        st.session_state.client = APIClient(BASE_URL)
    return st.session_state.client


def _guarded(fn):
    """Run an API call; return None and show the error on failure."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - surface connection/API errors in the UI
        st.error(f"API error ({BASE_URL}): {e}")
        return None


def _tenants() -> list:
    client = _client()
    try:
        return [t.get("tenant_id") for t in client.list_tenants()]
    except Exception as e:  # noqa: BLE001
        st.error(f"Cannot reach API at {BASE_URL}: {e}. Is `serve 8000` running?")
        return []


st.title("Standalone Analytics Platform")
st.caption(f"API: {BASE_URL}")

with st.sidebar:
    st.header("Tenant")
    tenants = _tenants()
    if tenants:
        tenant = st.selectbox("Tenant", tenants)
        st.session_state["tenant"] = tenant
    else:
        tenant = ""
        st.info("No tenants yet — create one below.")
    new_name = st.text_input("New tenant name", key="new_tenant")
    if st.button("Create tenant") and new_name.strip():
        res = _guarded(lambda: _client().create_tenant(new_name.strip()))
        if res:
            st.success(f"Created {res.get('tenant_id')}")

if tenant:
    tabs = st.tabs(["Junior", "Triage"])
    with tabs[0]:
        st.subheader("Junior maturity stage")
        st.json(_guarded(lambda: _client().junior_stage(tenant)) or {})
        st.subheader("Catalog (schema / EDA)")
        st.json(_guarded(lambda: _client().junior_catalog(tenant)) or {})
        st.subheader("Suggested questions")
        st.json(_guarded(lambda: _client().junior_questions(tenant)) or {})

    with tabs[1]:
        st.subheader("Summary")
        st.json(_guarded(lambda: _client().triage_summary(tenant)) or {})
        kind = st.text_input("Queue kind (e.g. DEFINITION, QUERY)", key="qkind")
        q = _guarded(lambda: _client().triage_queue(tenant, kind=kind.strip()))
        if q:
            ids = [n.get("id") for n in q]
            st.dataframe([{k: n.get(k) for k in ("id", "kind", "status", "title")}
                          for n in q])
            c1, c2 = st.columns(2)
            if c1.button("Approve listed"): 
                st.json(_client().triage_approve(tenant, ids))
            if c2.button("Bulk-approve by kind") and kind.strip():
                st.json(_client().triage_bulk(tenant, kind=kind.strip(), action="approve"))
else:
    st.info("Select or create a tenant to begin.")