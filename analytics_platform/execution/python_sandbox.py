"""Executes policy-approved Python cells in an isolated subprocess with
CPU/memory/wall-clock limits, returning only a small, JSON-safe summary of
`result` -- never the full DataFrame -- back to the caller. This is the
"local-compute-only" boundary in practice: the subprocess runs on the same
machine (nothing crosses the network), but its output crossing back into the
main process -- and from there, potentially into an LLM prompt or persisted
column -- is capped and summarized right here so nothing downstream has to
remember to truncate it.

Uses a fresh ("spawn") process per execution rather than fork, so resource
limits set inside the child don't inherit state from the parent's
already-running interpreter.
"""
from __future__ import annotations

import builtins
import contextlib
import io
import multiprocessing as mp
import time
import traceback
from dataclasses import dataclass
from typing import Any, Dict, Optional

import pandas as pd

DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MEMORY_MB = 512
MAX_RESULT_ROWS = 20
MAX_RESULT_CHARS = 4000

_ALLOWED_BUILTIN_NAMES = (
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int",
    "len", "list", "map", "max", "min", "range", "round", "set", "sorted", "str",
    "sum", "tuple", "zip", "print",
)
_SAFE_BUILTINS = {name: getattr(builtins, name) for name in _ALLOWED_BUILTIN_NAMES}


@dataclass
class PythonExecResult:
    ok: bool
    result_summary: Any = None
    result_shape: Optional[Dict[str, int]] = None
    stdout: str = ""
    error: str = ""
    execution_ms: float = 0.0


def _worker(code: str, dataframes: Dict[str, pd.DataFrame], memory_mb: int,
            timeout_s: float, conn) -> None:
    try:
        import resource
        cpu_limit = int(timeout_s) + 5
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
        resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024 * 1024, memory_mb * 1024 * 1024))
    except (ImportError, ValueError, OSError):
        pass  # best-effort: not all platforms support these rlimits

    scope: Dict[str, Any] = {"pd": pd, **dataframes}
    stdout_buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout_buf):
            exec(compile(code, "<python_cell>", "exec"), {"__builtins__": _SAFE_BUILTINS}, scope)
        if "result" not in scope:
            conn.send(("error", "code did not assign a 'result' variable", stdout_buf.getvalue()))
            return
        conn.send(("ok", scope["result"], stdout_buf.getvalue()))
    except Exception:
        conn.send(("error", traceback.format_exc(limit=3), stdout_buf.getvalue()))
    finally:
        conn.close()


def _summarize(value: Any):
    if isinstance(value, pd.DataFrame):
        head = value.head(MAX_RESULT_ROWS)
        return head.to_dict(orient="records"), {"rows": len(value), "columns": len(value.columns)}
    if isinstance(value, pd.Series):
        head = value.head(MAX_RESULT_ROWS)
        return head.to_dict(), {"rows": len(value), "columns": 1}
    try:
        import json
        text = json.dumps(value)
        if len(text) > MAX_RESULT_CHARS:
            return text[:MAX_RESULT_CHARS] + "...(truncated)", None
        return value, None
    except TypeError:
        return str(value)[:MAX_RESULT_CHARS], None


def run_python_sandboxed(code: str, dataframes: Dict[str, pd.DataFrame],
                          timeout_s: float = DEFAULT_TIMEOUT_S,
                          memory_mb: int = DEFAULT_MEMORY_MB) -> PythonExecResult:
    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_worker, args=(code, dataframes, memory_mb, timeout_s, child_conn))
    start = time.monotonic()
    proc.start()
    child_conn.close()  # only the child writes; parent must not hold this open

    if not parent_conn.poll(timeout_s):
        proc.terminate()
        proc.join(2)
        if proc.is_alive():
            proc.kill()
            proc.join(2)
        return PythonExecResult(ok=False, error=f"execution exceeded {timeout_s}s timeout",
                                execution_ms=(time.monotonic() - start) * 1000)

    try:
        status, payload, stdout = parent_conn.recv()
    except EOFError:
        proc.join(2)
        return PythonExecResult(
            ok=False,
            error="sandboxed process terminated unexpectedly (likely a memory limit or crash)",
            execution_ms=(time.monotonic() - start) * 1000)

    proc.join(2)
    elapsed_ms = (time.monotonic() - start) * 1000

    if status == "error":
        return PythonExecResult(ok=False, error=payload, stdout=stdout, execution_ms=elapsed_ms)

    summary, shape = _summarize(payload)
    return PythonExecResult(ok=True, result_summary=summary, result_shape=shape,
                            stdout=stdout[-MAX_RESULT_CHARS:], execution_ms=elapsed_ms)
