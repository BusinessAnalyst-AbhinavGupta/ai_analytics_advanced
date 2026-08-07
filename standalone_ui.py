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
                   f"superseded={len(res.get('superseded', []))} "
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
    # status map so each group can show WHY a node will be superseded vs rejected
    rows = _guarded(lambda: _client().triage_queue(tenant, limit=2000)) or []
    status_by_id = {r.get("id"): r.get("status", "?") for r in rows}
    st.caption(f"{len(conflicts)} conflict groups — pick one per group to keep. "
               "Dropping others: CANDIDATE/UNDER_REVIEW are rejected, APPROVED are "
               "superseded (kept node wins).")
    if not conflicts:
        st.info("No conflicts.")
        return
    for cf in conflicts:
        ids = cf.get("ids", [])
        with st.expander(f"`{cf.get('title')}` — {cf.get('count')} nodes"):
            st.code("\n".join(f"{i}  [{status_by_id.get(i, '?')}]" for i in ids))
            b = st.columns([1, 1, 3])
            group = st.radio("Keep / drop group", ids, label_visibility="collapsed",
                             key=f"keep-{ids[0]}") if len(ids) > 1 else None
            if b[0].button("Drop other(s) (keep one)", key=f"r-{ids[0]}") and group:
                rest = [i for i in ids if i != group]
                _run_review(tenant, lambda: _client().triage_dedupe(tenant, keep=group,
                                                                    drop=rest,
                                                                    by="senior",
                                                                    notes="dedupe group"),
                            f"Cleared {len(rest)}")
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


def _observability_tab(tenant):
    st.subheader("Observability (Phase 9) - owner-facing")
    st.caption("API access logs are kept 30 days and purged weekly by the "
               "scheduler; the background junior analyst runs serially, only "
               "inside its work window, at most once per hour.")
    status = _guarded(lambda: _client().observability_status()) or {}
    st.markdown("**Scheduler / retention**")
    cols = st.columns(4)
    cols[0].metric("Log retention (days)", status.get("retention_days"))
    cols[1].metric("Maintenance (days)", status.get("maintenance_interval_days"))
    cols[2].metric("Scheduler enabled", status.get("scheduler_enabled"))
    purge = status.get("purge") or {}
    cols[3].metric("Last purge", "yes" if purge.get("last_purge_ts") else "never")

    j = status.get("junior")
    if j:
        st.markdown("**Background junior**")
        jc = st.columns(4)
        jc[0].metric("Tenant", j.get("tenant_id", "")[:22])
        jc[1].metric("Work window", j.get("work_window", ""))
        jc[2].metric("Min interval (m)", j.get("min_interval_minutes"))
        jc[3].metric("In window", "yes" if j.get("in_window") else "no")

    if st.button("Run weekly log purge now", key=f"obs_purge_{tenant}"):
        st.write(_guarded(lambda: _client().observability_purge()))
    if st.button("Trigger one junior cycle (honours window/rate)",
                 key=f"obs_jr_{tenant}"):
        st.write(_guarded(lambda: _client().observability_junior_run(tenant)))

    with st.expander("Recent API logs", expanded=False):
        logs = _guarded(lambda: _client().observability_logs(tenant=tenant)) or {}
        rows = logs.get("logs") or []
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True)
        else:
            st.write("No API logs recorded yet.")
    with st.expander("Platform metrics", expanded=False):
        import requests as _req
        try:
            st.json(_req.get("http://localhost:8000/metrics", timeout=10).json())
        except Exception:
            st.write("(metrics endpoint not reachable here)")


_PROVIDERS = ["openrouter", "gemini", "ollama"]


def _provider_index(p) -> int:
    try:
        return _PROVIDERS.index((p or "openrouter").lower())
    except ValueError:
        return 0


