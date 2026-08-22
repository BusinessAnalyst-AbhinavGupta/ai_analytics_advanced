"""The LLM boundary, observed.

Wrapping the client rather than the seven call sites is the whole design: a call
site can be forgotten, and it will be forgotten by the person adding the eighth
call, which is discovered exactly when the trace is needed. A wrapper has one
code path and no opt-out.

It is applied at the point of *use* rather than inside `make_role_client`,
because the flow tests swap that factory out wholesale -- a wrapper living inside
it would be swapped out along with it, and the feature would ship untested.

The response is returned before anything is recorded, so a recorder that fails
cannot swallow a good answer.
"""
from __future__ import annotations

from time import perf_counter
from typing import Any, Optional

from .. import tracing
from .client import LLMResponse


class TracingLLMClient:
    """Wraps an LLMClient and records each call through the ambient sink."""

    def __init__(self, inner: Any):
        self.inner = inner

    # The inner client's own attributes stay readable through the wrapper, so
    # code that sniffs `client.name` (`_llm_live` does) still works.
    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)

    def generate(self, prompt: str, system_prompt: str = "", *,
                 temperature: float = 0.0, **kwargs: Any) -> LLMResponse:
        t0 = perf_counter()
        response: Optional[LLMResponse] = None
        error = ""
        try:
            # Forwarded by keyword because that is how every call site invokes
            # it, and test doubles inspect kwargs. Passing positionally here
            # silently changed the shape of every recorded call.
            response = self.inner.generate(prompt=prompt,
                                           system_prompt=system_prompt,
                                           temperature=temperature, **kwargs)
            return response
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._record(prompt, system_prompt, temperature, response, error,
                         (perf_counter() - t0) * 1000.0)

    def _record(self, prompt: str, system_prompt: str, temperature: float,
                response: Optional[LLMResponse], error: str,
                duration_ms: float) -> None:
        ok = bool(response is not None and getattr(response, "ok", True) and not error)
        payload = {
            "prompt": prompt or "",
            "system_prompt": system_prompt or "",
            "response_text": getattr(response, "text", "") or "",
            "provider": getattr(response, "provider", "") or "",
            "model": getattr(response, "model", "") or "",
            "temperature": temperature,
            "ok": ok,
        }
        if error:
            payload["error"] = error
        tracing.record("llm", payload, duration_ms=duration_ms,
                       tokens_in=getattr(response, "tokens_in", 0) or 0,
                       tokens_out=getattr(response, "tokens_out", 0) or 0, ok=ok)
