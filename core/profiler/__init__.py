"""
Modular Profiling Engine Interface & Adapters
"""
from core.profiler.base import BaseProfiler, ProfilerResult
from core.profiler.fast_summary import FastSummaryProfiler

__all__ = ["BaseProfiler", "ProfilerResult", "FastSummaryProfiler"]
