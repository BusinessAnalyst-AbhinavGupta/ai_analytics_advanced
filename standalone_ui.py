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

import pandas as pd
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


_KINDS = ["", "QUERY", "DEFINITION", "IDIOM", "BUSINESS_RULE"]
_ACTIONABLE = {"CANDIDATE", "UNDER_REVIEW", "REVISION_REQUIRED"}


def _actionable(rows):
    """Only nodes you can still approve/reject (REJECTED/APPROVED are excluded)."""
    return [r for r in rows if r.get("status") in _ACTIONABLE]


def _run_review(tenant, fn, confirm_msg):
    """Run an API triage write and reflect the outcome, re-running the app."""
    res = _guarded(fn)
    if res:
        st.success(f"{confirm_msg}: approved={len(res.get('approved', []))} "
                   f"rejected={len(res.get('rejected', []))} "
                   f"skipped={len(res.get('skipped', []))}")
        st.rerun()


def _queue_review(tenant):
    st.subheader("Queue review")
    c = st.columns([1, 1, 1, 1, 1, 1])
    kind = c[0].selectbox("Kind", _KINDS, key="qkind")
    search = c[1].text_input("Search title/summary", key="qsearch")
    by = c[2].text_input("Reviewer", value="senior", key="reviewer")
    notes = c[3].text_input("Notes (optional)", key="qnotes")
    refresh = c[5].button("Refresh")
    rows = _guarded(lambda: _client().triage_queue(tenant, kind=kind, search=search,
                                                   limit=2000)) or []
    rows = _actionable(rows)
    if not rows:
        st.info("Queue is empty for this filter.")
        return

    df = pd.DataFrame([{
        "Select": False,
        "id": r.get("id"), "kind": r.get("kind"), "status": r.get("status"),
        "title": r.get("title"), "summary": (r.get("summary") or "").strip()[:140],
    } for r in rows])
    edited = st.data_editor(
        df, hide_index=True, width="stretch", num_rows="fixed",
        disabled=[col for col in df.columns if col != "Select"],
        column_config={"Select": st.column_config.CheckboxColumn("✓", width="small")},
    )
    selected = edited[edited["Select"]].id.tolist()

    a = st.columns(4)
    if a[0].button(f"Approve selected ({len(selected)})", disabled=not selected, key="q-appr"):
        _run_review(tenant, lambda: _client().triage_approve(tenant, selected, by=by, notes=notes),
                    f"Approved {len(selected)}")
    if a[1].button(f"Reject selected ({len(selected)})", disabled=not selected, key="q-rej"):
        _run_review(tenant, lambda: _client().triage_reject(tenant, selected, by=by, notes=notes),
                    f"Rejected {len(selected)}")
    if a[2].button(f"Bulk-approve all '{kind or 'any kind'}'", disabled=not kind, key="q-bulk-appr"):
        _run_review(tenant, lambda: _client().triage_bulk(tenant, kind=kind, action="approve",
                                                          by=by, notes=notes),
                    "Bulk-approve")
    if a[3].button(f"Bulk-reject all '{kind or 'any kind'}'", disabled=not kind, key="q-bulk-rej"):
        _run_review(tenant, lambda: _client().triage_bulk(tenant, kind=kind, action="reject",
                                                          by=by, notes=notes),
                    "Bulk-reject")

    with st.expander("Inspect a node before deciding"):
        ids = [r.get("id") for r in rows]
        rows_by_id = {r.get("id"): r for r in rows}
        pick = st.selectbox("Node", ids,
                            format_func=lambda i: f"{i} — {rows_by_id[i]['title'][:60]}")
        if pick:
            node = rows_by_id[pick]
            st.json({k: node.get(k) for k in
                     ("id", "kind", "status", "title", "summary", "payload", "source_ref")})


def _conflicts_review(tenant):
    st.subheader("Conflicts (probable value-set dups)")
    conflicts = _guarded(lambda: _client().triage_conflicts(tenant)) or []
    st.caption(f"{len(conflicts)} title-conflicts — pick one per group to keep, "
               "reject the rest to dedupe.")
    if not conflicts:
        st.info("No conflicts.")
        return
    for cf in conflicts:
        ids = cf.get("ids", [])
        with st.expander(f"`{cf.get('title')}` — {cf.get('count')} nodes"):
            st.code("\n".join(ids))
            b = st.columns([1, 1, 3])
            group = st.radio("Keep / drop group", ids, label_visibility="collapsed",
                             key=f"keep-{ids[0]}") if len(ids) > 1 else None
            if b[0].button("Reject other(s) (keep one)", key=f"r-{ids[0]}") and group:
                rest = [i for i in ids if i != group]
                _run_review(tenant, lambda: _client().triage_reject(tenant, rest, by="senior",
                                                                    notes="dedupe group"),
                            f"Rejected {len(rest)}")
            if b[1].button("Approve whole group", key=f"ap-{ids[0]}"):
                _run_review(tenant, lambda: _client().triage_approve(tenant, ids, by="senior"),
                            "Approved group")


