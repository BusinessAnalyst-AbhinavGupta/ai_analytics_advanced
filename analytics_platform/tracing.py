"""Ambient turn state, and the sink every trace record goes through.

Two contextvars carry what a wrapper cannot be handed directly. `_stage` is set
by the pipeline at boundaries it already emits step events for, so an LLM call
made during planning is labelled `planning` without the call site saying so.
`_sink` holds the tenant's store and this turn's trace id, because the client is
built by a factory that is given neither.

Everything here is best-effort by construction. Tracing is bolted onto the answer
path; a sink that can raise is a sink that can take a turn down, and an
observability feature is never worth an answer.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from typing import Any, Dict, Optional, Tuple

from .database import Store, dump_json
from .domain import now_iso

logger = logging.getLogger(__name__)

UNATTRIBUTED = "unattributed"
MAX_FIELD = 64_000

_stage: ContextVar[str] = ContextVar("turn_stage", default=UNATTRIBUTED)
_sink: ContextVar[Optional["TraceSink"]] = ContextVar("trace_sink", default=None)


def set_stage(stage: str) -> Token:
    return _stage.set(stage or UNATTRIBUTED)


def reset_stage(token: Token) -> None:
    _stage.reset(token)


def current_stage() -> str:
    return _stage.get()


def use_sink(sink: Optional["TraceSink"]) -> Token:
    return _sink.set(sink)


def reset_sink(token: Token) -> None:
    _sink.reset(token)


def current_sink() -> Optional["TraceSink"]:
    return _sink.get()


def clear_turn() -> None:
    """End-of-turn teardown. Sets both vars by value rather than resetting a
    Token, because a generator resumed in a different context cannot reset a
    token minted in the first one -- which is exactly what the SSE route does.
    """
    _sink.set(None)
    _stage.set(UNATTRIBUTED)


def record(kind: str, payload: Dict[str, Any], **kw: Any) -> None:
    """Record through the active sink, or do nothing. Never raises."""
    sink = _sink.get()
    if sink is None:
        return
    sink.record(kind, payload, **kw)


def clip(value: str, limit: int) -> Tuple[str, bool, int]:
    """(clipped, was_truncated, original_length)."""
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text, False, len(text)
    return text[:limit], True, len(text)


class TraceSink:
    """One turn's worth of trace rows, written to one tenant's database."""

    def __init__(self, store: Store, tenant_id: str, trace_id: str,
                 max_field: int = MAX_FIELD):
        self.store = store
        self.tenant_id = tenant_id
        self.trace_id = trace_id
        self.max_field = max_field
        self._seq = 0

    def _clip_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, value in payload.items():
            if not isinstance(value, str):
                out[key] = value
                continue
            text, truncated, length = clip(value, self.max_field)
            out[key] = text
            if truncated:
                out[f"{key}_truncated"] = True
                out[f"{key}_len"] = length
        return out

    def record(self, kind: str, payload: Dict[str, Any], *,
               duration_ms: float = 0.0, tokens_in: int = 0,
               tokens_out: int = 0, ok: bool = True) -> None:
        self._seq += 1
        try:
            self.store.execute(
                "INSERT INTO llm_traces (ts,tenant_id,trace_id,seq,stage,kind,"
                "payload,duration_ms,tokens_in,tokens_out,ok) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (now_iso(), self.tenant_id, self.trace_id, self._seq,
                 current_stage(), kind, dump_json(self._clip_payload(payload)),
                 float(duration_ms), int(tokens_in), int(tokens_out),
                 1 if ok else 0))
        except Exception as exc:  # noqa: BLE001 - a trace is never worth a turn
            logger.warning("trace record dropped (tenant=%s trace=%s kind=%s): %s",
                           self.tenant_id, self.trace_id, kind, exc)
