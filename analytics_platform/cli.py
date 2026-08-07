"""CLI for the standalone analytics platform.

Examples:
    .venv/bin/python -m analytics_platform.cli demo
    .venv/bin/python -m analytics_platform.serve --port 8000   # run FastAPI
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

from .api import bootstrap_demo, make_context


def _print(label: str, obj: Any) -> None:
    print(f"\n--- {label} ---")
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, indent=2, default=str))
    else:
        print(obj)


def cmd_demo(args: argparse.Namespace) -> int:
    ctx = make_context()
    tenant_id = bootstrap_demo(ctx)
    print(f"Bootstrapped demo tenant: {tenant_id}")

    # 1) Direct-from-approved-knowledge path
    run1 = ctx.pipeline.run(tenant_id, "Order completion rate by month")
    _print("Question 1 (approved-query reuse)", {
        "answer_mode": run1.answer_mode.value if run1.answer_mode else None,
        "status": run1.status.value,
        "sql_used": run1.sql,
        "facts": run1.facts,
        "answer": run1.answer,
        "review_status": run1.review_status.value,
    })

    # 2) Novel question -> requires senior review
    run2 = ctx.pipeline.run(tenant_id, "Average revenue per completed order by region",
                            persisted_sql=(
                                "SELECT region, AVG(revenue) AS avg_revenue "
                                "FROM events WHERE action='order' AND status='completed' "
                                "GROUP BY 1 ORDER BY 2 DESC LIMIT 20"))
    _print("Question 2 (novel analysis -> requires review)", {
        "answer_mode": run2.answer_mode.value if run2.answer_mode else None,
        "status": run2.status.value,
        "answers": run2.answer,
        "row_count": run2.row_count,
    })

    # promote the novel finding (senior review)
    node = ctx.pipeline.promote_finding(tenant_id, run2.id, by="senior",
                                        notes="Verified avg revenue per region is actionable.")
    if node:
        _print("Senior promotion", {"node": node.id, "status": node.status.value,
                                     "kind": node.kind.value})

    # 3) Policy guard: DML must be blocked
    run3 = ctx.pipeline.run(tenant_id, "try to delete data",
                            persisted_sql="DELETE FROM events WHERE 1=1")
    _print("Question 3 (DML -> policy blocked)", {
        "status": run3.status.value,
        "policy_reasons": run3.policy_reasons,
    })

    _print("Brain stats", ctx.pipeline.brain(tenant_id).stats())
    _print("Metrics", ctx.observability.metrics(tenant_id))
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    ctx = make_context()
    try:
        run = ctx.pipeline.run(args.tenant_id, args.question)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _print("Run", run.to_dict())
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    ctx = make_context()
    if args.tenant_id:
        _print(f"Tenant {args.tenant_id} metrics", {
            "telemetry": ctx.observability.metrics(args.tenant_id),
            "brain": ctx.pipeline.brain(args.tenant_id).stats()})
    else:
        _print("Platform metrics", ctx.observability.metrics())
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from .migration import migrate_from_snapshot
    ctx = make_context()
    try:
        brain = ctx.pipeline.brain(args.tenant_id)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    summary = migrate_from_snapshot(
        brain, args.snapshot,
        derive_definitions=not args.no_derive_definitions,
        created_by=args.created_by)
    _print(f"Migration summary (tenant {args.tenant_id})", summary)
    _print("Brain stats", brain.stats())
    return 0


def cmd_browser(args: argparse.Namespace) -> int:
    from .execution.browser_session import make_live_executor
    ex = make_live_executor()
    if args.database_id is not None:
        ex.config.database_id = args.database_id
    if args.expected_host:
        ex.config.expected_host = args.expected_host
    if args.host:
        ex.base_url = args.host.rstrip("/")
    _print("Live Metabase executor", {
        "database_id": ex.config.database_id,
        "expected_host": ex.config.expected_host,
        "base_url": ex.base_url})

    status = ex.session_status(tenant_id=args.tenant_id or "")
    _print("Session status", {
        "state": status.state, "browser_ok": status.browser_ok,
        "detail": status.detail})
    if status.state != "valid":
        print("Aborting: session is not usable; no query executed. "
              "Log into Metabase in Chrome (active tab) and retry.", file=sys.stderr)
        return 1
    if not args.sql:
        print("Session OK. Pass --sql to run a read-only query.")
        return 0

    from .execution.base import ExecutionContext
    ctx = ExecutionContext(tenant_id=args.tenant_id or "", dialect="athena")
    r = ex.execute(args.sql, ctx)
    if not r.ok:
        _print("Query failed", {"ok": False, "error": r.error})
        return 1
    head = r.data.head(args.head).to_dict("records") if r.data is not None else []
    _print("Query result", {"ok": True, "row_count": r.row_count,
                            "columns": r.columns, "head": head})
    return 0


def _parse_kind(raw: str):
    from .domain import NodeKind
    if not raw:
        return None
    return NodeKind(raw.upper())


def cmd_review(args: argparse.Namespace) -> int:
    ctx = make_context()
    from .triage import TriageService
    try:
        ctx.tenants.require_tenant(args.tenant_id)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    svc = TriageService(ctx.store, ctx.observability)
    kind = _parse_kind(args.kind)

    if args.approve:
        ids = [s.strip() for s in args.approve.split(",") if s.strip()]
        _print("Triage approve", svc.approve(args.tenant_id, ids, by=args.by, notes=args.notes))
    if args.reject:
        ids = [s.strip() for s in args.reject.split(",") if s.strip()]
        _print("Triage reject", svc.reject(args.tenant_id, ids, by=args.by, notes=args.notes))
    if args.bulk_approve:
        _print("Triage bulk approve", svc.bulk(args.tenant_id, kind=kind,
                                               action="approve", by=args.by))
    if args.bulk_reject:
        _print("Triage bulk reject", svc.bulk(args.tenant_id, kind=kind,
                                              action="reject", by=args.by))

    if args.conflicts:
        conflicts = svc.conflicts(args.tenant_id)
        _print(f"Conflicts ({len(conflicts)})", conflicts[:args.conflict_limit])
        if not (args.approve or args.reject or args.bulk_approve or args.bulk_reject):
            return 0
    if not args.quiet:
        _print("Triage summary", svc.summary(args.tenant_id))
        queue = svc.queue(args.tenant_id, kind=kind, limit=args.limit)
        _print(f"Queue ({len(queue)}): {args.kind or 'all kinds'}",
               [{"id": n.id, "kind": n.kind.value, "status": n.status.value,
                 "title": n.title} for n in queue])
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="analytics-platform")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("demo", help="Run the offline synthetic-company demo end-to-end")
    d.set_defaults(func=cmd_demo)
    a = sub.add_parser("ask", help="Ask a question for an existing tenant")
    a.add_argument("tenant_id")
    a.add_argument("question")
    a.set_defaults(func=cmd_ask)
    m = sub.add_parser("metrics", help="Show telemetry metrics")
    m.add_argument("--tenant-id", default="")
    m.set_defaults(func=cmd_metrics)
    mg = sub.add_parser("migrate", help="Import a knowledge-graph snapshot into a tenant's Brain")
    mg.add_argument("tenant_id")
    mg.add_argument("--snapshot", required=True,
                    help="path to knowledge_graph_snapshot.json")
    mg.add_argument("--no-derive-definitions", action="store_true",
                    help="skip deriving DEFINITION nodes from query filters")
    mg.add_argument("--created-by", default="migration")
    mg.set_defaults(func=cmd_migrate)
    br = sub.add_parser("browser", help="Live Metabase via the open Chrome tab "
                                        "(uses ANALYTICS_MB_* env; read-only)")
    br.add_argument("--tenant-id", default="", help="tenant id (informational scoping)")
    br.add_argument("--sql", default="", help="read-only SQL to run (omit to just check the session)")
    br.add_argument("--database-id", default=None, help="override Metabase database id")
    br.add_argument("--expected-host", default="", help="override expected Metabase hostname")
    br.add_argument("--host", default="", help="override Metabase base URL")
    br.add_argument("--head", type=int, default=10, help="rows to print from the result")
    br.set_defaults(func=cmd_browser)
    rv = sub.add_parser("review", help="Triage/senior-review the Brain's CANDIDATE nodes")
    rv.add_argument("tenant_id")
    rv.add_argument("--kind", default="", help="NodeKind filter (QUERY|DEFINITION|IDIOM|BUSINESS_RULE|...)")
    rv.add_argument("--limit", type=int, default=50, help="queue rows to show")
    rv.add_argument("--approve", default="", help="comma-separated node ids to approve")
    rv.add_argument("--reject", default="", help="comma-separated node ids to reject")
    rv.add_argument("--bulk-approve", action="store_true", help="approve all actionable of --kind")
    rv.add_argument("--bulk-reject", action="store_true", help="reject all actionable of --kind")
    rv.add_argument("--by", default="senior", help="reviewer identity")
    rv.add_argument("--notes", default="")
    rv.add_argument("--conflicts", action="store_true", help="show title-conflict candidates")
    rv.add_argument("--conflict-limit", type=int, default=20)
    rv.add_argument("--quiet", action="store_true", help="suppress summary/queue output")
    rv.set_defaults(func=cmd_review)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())