def _definition_meta(node):
    """Human-readable view of a DEFINITION node (column X uses values Y)."""
    p = node.get("payload") or {}
    col = p.get("column") or ""
    vals = p.get("values")
    vals_s = ", ".join(map(str, vals)) if isinstance(vals, list) else str(vals or "")
    sql = p.get("source_sql") or ""
    ctx = " | ".join(
        line.lstrip().lstrip("-").strip()
        for line in sql.splitlines()[:10]
        if line.lstrip().startswith("--")
        and any(k in line for k in ("Business Problem", "Journey Stage", "Instructions")))
    return col, vals_s, ctx, sql


def _definitions_review(tenant):
    st.subheader("Definition review (value-sets by column)")
    rows = _guarded(lambda: _client().triage_queue(tenant, kind="DEFINITION", limit=2000)) or []
    rows = _actionable(rows)
    if not rows:
        st.info("No DEFINITION nodes to review.")
        return
    metas = [_definition_meta(r) for r in rows]
    ncols = len({c for c, *_ in metas if c})
    by = st.text_input("Reviewer", value="senior", key="drev")
    notes = st.text_input("Notes (optional)", key="dnotes")
    st.caption(f"{len(rows)} DEFINITION nodes across {ncols} columns — each is "
               "“column X uses value(s) Y” from a source query. Sortable by column; approve only "
               "what's correct & worth keeping. The Conflicts tab dedupes overlapping value-sets.")
    df = pd.DataFrame([{
        "Select": False,
        "id": r.get("id"), "column": m[0], "values": m[1], "status": r.get("status"),
        "context": m[2][:120],
    } for r, m in zip(rows, metas)]).sort_values("column").reset_index(drop=True)
    edited = st.data_editor(
        df, hide_index=True, width="stretch", num_rows="fixed",
        disabled=[c for c in df.columns if c != "Select"],
        column_config={"Select": st.column_config.CheckboxColumn("✓", width="small")},
    )
    selected = edited[edited["Select"]].id.tolist()
    a = st.columns(3)
    if a[0].button(f"Approve selected ({len(selected)})", disabled=not selected, key="d-appr"):
        _run_review(tenant, lambda: _client().triage_approve(tenant, selected, by=by, notes=notes),
                    "Approved")
    if a[1].button(f"Reject selected ({len(selected)})", disabled=not selected, key="d-rej"):
        _run_review(tenant, lambda: _client().triage_reject(tenant, selected, by=by, notes=notes),
                    "Rejected")
    if a[2].button("Bulk-approve all DEFINITIONs", key="d-bulk"):
        _run_review(tenant, lambda: _client().triage_bulk(tenant, kind="DEFINITION",
                                                          action="approve", by=by, notes=notes),
                    "Bulk-approve DEFINITION")
    with st.expander("See a definition's source SQL"):
        ids = [r.get("id") for r in rows]
        m = dict(zip(ids, metas))
        pick = st.selectbox("Definition", ids,
                            format_func=lambda i: f"{i} — {m[i][0]} = “{m[i][1][:40]}”")
        col, vals_s, ctx, sql = m[pick]
        st.markdown(f"**{col}** uses: `{vals_s}`")
        st.code(sql or "(no source_sql)", language="sql")


def _stakeholder_tab(tenant):
    st.subheader("Stakeholder analyst (P6)")
    st.caption("Low-cost, approved-knowledge-first answers. High-risk questions escalate; "
               "answers carry citations + freshness + cost.")
    q = st.text_input("Ask the stakeholder analyst", key="sk_q")
    if st.button("Answer", key="sk_ask") and q.strip():
        res = _guarded(lambda: _client().stakeholder_answer(tenant, q.strip()))
        if res:
            st.write(res.get("answer") or "(escalated / no answer)")
            st.json(res)
            st.session_state["sk_last"] = res.get("answer_id")
    last = st.session_state.get("sk_last")
    c1, c2 = st.columns([1, 1])
    c1.button("👍 helpful", key="sk_up",
              on_click=lambda: _client().stakeholder_feedback(tenant, last, rating="up")
              if last else None)
    c2.button("👎 not helpful", key="sk_down",
              on_click=lambda: _client().stakeholder_feedback(tenant, last, rating="down")
              if last else None)
    st.subheader("Answer quality")
    st.json(_guarded(lambda: _client().stakeholder_quality(tenant)) or {})


