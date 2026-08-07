"""llm package: injectable LLM clients + prompt builders."""
from .client import LLMClient, LLMResponse, NullClient, GatewayClient, make_client

__all__ = ["LLMClient", "LLMResponse", "NullClient", "GatewayClient", "make_client"]