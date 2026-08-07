"""LLM abstraction — injectable so the rest of the platform doesn't depend on
the static `core.llm_gateway.LLMGateway`. Provides a NullClient for offline/low
cost work and a GatewayClient adapter around the existing gateway.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@dataclass
class LLMResponse:
    text: str
    provider: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    ok: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "provider": self.provider, "model": self.model,
                "tokens_in": self.tokens_in, "tokens_out": self.tokens_out, "ok": self.ok}


@runtime_checkable
class LLMClient(Protocol):
    def generate(self, prompt: str, system_prompt: str = "", *, temperature: float = 0.0,
                 provider: str = "", model: str = "", api_key: str = "",
                 context_window: int = 0, timeout_s: float = 120.0,
                 on_token: Optional[Any] = None, **kwargs: Any) -> LLMResponse: ...


class NullClient(LLMClient):
    """Deterministic offline client used when no provider is configured."""

    name = "null"

    def generate(self, prompt: str, system_prompt: str = "", *, temperature: float = 0.0,
                 provider: str = "", model: str = "", api_key: str = "",
                 context_window: int = 0, timeout_s: float = 120.0,
                 on_token: Optional[Any] = None, **kwargs: Any) -> LLMResponse:
        return LLMResponse(text="", provider="null", model="null")


class GatewayClient(LLMClient):
    """Adapter around the existing static LLMGateway.generate (never instantiate LLMGateway)."""

    name = "gateway"

    def __init__(self, provider: str = "", model: str = "", api_key: str = "",
                 ollama_base_url: str = ""):
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.ollama_base_url = ollama_base_url

    def generate(self, prompt: str, system_prompt: str = "", *, temperature: float = 0.0,
                 provider: str = "", model: str = "", api_key: str = "",
                 context_window: int = 0, timeout_s: float = 120.0,
                 on_token: Optional[Any] = None, **kwargs: Any) -> LLMResponse:
        from core.llm_gateway import LLMGateway  # local import; static usage only
        res = LLMGateway.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            provider=provider or self.provider,
            model=model or self.model,
            api_key=api_key or self.api_key,
            temperature=temperature,
            context_window=context_window or 262144,
            ollama_base_url=self.ollama_base_url,
            on_token=on_token,
            **kwargs,
        )
        if isinstance(res, str):
            return LLMResponse(text=res, provider=provider or self.provider,
                               model=model or self.model)
        return LLMResponse(
            text=res.get("text", ""), provider=res.get("provider", self.provider),
            model=res.get("model", self.model), tokens_in=res.get("tokens_in", 0),
            tokens_out=res.get("tokens_out", 0), ok=res.get("ok", True))


def make_client(provider: str = "", model: str = "", api_key: str = "",
                ollama_base_url: str = "") -> LLMClient:
    if provider in ("null", "", "None"):
        return NullClient()
    return GatewayClient(provider=provider, model=model, api_key=api_key,
                         ollama_base_url=ollama_base_url)


def make_client_from(settings) -> LLMClient:
    """Build an LLMClient from platform Settings (duck-typed).

    `provider == "null"` (the default) yields NullClient, so the LLM path stays
    inert unless the operator explicitly enables a provider + key — data never
    leaves the process by accident.
    """
    return make_client(provider=settings.llm_provider, model=settings.llm_model,
                       api_key=settings.effective_api_key(),
                       ollama_base_url=settings.ollama_base_url)