"""
Fast Vectorized Statistical Summarizer (<100ms)
Produces structured, LLM-optimized JSON context from DataFrames.
"""
import time
import json
from typing import Optional, Dict, Any, List
import pandas as pd
import numpy as np

from core.profiler.base import BaseProfiler, ProfilerResult


class FastSummaryProfiler(BaseProfiler):
    """Generates instant vectorized summary stats and structured JSON for LLMs."""

    def profile(
        self,
        df: pd.DataFrame,
        title: str = "Dataset Profile",
        target_col: Optional[str] = None,
        generate_html: bool = False
    ) -> ProfilerResult:
        t0 = time.time()

        if df is None or df.empty:
            return ProfilerResult(
                engine_name="fast_summary",
                row_count=0,
                col_count=0,
                summary_dict={"empty_dataset": True},
                summary_json_str=json.dumps({"empty_dataset": True}),
                duration_seconds=round(time.time() - t0, 3),
                quality_warnings=["Dataset is empty (0 rows)."]
            )

        n_rows = len(df)
        n_cols = len(df.columns)
        mem_mb = round(df.memory_usage(deep=True).sum() / (1024 * 1024), 2)
        dup_rows = int(df.duplicated().sum())
        dup_pct = round((dup_rows / n_rows) * 100, 2) if n_rows > 0 else 0.0

        quality_warnings: List[str] = []
        if dup_pct > 0.0:
            quality_warnings.append(f"{dup_rows:,} exact duplicate row(s) ({dup_pct}%) detected.")

        column_summaries: Dict[str, Any] = {}

        for col in df.columns:
            series = df[col]
            null_count = int(series.isnull().sum())
            null_pct = round((null_count / n_rows) * 100, 2) if n_rows > 0 else 0.0
            unique_count = int(series.nunique(dropna=True))
            cardinality_ratio = round(unique_count / n_rows, 4) if n_rows > 0 else 0.0

            col_meta: Dict[str, Any] = {
                "dtype": str(series.dtype),
                "null_count": null_count,
                "null_pct": null_pct,
                "unique_count": unique_count,
                "cardinality_ratio": cardinality_ratio
            }

            if null_pct == 100.0:
                quality_warnings.append(f"Column '{col}' is 100% NULL.")
                column_summaries[col] = col_meta
                continue
            elif null_pct >= 30.0:
                quality_warnings.append(f"Column '{col}' has high NULL rate ({null_pct}%).")

            if unique_count == 1:
                quality_warnings.append(f"Column '{col}' is constant (single unique value).")

            # Numeric Analysis
            if pd.api.types.is_numeric_dtype(series):
                clean_num = series.dropna()
                if not clean_num.empty:
                    c_min = float(clean_num.min())
                    c_max = float(clean_num.max())
                    c_mean = round(float(clean_num.mean()), 3)
                    c_median = round(float(clean_num.median()), 3)
                    c_std = round(float(clean_num.std()), 3) if len(clean_num) > 1 else 0.0
                    neg_count = int((clean_num < 0).sum())
                    zero_count = int((clean_num == 0).sum())

                    col_meta.update({
                        "kind": "numeric",
                        "min": c_min,
                        "max": c_max,
                        "mean": c_mean,
                        "median": c_median,
                        "std": c_std,
                        "negative_count": neg_count,
                        "zero_count": zero_count
                    })

                    if neg_count > 0 and any(kw in col.lower() for kw in ["count", "views", "orders", "price", "revenue", "rate", "pct"]):
                        quality_warnings.append(f"Numeric column '{col}' contains {neg_count} negative value(s).")

            # DateTime / Temporal Analysis
            elif pd.api.types.is_datetime64_any_dtype(series):
                clean_dt = series.dropna()
                if not clean_dt.empty:
                    col_meta.update({
                        "kind": "datetime",
                        "min_timestamp": str(clean_dt.min()),
                        "max_timestamp": str(clean_dt.max()),
                        "timespan_days": round((clean_dt.max() - clean_dt.min()).total_seconds() / 86400, 1)
                    })

            # Categorical / String Analysis
            else:
                clean_cat = series.dropna().astype(str)
                if not clean_cat.empty:
                    top_vals = clean_cat.value_counts().head(5).to_dict()
                    top_summary = {str(k): int(v) for k, v in top_vals.items()}
                    col_meta.update({
                        "kind": "categorical",
                        "top_values": top_summary
                    })

            column_summaries[col] = col_meta

        # Sample rows for context
        sample_records = df.head(5).to_dict(orient="records")
        # Ensure json serializable
        clean_sample = json.loads(json.dumps(sample_records, default=str))

        summary_dict = {
            "title": title,
            "dimensions": {
                "row_count": n_rows,
                "col_count": n_cols,
                "memory_mb": mem_mb,
                "duplicate_rows": dup_rows,
                "duplicate_pct": dup_pct
            },
            "columns": column_summaries,
            "sample_rows": clean_sample,
            "quality_warnings": quality_warnings
        }

        duration = round(time.time() - t0, 3)
        summary_json_str = json.dumps(summary_dict, indent=2, default=str)

        return ProfilerResult(
            engine_name="fast_summary",
            row_count=n_rows,
            col_count=n_cols,
            summary_dict=summary_dict,
            summary_json_str=summary_json_str,
            html_report_path=None,
            html_content=None,
            duration_seconds=duration,
            quality_warnings=quality_warnings
        )
