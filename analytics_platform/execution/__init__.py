"""Execution layer: executor abstraction + policy engine.

Analytics code never knows whether execution is a browser session (current
Metabase reality), a Metabase API, or a direct DB. It only talks to the
`QueryExecutor` protocol.
"""
from .base import QueryExecutor, QueryResult, SessionStatus, ExecutionContext
from .policy import QueryPolicy, PolicyDecision

__all__ = ["QueryExecutor", "QueryResult", "SessionStatus", "ExecutionContext",
           "QueryPolicy"]