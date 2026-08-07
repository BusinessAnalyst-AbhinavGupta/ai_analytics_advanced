"""
scratch/test_copilot_e2e.py
=============================
Offline End-to-End harness for the Copilot (NO Metabase, NO Neo4j, NO live LLM).

Simulates the full loop: SQL gen -> execute -> profile -> rules -> senior briefing
-> anomaly auto-correction (BUSINESS_LOGIC) -> re-execute -> re-verify.

Reuses the REAL `FastSummaryProfiler`, `BusinessRuleEngine`, and
`ProductAnalystAgent.get_critical_anomalies()`. Only the three external-touching
edges are stubbed deterministically:
  1. SQL generation    -> returns a deliberately-wrong v1 (denominator = completed only)
  2. Metabase execution -> returns a DataFrame consistent with the given SQL
  3. AutoHealer        -> rewrites the denominator to count ALL initiated events

This is the P0 regression harness referenced in STANDALONE_ANALYTICS_PLATFORM_PLAN.md
(section 11, P0; section 14). Run:
    .venv/bin/python scratch/test_copilot_e2e.py
"""
import sys
import time

sys.path.insert(0, ".")  # allow `core.*` imports from repo root

import pandas as pd

from core.profiler.fast_summary import FastSummaryProfiler
from core.rules.engine import BusinessRuleEngine
from core.reasoning.analyst import ProductAnalystAgent

# --------------------------------------------------------------------------- #
# Forward model: the "business truth" this harness knows.
#   100 initiated sessions; 40 completed  -> true completion rate = 40%.
# --------------------------------------------------------------------------- #
BAD_SQL = (
    "-- v1 (BUG): denominator filtered to completed sessions only\n"
    "SELECT date_format(CAST(log_time AS TIMESTAMP), '%Y-%m') AS month,\n"
    "       COUNT(*) AS completed_total,\n"
    "       ROUND(100.0 * COUNT(*) / COUNT(*), 1) AS completed_pct\n"
    "  FROM event_table\n"
    " WHERE action = 'appointmentCompleted'\n"
    " GROUP BY 1 ORDER BY 1"
)
GOOD_SQL = (
    "-- v2 (healed): denominator counts ALL initiated events (funnel-correct)\n"
    "SELECT date_format(CAST(log_time AS TIMESTAMP), '%Y-%m') AS month,\n"
    "       COUNT_IF(action = 'appointmentCompleted') AS completed_total,\n"
    "       ROUND(100.0 * COUNT_IF(action = 'appointmentCompleted') / COUNT(*), 1) AS completed_pct\n"
    "  FROM event_table\n"
    " WHERE action IN ('appointmentView','appointmentBooked','appointmentCompleted')\n"
    " GROUP BY 1 ORDER BY 1"
)


def mock_generate_sql(question: str, version: int) -> str:
    """Pretend QueryGenerator produced a wrong v1 as a first draft."""
    return BAD_SQL if version == 1 else GOOD_SQL


def mock_execute_sql(sql: str) -> pd.DataFrame:
    """Pretend Metabase execution returns a result set for `sql`.

    v1 (bad): only completed sessions come back  -> 100% conversion.
    v2 (good): all initiated come back           -> ~40% conversion.
    """
    initiated = [40, 40]            # initiated sessions per month (ground truth)
    completed = [22, 18]            # completed per month (ground truth)
    if "appointmentView" in sql:    # healed SQL counts all initiated events
        completed_total, totals = completed, initiated
        pct = [round(100.0 * c / t, 1) for c, t in zip(completed, totals)]
    else:                           # bad SQL only sees completed rows
        completed_total = completed
        pct = [100.0, 100.0]
    return pd.DataFrame({"month": ["2026-06", "2026-07"],
                         "completed_total": completed_total,
                         "completed_pct": pct})


class StubBusinessLogicHealer:
    """Stand-in for AutoHealer.diagnose_and_heal(feedback_type='BUSINESS_LOGIC')."""

    def diagnose_and_heal(self, failed_sql, error_message, **kwargs) -> dict:
        return {
            "healed_sql": GOOD_SQL,
            "root_cause": "SQL denominator filtered to completed sessions only",
            "what_changed": "Denominator now counts ALL initiated events; COUNT_IF numerator added.",
            "rule_text": "Rule: funnel % = completed / ALL initiated, never completed / completed.",
            "total_iterations": 1,
            "history": [],
        }


