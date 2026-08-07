"""Per-analysis markdown rendering (R3).

Every junior analysis can be rendered as a standalone `.md` file for review by a
human. The file is scoped per tenant under the configured `reviews_dir` and carries
the question, the SQL, facts, hypotheses, and the current review state so a human
can review offline. We only ever render from the governed `AnalysisRun` — the same
data the senior inbox reads — so the markdown is always an exact view of the DB.
"""
from __future__ import annotations

import os
from typing import Any

from .domain import AnalysisRun, ReviewStatus


def _li(items: Any) -> str:
    if not items:
        return "_none_"
    return "\n".join(f"- {i}" for i in items)


def _li_insights(insights: Any) -> str:
    if not insights:
        return "_none_"
    lines = []
    for ins in insights:
        if isinstance(ins, dict):
            tag = "🔍 novel" if ins.get("novel") else "already covered"
            lines.append(f"- **[{tag}]** {ins.get('text', '')}")
        else:
            lines.append(f"- {ins}")
    return "\n".join(lines)


def render_analysis_md(run: AnalysisRun) -> str:
    """Render an AnalysisRun as markdown for human review."""
    review = (run.review_status.value if isinstance(run.review_status, ReviewStatus)
              else str(run.review_status or ReviewStatus.CANDIDATE.value))
    status = run.status.value if hasattr(run.status, "value") else str(run.status)
    return "\n".join([
        f"# Analysis · `{run.id}`",
        "",
        f"- **Question:** {run.question_text}",
        f"- **Status:** {status} · **Review:** {review}",
        f"- **Generated:** {run.generated_at}",
        f"- **Row count:** {run.row_count} · **Execution ms:** {run.execution_ms}",
        "",
        "## SQL",
        "",
        "```sql",
        run.sql,
        "```",
        "",
        "## Answer summary",
        "",
        run.answer or "_no summary_",
        "",
        "## Insights",
        "",
        _li_insights(run.insights),
        "",
        "## Assumptions",
        "",
        _li(run.assumptions),
        "",
        "## Actionable recommendations",
        "",
        _li(run.next_actions),
        "",
        "## Facts",
        "",
        _li(run.facts),
        "",
        "## Hypotheses",
        "",
        _li(run.hypotheses),
        "",
        "## Uncertainties",
        "",
        _li(run.uncertainties),
        "",
    ])


def write_analysis_md(run: AnalysisRun, base_dir: str) -> str:
    """Persist the markdown under ``base_dir/<tenant_id>/<run_id>.md`` and return
    the path. Missing directories are created; best-effort (never raises)."""
    try:
        directory = os.path.join(base_dir, run.tenant_id)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{run.id}.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(render_analysis_md(run))
        return path
    except Exception:  # noqa: BLE001 - persisting a file must never break the flow
        return ""


__all__ = ["render_analysis_md", "write_analysis_md"]