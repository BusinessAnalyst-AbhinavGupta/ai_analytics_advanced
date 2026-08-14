"""In-process, per-conversation DataFrame cache for the Python compute path.

Holds already-fetched query results in memory so a follow-up turn in the
same conversation can run Python against them instead of re-running SQL.
Never persisted to disk. Only `describe()`'s schema (columns/dtypes/row
count -- never row data) is meant to leave this module, e.g. into an LLM
prompt; callers must not serialize a DataFrame straight out of `get()` into
anything that reaches the LLM or a persisted column. Tenant- and
conversation-scoped by construction (every method takes both ids), and
LRU-bounded in both dimensions so a long-running process doesn't grow
without limit.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


@dataclass
class CachedFrame:
    label: str
    description: str
    df: pd.DataFrame

    def describe(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "description": self.description,
            "columns": list(self.df.columns),
            "dtypes": {c: str(t) for c, t in self.df.dtypes.items()},
            "row_count": len(self.df),
        }


class ConversationDataCache:
    def __init__(self, max_conversations: int = 50, max_frames_per_conversation: int = 5):
        self.max_conversations = max_conversations
        self.max_frames_per_conversation = max_frames_per_conversation
        self._data: "OrderedDict[Tuple[str, str], OrderedDict[str, CachedFrame]]" = OrderedDict()

    def _key(self, tenant_id: str, conversation_id: str) -> Tuple[str, str]:
        return (tenant_id, conversation_id)

    def put(self, tenant_id: str, conversation_id: str, label: str,
            description: str, df: pd.DataFrame) -> None:
        key = self._key(tenant_id, conversation_id)
        if key in self._data:
            self._data.move_to_end(key)
        frames = self._data.setdefault(key, OrderedDict())
        frames[label] = CachedFrame(label=label, description=description, df=df)
        frames.move_to_end(label)
        while len(frames) > self.max_frames_per_conversation:
            frames.popitem(last=False)
        while len(self._data) > self.max_conversations:
            self._data.popitem(last=False)

    def get(self, tenant_id: str, conversation_id: str, label: str) -> Optional[pd.DataFrame]:
        key = self._key(tenant_id, conversation_id)
        frames = self._data.get(key)
        if not frames or label not in frames:
            return None
        self._data.move_to_end(key)
        frames.move_to_end(label)
        return frames[label].df

    def list_available(self, tenant_id: str, conversation_id: str) -> List[Dict[str, Any]]:
        frames = self._data.get(self._key(tenant_id, conversation_id))
        if not frames:
            return []
        return [f.describe() for f in frames.values()]

    def next_label(self, tenant_id: str, conversation_id: str) -> str:
        frames = self._data.get(self._key(tenant_id, conversation_id))
        existing = set(frames.keys()) if frames else set()
        n = 1
        while f"df_{n}" in existing:
            n += 1
        return f"df_{n}"
