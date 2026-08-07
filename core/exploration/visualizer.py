"""
Interactive Visual Exploration Canvas Adapter (PyGWalker Integration)
"""
import logging
from typing import Optional
import pandas as pd

logger = logging.getLogger(__name__)


class ExplorationVisualizer:
    """Generates PyGWalker HTML canvas for Streamlit embedding."""

    @staticmethod
    def generate_pygwalker_html(df: pd.DataFrame, theme_key: str = "dark") -> Optional[str]:
        if df is None or df.empty:
            return None
        try:
            import pygwalker as pyg
            # Sample large datasets for responsive interactive UI
            df_sample = df.sample(n=min(len(df), 50_000), random_state=42) if len(df) > 50_000 else df
            html_code = pyg.to_html(df_sample, dark=theme_key)
            return html_code
        except Exception as e:
            logger.warning(f"Failed to generate PyGWalker HTML: {e}")
            return None
