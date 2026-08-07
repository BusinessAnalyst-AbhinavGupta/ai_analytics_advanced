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


def list_models(provider: str = "openrouter", api_key: str = "",
                base_url: str = "https://openrouter.ai/api/v1",
                ollama_base_url: str = "http://localhost:11434",
                timeout: float = 10.0) -> List[Dict[str, Any]]:
    """List provider model options via a live ping (config-panel model dropdown).

    OpenRouter `/models` and local Ollama `/api/tags` are supported; anything else
    returns []. The key is used for this one call only and never persisted. Uses
    the stdlib urlopen so the platform stays dependency-light.
    """
    import json as _json
    from urllib import request as _urlreq

    provider = (provider or "").lower()
    try:
        if provider == "ollama":
            req = _urlreq.Request(f"{ollama_base_url.rstrip('/')}/api/tags",
                                  method="GET")
        else:  # openrouter (default) and unknown -> OpenRouter convention
            url = f"{base_url.rstrip('/')}/models"
            req = _urlreq.Request(url, method="GET")
            if api_key:
                req.add_header("Authorization", f"Bearer {api_key}")
        with _urlreq.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - provider URL is operator-supplied
            data = _json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - a failed provider ping must never break the panel
        return []

    models: List[Dict[str, Any]] = []
    if provider == "ollama":
        for m in data.get("models", []):
            models.append({"id": m.get("name", ""), "name": m.get("name", ""),
                           "provider": "ollama"})
    else:
        for m in data.get("data", []):
            models.append({"id": m.get("id", ""), "name": m.get("name", ""),
                           "context": m.get("context_length") or m.get("context", 0),
                           "provider": "openrouter"})
    return models


def list_provider_models(provider: str = "", api_key: str = "",
                         ollama_base_url: str = "http://localhost:11434",
                         timeout: float = 10.0) -> List[Dict[str, Any]]:
    """Thin alias for the config panel; empty provider -> [] (no accidental calls)."""
    if (provider or "").lower() in ("null", "", "none"):
        return []
    return list_models(provider=provider, api_key=api_key,
                       ollama_base_url=ollama_base_url, timeout=timeout)