def _config_tab(tenant):
    """Config panel: per-analyst AI toggles + per-role model, junior depth,
    human-signoff window, live provider ping, and versioned history."""
    st.subheader("Analyst AI config (config panel)")
    cfg = _guarded(lambda: _client().get_analyst_config(tenant)) or {}
    if not cfg:
        st.info("No analyst config yet — save one below.")
        return

    depth = int(cfg.get("junior_depth", 1))
    st.caption(f"Junior question depth: **{cfg.get('depth_label', 'standard')}** "
               f"({depth}/2) — human-controlled on this tab; higher = deeper business "
               f"questions **+ hypotheses**, lower = basic questions.")
    d = st.columns([1, 1, 3])
    if d[0].button("⬆ Promote junior", key=f"cfg_up_{tenant}"):
        _guarded(lambda: _client().senior_junior_depth(tenant, action="up", by="human"))
        st.rerun()
    if d[1].button("⬇ Downgrade junior", key=f"cfg_dn_{tenant}"):
        _guarded(lambda: _client().senior_junior_depth(tenant, action="down", by="human"))
        st.rerun()
    d[2].caption("Promoting makes the junior ask deeper questions and business "
                 "hypotheses; demoting pulls it back to basic questions.")

    st.markdown("### Per-role AI toggles + model (save to apply)")
    roles = ["junior", "senior", "stakeholder"]
    new = {}
    for role in roles:
        rc = cfg.get(role) or {}
        st.markdown(f"**{role.title()} analyst**")
        cols = st.columns([2, 3, 2, 1])
        enabled = cols[0].toggle(f"AI enabled", value=bool(rc.get("enabled", True)),
                                 key=f"cfg_on_{role}_{tenant}")
        provider = cols[1].selectbox("provider", _PROVIDERS,
                                     index=_provider_index(rc.get("provider")),
                                     key=f"cfg_pr_{role}_{tenant}")
        model = cols[2].text_input("model", value=rc.get("model", ""),
                                   key=f"cfg_m_{role}_{tenant}")
        new[role] = {"enabled": enabled, "provider": provider, "model": model.strip()}

    hcols = st.columns([2, 2])
    junior_depth = hcols[0].number_input("Junior depth (0-2)", min_value=0, max_value=2,
                                         value=depth, key=f"cfg_depth_{tenant}")
    signoff_days = hcols[1].number_input("Human-signoff days (first N days)",
                                         min_value=0, max_value=365,
                                         value=int(cfg.get("human_signoff_days", 7)),
                                         key=f"cfg_signoff_{tenant}")
    st.caption("During the first N days (default 7) every junior analysis needs an "
               "explicit **human** review — an AI senior can't auto-approve in that window.")

    if st.button("Save config", key=f"cfg_save_{tenant}", type="primary"):
        _guarded(lambda: _client().set_analyst_config(tenant, {
            "junior": new["junior"], "senior": new["senior"],
            "stakeholder": new["stakeholder"],
            "junior_depth": int(junior_depth), "human_signoff_days": int(signoff_days),
            "changed_by": "human"}))
        cp = f"cfg_on_senior_{tenant}"
        st.success(f"Saved (senior AI {'ON' if new['senior']['enabled'] else 'OFF'} → "
                   f"{'notes human-on-top: workload falls to you' if not new['senior']['enabled'] else 'automated'})")
        st.rerun()

    st.markdown("### Provider model options (live ping)")
    pcols = st.columns([2, 3, 1])
    ping_prov = pcols[0].selectbox("Provider", ["openrouter", "ollama"],
                                   key=f"ping_prov_{tenant}")
    ping_key = pcols[1].text_input("Provider key (used once, never stored)",
                                   value="", key=f"ping_key_{tenant}", type="password")
    if pcols[2].button("Ping models", key=f"ping_btn_{tenant}"):
        models = _guarded(lambda: _client().llm_models(provider=ping_prov, key=ping_key)) or []
        st.session_state.setdefault(f"ping_models_{tenant}", models)
        st.session_state[f"ping_prov_raw_{tenant}"] = ping_prov
    models = st.session_state.get(f"ping_models_{tenant}", [])
    if models:
        st.markdown(f"**{len(models)} models from {st.session_state.get(f'ping_prov_raw_{tenant}', '')}**")
        st.dataframe(pd.DataFrame(models), hide_index=True, use_container_width=True)

    st.markdown("### Config history (versioned)")
    hist = _guarded(lambda: _client().analyst_config_history(tenant, limit=20)) or []
    if hist:
        hdf = pd.DataFrame([{
            "version": h.get("version"), "changed_by": h.get("changed_by"),
            "at": h.get("created_at"),
            "junior_depth": (h.get("snapshot") or {}).get("junior_depth"),
            "junior_enabled": (h.get("snapshot") or {}).get("junior", {}).get("enabled"),
            "senior_enabled": (h.get("snapshot") or {}).get("senior", {}).get("enabled"),
            "stakeholder_enabled": (h.get("snapshot") or {}).get("stakeholder", {}).get("enabled"),
        } for h in hist])
        st.dataframe(hdf, hide_index=True, use_container_width=True)
    else:
        st.caption("No config changes logged yet.")
