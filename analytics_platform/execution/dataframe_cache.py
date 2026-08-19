"""Per-conversation DataFrame cache for the Python and DuckDB compute paths.

Holds already-materialised query results so a follow-up turn in the same
conversation can be answered locally instead of re-running SQL through the
warehouse. Memory is the hot layer; the durable layer is `ExtractStore`, which
writes one Parquet file plus a JSON sidecar per extract under the tenant's own
directory. A memory miss therefore falls back to disk rather than forcing a new
warehouse round trip -- that fallback is what keeps a *reopened* conversation
analysable, which the pure in-memory version could never do.

Row data still must not leave this module casually: only `describe()`'s output
(schema, grain, cube dimensions, truncation flag, and a 3-row JSON-safe sample)
is meant for an LLM prompt or a persisted column. Callers must not serialize a
DataFrame straight out of `get()` into either. Tenant- and conversation-scoped
by construction (every method takes both ids), and LRU-bounded in memory in both
dimensions so a long-running process doesn't grow without limit.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .extract_store import ExtractMeta, ExtractStore

logger = logging.getLogger(__name__)

_SAMPLE_ROWS = 3


def _json_safe(value: Any) -> Any:
    """describe() lands in a prompt and in the JSON answer payload, so a
    Timestamp, a numpy scalar, or a Decimal has to be coerced here rather than
    blowing up at the API boundary."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


@dataclass
class CachedFrame:
    label: str
    description: str
    df: pd.DataFrame
    meta: Optional[ExtractMeta] = None

    def describe(self) -> Dict[str, Any]:
        return dict(_describe_meta(self.meta, self.label, self.description),
                    columns=list(self.df.columns),
                    dtypes={c: str(t) for c, t in self.df.dtypes.items()},
                    row_count=len(self.df),
                    sample=[{k: _json_safe(v) for k, v in row.items()}
                            for row in self.df.head(_SAMPLE_ROWS).to_dict(orient="records")])


def _describe_meta(meta: Optional[ExtractMeta], label: str,
                   description: str = "") -> Dict[str, Any]:
    """The provenance half of describe(), shared by the memory and disk paths."""
    return {
        "label": label,
        "description": description or (meta.description if meta else ""),
        "grain": list(meta.grain) if meta else [],
        "truncated": bool(meta.truncated) if meta else False,
        "base_view": meta.base_view if meta else "",
        "population_hash": meta.population_hash if meta else "",
        "projection_hash": meta.projection_hash if meta else "",
        "dimensions": list(meta.dimensions) if meta else [],
        "non_additive": list(meta.non_additive) if meta else [],
        "filters": dict(meta.filters) if meta else {},
        "time_column": meta.time_column if meta else "",
        "time_start": meta.time_start if meta else "",
        "time_end": meta.time_end if meta else "",
        "requested_time_start": meta.requested_time_start if meta else "",
        "requested_time_end": meta.requested_time_end if meta else "",
    }


def _describe_from_disk(meta: ExtractMeta) -> Dict[str, Any]:
    d = _describe_meta(meta, meta.label)
    d.update(columns=list(meta.columns), dtypes=dict(meta.dtypes),
             row_count=int(meta.row_count), sample=[])
    return d