def build_briefing(profiler_res, rule_res, sql: str) -> dict:
    """Build the dict shape ProductAnalystAgent.analyze_results() would return.

    The senior analyst interprets a 'funnel completed == total' trigger (i.e. a
    conversion rate pinned at 100%) as a CRITICAL business-logic error and
    elevates the rule's WARNING severity to CRITICAL.
    """
    anomaly_diagnostics = []
    for obs in getattr(rule_res, "observations", []) or []:
        sev = (obs.severity.value if hasattr(obs.severity, "value") else str(obs.severity)).upper()
        elevated = "CRITICAL" if sev == "WARNING" else sev  # analyst elevation
        title = obs.rule_name if obs.rule_name.rstrip().lower().endswith("anomaly") \
            else f"{obs.rule_name} Anomaly"
        anomaly_diagnostics.append(
            {
                "severity": elevated,
                "title": title,
                "observed_pattern": obs.observation_text,
                "plausible_hypotheses": list(obs.hypotheses),
                "recommended_verification": "; ".join(obs.recommended_checks),
            }
        )
    return {
        "question": "show me appointment view and completed % MoM",
        "sql_used": sql,
        "row_count": profiler_res.row_count,
        "summary": f"{profiler_res.row_count} rows profiled; {rule_res.triggered_count} rule(s) triggered.",
        "anomaly_diagnostics": anomaly_diagnostics,
    }

def main() -> None:
    print("=" * 72)
    print("COPILOT OFFLINE E2E  (no Metabase / Neo4j / live LLM)")
    print("=" * 72)

    question = "show me appointment view and completed % MoM"

    t0 = time.time()

    # 1. Generate v1 SQL (deliberately wrong)
    sql = mock_generate_sql(question, version=1)
    print(f"\n[1] Generated v1 SQL | denom-bug present: {'appointmentView' not in sql}")

    # 2. Execute on (mock) Metabase -> bad results
    df = mock_execute_sql(sql)
    print(f"[2] Executed on mock Metabase -> {len(df)} rows | pct={df['completed_pct'].tolist()}")

    # 3. Profile + business rules -> anomaly
    profiler = FastSummaryProfiler()
    rule_engine = BusinessRuleEngine()
    profiler_res = profiler.profile(df)
    rule_res = rule_engine.evaluate(df)
    print(f"[3] Profiled rows={profiler_res.row_count} | rules triggered={rule_res.triggered_count}")

    # 4. Senior analyst briefing -> critical anomalies (REAL method)
    briefing = build_briefing(profiler_res, rule_res, sql)
    critical = ProductAnalystAgent.get_critical_anomalies(briefing)
    print(f"[4] Critical anomalies detected = {len(critical)}")
    for c in critical:
        print(f"      - {c['title']} [{c['severity']}] | {c['observed_pattern'][:90]}")
    assert critical, "E2E FAIL: expected a CRITICAL anomaly on the bad v1 SQL"

    combined_feedback = "\n\n".join(a["feedback_text"] for a in critical)

    # 5. AutoHealer (BUSINESS_LOGIC) heals the SQL
    healer = StubBusinessLogicHealer()
    ah = healer.diagnose_and_heal(failed_sql=sql, error_message=combined_feedback,
                                  feedback_type="BUSINESS_LOGIC", question=question)
    healed_sql = ah["healed_sql"]
    print(f"[5] AutoHeal (BUSINESS_LOGIC) | root: {ah['root_cause']}")
    print(f"    rule learned: {ah['rule_text']}")

    # 6. Re-execute healed SQL + re-verify round-trip
    df2 = mock_execute_sql(healed_sql)
    p2 = profiler.profile(df2)
    r2 = rule_engine.evaluate(df2)
    b2 = build_briefing(p2, r2, healed_sql)
    crit2 = ProductAnalystAgent.get_critical_anomalies(b2)
    print(f"[6] Re-executed healed SQL -> rows={p2.row_count} rules={r2.triggered_count} critical={len(crit2)}")
    print(f"    healed pct: {df2['completed_pct'].round(1).tolist()}")
    assert crit2 == [], "E2E FAIL: healed SQL still produces a CRITICAL anomaly"

    elapsed = round(time.time() - t0, 2)
    print("\n" + "=" * 72)
    print(f"PASS: wrong-SQL -> detect -> heal -> re-verify round-trip OK ({elapsed}s)")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())