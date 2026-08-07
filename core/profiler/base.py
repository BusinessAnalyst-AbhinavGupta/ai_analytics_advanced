"""
Base Profiler Interface & Data Models
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List
import pandas as pd


@dataclass
class ProfilerResult:
    """Standardized output container across all profiling engines."""
    engine_name: str
    row_count: int
    col_count: int
    summary_dict: Dict[str, Any] = field(default_factory=dict)
    summary_json_str: str = ""
    html_report_path: Optional[str] = None
    html_content: Optional[str] = None
    duration_seconds: float = 0.0
    quality_warnings: List[str] = field(default_factory=list)


class BaseProfiler(ABC):
    """Abstract interface that all profiling engines must implement."""

    @abstractmethod
    def profile(
        self,
        df: pd.DataFrame,
        title: str = "Dataset Profile",
        target_col: Optional[str] = None,
        generate_html: bool = False
    ) -> ProfilerResult:
        """
        Profile a DataFrame and return a ProfilerResult.

        Parameters
        ----------
        df            : pd.DataFrame to profile
        title         : Title for reports
        target_col    : Optional target feature for supervised association analysis
        generate_html : Whether to generate interactive HTML report file

        Returns
        -------
        ProfilerResult with structured statistics and optional HTML path.
        """
        pass