class ConversationDataCache:
    def __init__(self, max_conversations: int = 50, max_frames_per_conversation: int = 5,
                 store: Optional[ExtractStore] = None):
        self.max_conversations = max_conversations
        self.max_frames_per_conversation = max_frames_per_conversation
        self.store = store
        self._data: "OrderedDict[Tuple[str, str], OrderedDict[str, CachedFrame]]" = OrderedDict()
        # Highest label ordinal ever issued per conversation. Deliberately NOT
        # derived from the live frames: frame-level LRU eviction frees a label name,
        # and reissuing it would let two different queries in one persisted
        # conversation share a df_label. Only whole-conversation eviction clears it,
        # so memory stays bounded by max_conversations exactly as before.
        self._label_counters: "OrderedDict[Tuple[str, str], int]" = OrderedDict()

    def _key(self, tenant_id: str, conversation_id: str) -> Tuple[str, str]:
        return (tenant_id, conversation_id)

    def _disk_metas(self, tenant_id: str, conversation_id: str) -> List[ExtractMeta]:
        if self.store is None:
            return []
        try:
            return self.store.list_metas(tenant_id, conversation_id)
        except Exception as exc:  # noqa: BLE001 - an unreadable store degrades to memory
            logger.warning("could not list extracts for %s/%s: %s",
                           tenant_id, conversation_id, exc)
            return []

    def put(self, tenant_id: str, conversation_id: str, label: str,
            description: str, df: pd.DataFrame,
            meta: Optional[ExtractMeta] = None) -> None:
        key = self._key(tenant_id, conversation_id)
        if key in self._data:
            self._data.move_to_end(key)
        frames = self._data.setdefault(key, OrderedDict())
        frames[label] = CachedFrame(label=label, description=description, df=df, meta=meta)
        frames.move_to_end(label)
        while len(frames) > self.max_frames_per_conversation:
            frames.popitem(last=False)
        while len(self._data) > self.max_conversations:
            evicted_key, _ = self._data.popitem(last=False)
            self._label_counters.pop(evicted_key, None)

        if self.store is not None and meta is not None:
            try:
                self.store.put(tenant_id, conversation_id, meta, df)
            except Exception as exc:  # noqa: BLE001
                # An unwritable disk degrades to the old in-memory behaviour. It
                # does not fail the chat turn -- the answer is still computable,
                # it just won't survive a restart.
                logger.warning("could not persist extract %s for %s/%s: %s",
                               label, tenant_id, conversation_id, exc)

    def get(self, tenant_id: str, conversation_id: str, label: str) -> Optional[pd.DataFrame]:
        key = self._key(tenant_id, conversation_id)
        frames = self._data.get(key)
        if frames and label in frames:
            self._data.move_to_end(key)
            frames.move_to_end(label)
            return frames[label].df
        if self.store is None:
            return None
        try:
            df = self.store.load(tenant_id, conversation_id, label)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not load extract %s for %s/%s: %s",
                           label, tenant_id, conversation_id, exc)
            return None
        if df is None:
            return None
        meta = self.store.meta(tenant_id, conversation_id, label)
        self.put(tenant_id, conversation_id, label,
                 meta.description if meta else "", df, meta=None)
        # Re-attach the sidecar without re-writing the parquet we just read.
        cached = self._data.get(key, {}).get(label)
        if cached is not None:
            cached.meta = meta
        return df

    def list_available(self, tenant_id: str, conversation_id: str) -> List[Dict[str, Any]]:
        """Disk first, then overlay live in-memory entries so a hot frame's real
        row count and sample win over what the sidecar recorded."""
        by_label: Dict[str, Dict[str, Any]] = {
            m.label: _describe_from_disk(m)
            for m in self._disk_metas(tenant_id, conversation_id)}
        for frame in (self._data.get(self._key(tenant_id, conversation_id)) or {}).values():
            by_label[frame.label] = frame.describe()
        return [by_label[label] for label in sorted(by_label)]

    def paths(self, tenant_id: str, conversation_id: str) -> Dict[str, str]:
        """label -> Parquet path, for the sandbox's out-of-process loading."""
        if self.store is None:
            return {}
        try:
            return self.store.parquet_paths(tenant_id, conversation_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not resolve extract paths for %s/%s: %s",
                           tenant_id, conversation_id, exc)
            return {}

    def next_label(self, tenant_id: str, conversation_id: str) -> str:
        """Issue the next df_N label for a conversation. Monotonic: a label is never
        reissued within one conversation, even after its frame is LRU-evicted, and
        never over an extract that exists only on disk."""
        key = self._key(tenant_id, conversation_id)
        frames = self._data.get(key)
        existing = set(frames.keys()) if frames else set()
        existing.update(m.label for m in self._disk_metas(tenant_id, conversation_id))
        n = self._label_counters.get(key, 0) + 1
        while f"df_{n}" in existing:
            n += 1
        self._label_counters[key] = n
        return f"df_{n}"
