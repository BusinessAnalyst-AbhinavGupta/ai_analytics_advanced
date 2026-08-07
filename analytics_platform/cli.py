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
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())