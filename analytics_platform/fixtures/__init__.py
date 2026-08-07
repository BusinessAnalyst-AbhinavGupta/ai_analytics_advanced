"""Synthetic "company" fixture for offline demo, tests, and the API.

Provides a small retail warehouse (pandas frames) that the SamplerExecutor can
execute real SQL against via duckdb, plus golden queries a tenant would approve
during onboarding. This makes the platform demonstrable with no live Metabase.
"""
from __future__ import annotations

import pandas as pd

WEEKLY_ORDER_SQL = """
SELECT date_format(CAST(created_at AS TIMESTAMP), '%Y-%m') AS month,
       COUNT(*)                                                  AS orders,
       COUNT_IF(status = 'completed')                             AS completed_orders,
       ROUND(100.0 * COUNT_IF(status = 'completed') / COUNT(*), 1) AS completion_pct
  FROM events
 WHERE action = 'order'
 GROUP BY 1
 ORDER BY 1
LIMIT 40
"""

FUNNEL_SQL = """
SELECT date_format(CAST(created_at AS TIMESTAMP), '%Y-%m') AS month,
       COUNT_IF(action = 'view')     AS views,
       COUNT_IF(action = 'add_to_cart') AS adds,
       COUNT_IF(action = 'order')    AS orders,
       ROUND(100.0 * COUNT_IF(action = 'order') / COUNT_IF(action = 'view'), 1) AS view_to_order_pct
  FROM events
 GROUP BY 1
 ORDER BY 1
LIMIT 40
"""

GOLDEN_QUERIES = [
    {"title": "Order completion rate by month",
     "summary": "Funnel % = completed / ALL orders (denominator counts all order events).",
     "sql": WEEKLY_ORDER_SQL},
    {"title": "Funnel view -> order conversion by month",
     "summary": "Conversion uses ALL views in the denominator.",
     "sql": FUNNEL_SQL},
]


def build_retail_warehouse(rows: int = 4000, seed: int = 42) -> dict:
    import numpy as np
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2026-01-01", periods=rows, freq="h")
    action = rng.choice(["view", "add_to_cart", "order"], size=rows, p=[0.6, 0.25, 0.15])
    status = np.where(action == "order",
                      np.where(rng.random(rows) < 0.6, "completed", "cancelled"),
                      "n/a")
    revenue = np.where((action == "order") & (status == "completed"),
                       rng.lognormal(mean=4.0, sigma=0.5, size=rows), 0.0)
    df = pd.DataFrame({
        "session_id": rng.choice(5000, size=rows, replace=True),
        "created_at": dates,
        "action": action,
        "status": status,
        "revenue": revenue.round(2),
        "region": rng.choice(["DE", "AT", "CH"], size=rows, p=[0.7, 0.2, 0.1]),
    })
    return {"events": df}


def make_company_doc(name: str = "Acme Retail GmbH") -> dict:
    return {
        "name": name,
        "industry": "ecommerce",
        "region": "DE",
        "description": "Online retailer of consumer electronics in DACH.",
        "customers": "Consumer households and small businesses.",
        "product": "Hardware and accessories sold via a web shop.",
        "value_creation": "Convenient one-stop ordering with fast fulfilment.",
        "revenue_model": "Product margin on each completed order.",
        "targets": [
            {"name": "Grow order volume", "category": "growth", "priority": 1,
             "owner": "Head of Commercial", "metric_refs": ["orders"]},
            {"name": "Raise funnel conversion", "category": "funnel", "priority": 2,
             "owner": "Head of CRO", "metric_refs": ["view_to_order_pct"]},
        ],
        "preferred_metrics": ["orders", "completion_pct", "view_to_order_pct"],
    }