def _business_context_tab(tenant: str):
    """Initialisation: the one business context every analyst reads.

    Writes through the API (PUT company-profile / datasources) so the stored
    context is the single source of truth for Junior, Stakeholder, Research.
    """
    import math

    st.subheader("Business context / Onboarding")
    data = _guarded(lambda: _client().get_tenant(tenant)) or {}
    trow = data.get("tenant") or {}
    profile = data.get("profile") or {}
    targets = [dict(x) for x in (profile.get("targets") or [])]

    st.info(
        "**One source of truth for every analyst.** What the company does, the "
        "product/process it runs, and the OKRs it optimises for (Junior goal "
        "alignment, Stakeholder answers and Research targeting all read this). "
        "Set it per tenant - the analysts inherit it."
    )

    lc, rc = st.columns(2)
    name = lc.text_input("Company name", value=profile.get("name") or trow.get("name") or "",
                         key=f"biz_name_{tenant}")
    industry = lc.text_input("Industry", value=profile.get("industry", ""),
                             key=f"biz_ind_{tenant}")
    region = lc.text_input("Region", value=profile.get("region") or trow.get("region") or "",
                           key=f"biz_reg_{tenant}")
    product = lc.text_input("Product / what you sell", value=profile.get("product", ""),
                            key=f"biz_prod_{tenant}")
    revenue_model = lc.text_input("Revenue model (how you make money)",
                                  value=profile.get("revenue_model", ""),
                                  key=f"biz_rev_{tenant}")
    customers = rc.text_input("Customers (who buys)", value=profile.get("customers", ""),
                              key=f"biz_cust_{tenant}")
    value_creation = rc.text_input("Value creation (the job you do for them)",
                                   value=profile.get("value_creation", ""),
                                   key=f"biz_val_{tenant}")
    description = rc.text_area("What the company / business does",
                               value=profile.get("description", ""),
                               key=f"biz_desc_{tenant}")
    constraints = st.text_input("Constraints (comma-separated)",
                                value=", ".join(profile.get("constraints", [])),
                                key=f"biz_cons_{tenant}")
    risks = st.text_input("Risks (comma-separated)",
                          value=", ".join(profile.get("risks", [])),
                          key=f"biz_risk_{tenant}")
    competitors = st.text_input("Competitors (comma-separated)",
                                value=", ".join(profile.get("competitors", [])),
                                key=f"biz_comp_{tenant}")
    preferred = st.text_input("Preferred metrics (comma-separated)",
                              value=", ".join(profile.get("preferred_metrics", [])),
                              key=f"biz_pref_{tenant}")
    changed_by = st.text_input("Changed by (recorded on this version)",
                               value="owner", key=f"biz_changedby_{tenant}")
    st.caption("Each save appends a dated snapshot to the Business Context history - "
               "product, services and OKRs can evolve over time and are all tracked.")

    st.divider()
    st.markdown("**OKRs / Targets** - what the analysts optimise for (add/remove rows).")
    cols = ["name", "description", "category", "priority", "owner",
            "time_horizon", "target_value", "metric_refs", "constraints",
            "last_reviewed"]
    base_rows = []
    for t in targets:
        base_rows.append({
            "name": t.get("name", ""), "description": t.get("description", ""),
            "category": t.get("category", "growth"), "priority": t.get("priority", 1),
            "owner": t.get("owner", ""), "time_horizon": t.get("time_horizon", "quarterly"),
            "target_value": t.get("target_value"),
            "metric_refs": ", ".join(t.get("metric_refs") or []),
            "constraints": ", ".join(t.get("constraints") or []),
            "last_reviewed": t.get("last_reviewed", ""),
        })
    if not base_rows:
        base_rows = [{"name": "", "description": "", "category": "growth", "priority": 1,
                      "owner": "", "time_horizon": "quarterly", "target_value": None,
                      "metric_refs": "", "constraints": "", "last_reviewed": ""}]
    ed = st.data_editor(
        pd.DataFrame(base_rows, columns=cols),
        num_rows="dynamic", key=f"biz_targets_{tenant}", use_container_width=True,
        column_config={
            "category": st.column_config.SelectboxColumn(
                "Category", options=["growth", "margin", "funnel", "retention",
                                     "risk", "efficiency", "satisfaction"]),
            "priority": st.column_config.NumberColumn("Priority", min_value=1, step=1),
            "target_value": st.column_config.NumberColumn("Target value"),
        },
    )

    st.divider()
    st.markdown("**Data sources** - the tables analysts query.")
    dss = _guarded(lambda: _client().list_datasources(tenant)) or []
    if dss:
        for ds in dss:
            st.markdown(f"- `{ds.get('name')}` · dialect `{ds.get('dialect')}` · "
                        f"tables: {', '.join(ds.get('tables') or [])}")
    else:
        st.caption("No data sources registered yet.")
    dc = st.columns(4)
    ds_name = dc[0].text_input("Datasource name", key=f"biz_dsname_{tenant}")
    ds_dialect = dc[1].text_input("Dialect", value="athena", key=f"biz_dsdial_{tenant}")
    ds_tables = dc[2].text_input("Tables (comma-separated)", key=f"biz_dstab_{tenant}")
    if dc[3].button("Add datasource", key=f"biz_dsadd_{tenant}"):
        ds_tbls = [x.strip() for x in (ds_tables or "").split(",") if x.strip()]
        res = _guarded(lambda: _client().add_datasource(
            tenant, ds_name, dialect=ds_dialect, tables=ds_tbls))
        if res:
            st.success("Data source added")
            st.rerun()

    if st.button("Save business context + OKRs", type="primary",
                 key=f"biz_save_{tenant}"):
        def _lst(v):
            return [x.strip() for x in (v or "").split(",") if x.strip()]

        okrs = []
        for r in ed.to_dict("records"):
            n = (r.get("name") or "").strip()
            if not n:
                continue
            tv = r.get("target_value")
            if tv is None or (isinstance(tv, float) and math.isnan(tv)):
                tv = None
            okrs.append({
                "name": n, "description": r.get("description") or "",
                "category": r.get("category") or "growth",
                "priority": int(r.get("priority") or 1) if r.get("priority") else 1,
                "owner": r.get("owner") or "",
                "time_horizon": r.get("time_horizon") or "quarterly",
                "target_value": tv,
                "metric_refs": _lst(r.get("metric_refs")),
                "constraints": _lst(r.get("constraints")),
                "last_reviewed": r.get("last_reviewed") or "",
            })
        payload = {
            "name": name, "industry": industry, "region": region,
            "description": description, "customers": customers, "product": product,
            "value_creation": value_creation, "revenue_model": revenue_model,
            "targets": okrs, "constraints": _lst(constraints), "risks": _lst(risks),
            "competitors": _lst(competitors), "preferred_metrics": _lst(preferred),
            "changed_by": changed_by or "owner",
        }
        res = _guarded(lambda: _client().set_profile(tenant, payload))
        if res:
            st.success(f"Business context saved as a new version · {len(okrs)} OKR/s")
            st.rerun()

    with st.expander("Version history (business context over time)", expanded=False):
        hist = _guarded(lambda: _client().profile_history(tenant)) or []
        if not hist:
            st.caption("No saved versions yet - save the context above to create version 1.")
        for h in hist:
            snap = h.get("snapshot") or {}
            ts = (h.get("created_at") or "")[:19]
            st.markdown(
                f"**v{h.get('version')}** · {ts} · by `{h.get('changed_by') or 'owner'}` · "
                f"{len(snap.get('targets') or [])} OKR/s | "
                f"{((snap.get('description') or '')[:110]) or snap.get('name') or '(no description)'}"
            )


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
    tabs = st.tabs(["Business", "Junior", "Triage", "Stakeholder", "Research",
                    "Governance", "Observability", "Config"])
    with tabs[0]:
        _business_context_tab(tenant)
    with tabs[1]:
        st.subheader("Junior maturity stage")
        st.json(_guarded(lambda: _client().junior_stage(tenant)) or {})
        jcfg = _guarded(lambda: _client().get_analyst_config(tenant)) or {}
        st.caption(f"Junior question depth: **{jcfg.get('depth_label', 'standard')}** "
                   f"({int(jcfg.get('junior_depth', 1))}/2)")
        st.subheader("Suggested questions (depth-scaled)")
        st.json(_guarded(lambda: _client().junior_questions(tenant)) or {})
        st.subheader("Business hypotheses (depth-scaled)")
        st.json(_guarded(lambda: _client().junior_hypotheses(tenant)) or {})
        st.subheader("Run next junior analysis (test drive)")
        jrun = st.columns([2, 1])
        jrun_force = jrun[0].checkbox(
            "Force (bypass 10–19h window + 1/hr rate for testing; serial + disable still hold)",
            value=True, key=f"jr_force_{tenant}")
        if jrun[1].button("▶ Run one", key=f"jr_run_{tenant}"):
            res = _guarded(lambda: _client().junior_run(tenant, force=jrun_force)) or {}
            st.session_state[f"jr_res_{tenant}"] = res
        if f"jr_res_{tenant}" in st.session_state:
            res = st.session_state[f"jr_res_{tenant}"]
            if res.get("ran"):
                st.success(f"Ran · {res.get('run_id')} · ok={res.get('ok')} "
                           f"rows={res.get('row_count')} insights={res.get('insights')}")
                st.caption((res.get("question") or "")[:160])
            else:
                st.warning(f"Did not run: {res.get('reason')}")
        st.subheader("Catalog (schema / EDA)")
        st.subheader("Catalog (schema / EDA)")
        st.json(_guarded(lambda: _client().junior_catalog(tenant)) or {})

    with tabs[2]:
        summary = _guarded(lambda: _client().triage_summary(tenant)) or {}
        if summary:
            m = st.columns(4)
            m[0].metric("Total nodes", summary.get("total", 0))
            m[1].metric("Actionable (needs review)", summary.get("actionable", 0))
            m[2].metric("Approved", summary.get("approved", 0))
            m[3].metric("Conflicts", summary.get("conflicts", 0))
        st.markdown("**Junior depth / senior mode**")
        sstatus = _guarded(lambda: _client().senior_status(tenant)) or {}
        scols = st.columns([2, 2, 2])
        scols[0].markdown(f"Senior AI: **{('ON' if sstatus.get('enabled') else 'OFF')}** — "
                          f"mode **{sstatus.get('mode', '?')}**")
        scols[1].markdown(f"Junior depth: **{sstatus.get('junior_depth_label', '?')}** "
                          f"({sstatus.get('junior_depth', '?')}/2)")
        scols[2].markdown(f"Human-signoff: **{sstatus.get('human_signoff_days', '?')} days**")
        jb = st.columns([1, 1])
        if jb[0].button("⬆ Promote junior", key="tri_depth_up"):
            _guarded(lambda: _client().senior_junior_depth(tenant, action="up", by="human"))
            st.rerun()
        if jb[1].button("⬇ Downgrade junior", key="tri_depth_dn"):
            _guarded(lambda: _client().senior_junior_depth(tenant, action="down", by="human"))
            st.rerun()

        with st.expander("Senior review inbox (analyst runs → .md for human review)"):
            runs = _guarded(lambda: _client().senior_queue(tenant, limit=50)) or []
            if not runs:
                st.caption("No completed analyses awaiting senior review yet.")
            for r in runs:
                st.markdown(f"**{r.get('review_status', '?')}** · `{r.get('run_id')}` — "
                            f"{(r.get('question') or '').strip()[:90]}")
                with st.container(border=True):
                    rcols = st.columns([3, 2, 2])
                    if rcols[0].button("Preview / save .md", key=f"md_{r.get('run_id')}"):
                        md = _guarded(lambda: _client().analysis_md(tenant, r.get("run_id")))
                        if md:
                            st.session_state[f"mdtext_{r.get('run_id')}"] = md
                    if rcols[1].button("Approve", key=f"appr_{r.get('run_id')}"):
                        res = _guarded(lambda: _client().senior_review(tenant, r.get("run_id"),
                                                                       action="approve", by="human"))
                        st.session_state[f"mdres_{r.get('run_id')}"] = res
                    if rcols[2].button("Reject", key=f"rej_{r.get('run_id')}"):
                        res = _guarded(lambda: _client().senior_review(tenant, r.get("run_id"),
                                                                       action="reject", by="human"))
                        st.session_state[f"mdres_{r.get('run_id')}"] = res
                    if f"mdtext_{r.get('run_id')}" in st.session_state:
                        st.code(st.session_state[f"mdtext_{r.get('run_id')}"].get("md", ""))
                    if f"mdres_{r.get('run_id')}" in st.session_state:
                        res = st.session_state[f"mdres_{r.get('run_id')}"]
                        st.success(res) if res.get("ok") else st.error(res.get("error", res))
        rt1, rt2, rt3 = st.tabs(["Definitions", "Queue review", "Conflicts"])
        with rt1:
            _definitions_review(tenant)
        with rt2:
            _queue_review(tenant)
        with rt3:
            _conflicts_review(tenant)

    with tabs[3]:
        _stakeholder_tab(tenant)
    with tabs[4]:
        _research_tab(tenant)
    with tabs[5]:
        _governance_tab(tenant)
    with tabs[6]:
        _observability_tab(tenant)
    with tabs[7]:
        _config_tab(tenant)
else:
    st.info("Select or create a tenant to begin.")