"""
EDA Engine — Generates standalone interactive HTML profiling reports
from pandas DataFrames using sweetviz.

sweetviz produces self-contained HTML files that can be rendered inline
in Streamlit via st.components.v1.html() or shared as standalone files.
"""
import os
import time
import logging
from typing import List, Dict, Optional
import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_REPORTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data", "eda_reports"
)


class EDAEngine:
    """Generates and manages EDA profiling reports."""

    def __init__(self, output_dir: str = DEFAULT_REPORTS_DIR):
        self.output_dir = os.path.abspath(output_dir)
        os.makedirs(self.output_dir, exist_ok=True)

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

    # ── Report Generation ─────────────────────────────────────────

    def generate_report(
        self,
        df: pd.DataFrame,
        title: str = "EDA Report",
        target_col: Optional[str] = None,
    ) -> str:
        """
        Generate a sweetviz HTML report and save it to disk.

        Parameters
        ----------
        df         : DataFrame to profile.
        title      : Human-readable title for the report.
        target_col : Optional target column for target-aware analysis.

        Returns
        -------
        Absolute path to the generated HTML file.
        """
        import sweetviz as sv

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        safe_name = self._sanitize(title)
        filename = f"{safe_name}_{timestamp}.html"
        output_path = os.path.join(self.output_dir, filename)

        logger.info(f"Generating sweetviz report: {output_path}")

        # Cap DataFrame at 50k rows for performance (sweetviz can be slow on large data)
        if len(df) > 50_000:
            logger.info(f"Sampling 50,000 rows from {len(df):,} for profiling performance.")
            df_sample = df.sample(n=50_000, random_state=42)
        else:
            df_sample = df

        if target_col and target_col in df_sample.columns:
            report = sv.analyze(df_sample, target_feat=target_col)
        else:
            report = sv.analyze(df_sample)

        report.show_html(
            filepath=output_path,
            open_browser=False,
            layout="widescreen",
        )

        logger.info(f"✅ Report saved: {output_path} ({os.path.getsize(output_path) / 1024:.0f} KB)")
        return output_path

    # ── Report Management ─────────────────────────────────────────

    def list_saved_reports(self) -> List[Dict]:
        """Returns metadata for all saved HTML reports, newest first."""
        reports = []
        if not os.path.isdir(self.output_dir):
            return reports

        for fname in os.listdir(self.output_dir):
            if fname.endswith(".html"):
                fpath = os.path.join(self.output_dir, fname)
                stat = os.stat(fpath)
                reports.append({
                    "name": fname,
                    "path": fpath,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "created": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(stat.st_ctime)
                    ),
                })

        reports.sort(key=lambda r: r["created"], reverse=True)
        return reports

    def load_report_html(self, path: str) -> str:
        """Read and return the full HTML string of a saved report."""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
