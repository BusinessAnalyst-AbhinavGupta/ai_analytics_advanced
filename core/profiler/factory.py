"""
Profiler Factory — Returns requested profiling engine.
"""
from typing import Optional
from core.profiler.base import BaseProfiler
from core.profiler.fast_summary import FastSummaryProfiler
from core.profiler.sweetviz_engine import SweetvizProfiler
from core.profiler.ydata_engine import YDataProfiler


class ProfilerFactory:
    """Creates and returns the appropriate profiling engine."""

    @staticmethod
    def get_profiler(engine_name: str = "fast_summary", output_dir: Optional[str] = None) -> BaseProfiler:
        engine_lower = engine_name.lower().strip()

        if engine_lower in ["sweetviz", "sv"]:
            return SweetvizProfiler(output_dir=output_dir) if output_dir else SweetvizProfiler()
        elif engine_lower in ["ydata", "ydata_profiling", "pandas_profiling"]:
            return YDataProfiler(output_dir=output_dir) if output_dir else YDataProfiler()
        else:
            return FastSummaryProfiler()