def _research_tab(tenant):
    st.subheader("External research (P7)")
    _guarded(lambda: _client().research_seed(tenant))
    st.caption("Cited, source-credit-classified external claims — they land as EXTERNAL "
               "CANDIDATE nodes and only the senior gate can promote them.")
    cols = st.columns([3, 2])
    query = cols[0].text_input("Search topic", key="rs_q")
    url = cols[1].text_input("Source URL", key="rs_url")
    if st.button("Search + capture (via approved providers)", key="rs_go"):
        results = [{"source_name": "Official documentation",
                    "title": query, "url": url or "https://docs.example.com/1",
                    "kind": "official", "snippet": f"External claim on {query}."}]
        found = _guarded(lambda: _client().research_search(tenant, query, results)) or []
        _guarded(lambda: _client().research_capture(tenant, query, found))
    ov = _guarded(lambda: _client().research_overview(tenant)) or {}
    st.json(ov)
    docs = _guarded(lambda: _client().research_docs(tenant)) or []
    if docs:
        df = pd.DataFrame([{"id": d.get("id"), "credibility": d.get("credibility"),
                            "origin": d.get("origin"), "title": (d.get("title") or "")[:60],
                            "url": d.get("url")} for d in docs])
        st.dataframe(df, hide_index=True, width="stretch")
        pick = st.selectbox("Promote to Brain (stays CANDIDATE until senior approves)",
                            [d.get("id") for d in docs],
                            format_func=lambda i: i)
        if st.button("Promote", key="rs_promote") and pick:
            n = _guarded(lambda: _client().research_promote(tenant, pick))
            if n:
                st.success(f"{n['status']} node {n['id']} — awaiting senior approval")


def _governance_tab(tenant):
    st.subheader("Governance (P8)")
    st.caption("RBAC + SSO seam and cross-tenant isolation are enabled by setting "
               "ANALYTICS_AUTH_SECRET / ANALYTICS_AUTH_ENABLED=1.")
    usage = _guarded(lambda: _client().billing_usage(tenant)) or {}
    if usage:
        m = st.columns(4)
        m[0].metric("Spans", usage.get("spans", 0))
        m[1].metric("Failed spans", usage.get("failed_spans", 0))
        m[2].metric("Tokens", usage.get("tokens_in", 0))
        m[3].metric("Cost (USD)", usage.get("cost_usd", {}).get("total", 0.0))
        st.json(usage.get("by_stage", []))


def _tenants() -> list:
    client = _client()
    try:
        # NOTE: GET /tenants serializes the id as `id`; the create endpoint uses `tenant_id`.
        return [t.get("id") or t.get("tenant_id") for t in client.list_tenants()]
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
    tabs = st.tabs(["Junior", "Triage", "Stakeholder", "Research", "Governance"])
    with tabs[0]:
        st.subheader("Junior maturity stage")
        st.json(_guarded(lambda: _client().junior_stage(tenant)) or {})
        st.subheader("Catalog (schema / EDA)")
        st.json(_guarded(lambda: _client().junior_catalog(tenant)) or {})
        st.subheader("Suggested questions")
        st.json(_guarded(lambda: _client().junior_questions(tenant)) or {})

    with tabs[1]:
        summary = _guarded(lambda: _client().triage_summary(tenant)) or {}
        if summary:
            m = st.columns(4)
            m[0].metric("Total nodes", summary.get("total", 0))
            m[1].metric("Actionable (needs review)", summary.get("actionable", 0))
            m[2].metric("Approved", summary.get("approved", 0))
            m[3].metric("Conflicts", summary.get("conflicts", 0))
        rt1, rt2, rt3 = st.tabs(["Definitions", "Queue review", "Conflicts"])
        with rt1:
            _definitions_review(tenant)
        with rt2:
            _queue_review(tenant)
        with rt3:
            _conflicts_review(tenant)

    with tabs[2]:
        _stakeholder_tab(tenant)
    with tabs[3]:
        _research_tab(tenant)
    with tabs[4]:
        _governance_tab(tenant)
else:
    st.info("Select or create a tenant to begin.")