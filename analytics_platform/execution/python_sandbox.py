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


def _cap_structured_summary(value: Any) -> Any:
    """Enforce the same MAX_RESULT_CHARS ceiling on a structured (list/dict)
    summary that the scalar branch of _summarize already enforces. Row-count
    capping (head(MAX_RESULT_ROWS)) alone isn't enough -- a handful of rows
    with wide or long-text columns can still serialize to something far
    larger than MAX_RESULT_CHARS. Falls back to a truncated string, mirroring
    the scalar path's fallback when JSON serialization would exceed the cap."""
    import json
    try:
        text = json.dumps(value, default=str)
    except TypeError:
        text = str(value)
    if len(text) > MAX_RESULT_CHARS:
        return text[:MAX_RESULT_CHARS] + "...(truncated)"
    return value


def _summarize(value: Any):
    if isinstance(value, pd.DataFrame):
        head = value.head(MAX_RESULT_ROWS)
        records = head.to_dict(orient="records")
        shape = {"rows": len(value), "columns": len(value.columns)}
        return _cap_structured_summary(records), shape
    if isinstance(value, pd.Series):
        head = value.head(MAX_RESULT_ROWS)
        series_dict = head.to_dict()
        shape = {"rows": len(value), "columns": 1}
        return _cap_structured_summary(series_dict), shape
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

    try:
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
            if proc.is_alive():
                proc.kill()
                proc.join(2)
            return PythonExecResult(
                ok=False,
                error="sandboxed process terminated unexpectedly (likely a memory limit or crash)",
                execution_ms=(time.monotonic() - start) * 1000)

        proc.join(2)
        if proc.is_alive():
            proc.kill()
            proc.join(2)
        elapsed_ms = (time.monotonic() - start) * 1000
        # Same cap applies whether the child succeeded or errored -- printed
        # output (e.g. `print(df)` right before a raised exception) can carry
        # raw DataFrame content and must never cross this boundary uncapped.
        capped_stdout = stdout[-MAX_RESULT_CHARS:]

        if status == "error":
            # Same cap as stdout above -- a raised exception's message/traceback
            # can carry raw DataFrame content (e.g. `assert False, df.to_string()`)
            # and this string is fed verbatim into the next repair-loop prompt
            # and into logs, so it must never cross this boundary uncapped.
            capped_error = payload[-MAX_RESULT_CHARS:]
            return PythonExecResult(ok=False, error=capped_error, stdout=capped_stdout, execution_ms=elapsed_ms)

        summary, shape = _summarize(payload)
        return PythonExecResult(ok=True, result_summary=summary, result_shape=shape,
                                stdout=capped_stdout, execution_ms=elapsed_ms)
    finally:
        parent_conn.close()
