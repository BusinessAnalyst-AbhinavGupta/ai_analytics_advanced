"""
Sweetviz Profiling Engine Adapter
"""
import os
import time
import logging
from typing import Optional
import pandas as pd

from core.profiler.base import BaseProfiler, ProfilerResult
from core.profiler.fast_summary import FastSummaryProfiler

logger = logging.getLogger(__name__)
DEFAULT_REPORTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "eda_reports")


class SweetvizProfiler(BaseProfiler):
    """Generates sweetviz interactive standalone HTML reports."""

    def __init__(self, output_dir: str = DEFAULT_REPORTS_DIR):
        self.output_dir = os.path.abspath(output_dir)
        os.makedirs(self.output_dir, exist_ok=True)
        self.fast_profiler = FastSummaryProfiler()

    @staticmethod
    def _sanitize(name: str) -> str:
        return (
            name.replace(" ", "_")
            .replace("/", "_")
            .replace("\\", "_")
            .replace("'", "")
            .replace('"', "")
            .replace("?", "")
            [:80]
            .strip("_")
            .lower()
        )

    def profile(
        self,
        df: pd.DataFrame,
        title: str = "Dataset Profile",
        target_col: Optional[str] = None,
        generate_html: bool = True
    ) -> ProfilerResult:
        t0 = time.time()
        # First compute fast in-memory statistics
        base_res = self.fast_profiler.profile(df, title=title, target_col=target_col)

        html_path = None
        html_content = None

        if generate_html and df is not None and not df.empty:
            try:
                import sweetviz as sv
                timestamp = time.strftime("%Y%m%d_%H%M%S")
                safe_name = self._sanitize(title)
                filename = f"sweetviz_{safe_name}_{timestamp}.html"
                html_path = os.path.join(self.output_dir, filename)

                df_sample = df.sample(n=min(len(df), 50_000), random_state=42) if len(df) > 50_000 else df

                if target_col and target_col in df_sample.columns:
                    report = sv.analyze(df_sample, target_feat=target_col)
                else:
                    report = sv.analyze(df_sample)

                report.show_html(filepath=html_path, open_browser=False, layout="widescreen")
                
                if os.path.exists(html_path):
                    with open(html_path, "r", encoding="utf-8") as f:
                        html_content = f.read()
            except Exception as e:
                logger.warning(f"Sweetviz generation failed: {e}")
                base_res.quality_warnings.append(f"Sweetviz HTML report error: {e}")

        total_duration = round(time.time() - t0, 3)

        return ProfilerResult(
            engine_name="sweetviz",
            row_count=base_res.row_count,
            col_count=base_res.col_count,
            summary_dict=base_res.summary_dict,
            summary_json_str=base_res.summary_json_str,
            html_report_path=html_path,
            html_content=html_content,
            duration_seconds=total_duration,
            quality_warnings=base_res.quality_warnings
        )
