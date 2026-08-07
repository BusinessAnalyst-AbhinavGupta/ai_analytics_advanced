"""Analytics Platform — standalone, company-independent AI analytics copilot.

See STANDALONE_ANALYTICS_PLATFORM_PLAN.md for the north-star design.
This package is the P1-P3 foundation: a modular monolith with typed domain
models, an injectable LLM client, an executor abstraction (browser-session
first), a governed Company Brain, a query-policy engine, observability, and a
FastAPI surface — all runnable fully offline via a sampler executor.
"""
__version__ = "0.1.0"