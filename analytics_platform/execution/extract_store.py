"""Durable, per-tenant Parquet store for conversation extracts.

This is the artifact layer the analyst was missing: every cube a turn
materialises lands as `<tenants_root>/<tenant_id>/extracts/<conversation_id>/
<label>.parquet` with a JSON sidecar carrying its provenance. Because the rows
survive the process, a reopened conversation stays analysable in Python and in
DuckDB instead of forcing a fresh warehouse round trip.

**Tenant isolation here is filesystem-level.** Every tenant is a different
company; a `tenant_id` column would not isolate anything. Every id and label is
validated against SAFE_ID before it is allowed anywhere near a path, and this
module is the sole owner of extract paths -- nothing else in the platform builds
one.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _require_safe(value: Any, what: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.match(value):
        raise ValueError(f"unsafe {what}: {value!r} (must match {SAFE_ID.pattern})")
    return value


@dataclass
class ExtractMeta:
    """What a materialised extract is, and which population it came from.

    The first block is true of any extract. The cube block below it is what
    makes two extracts comparable: `population_hash` is the reconciliation key
    (Task 7), and `dimensions` / `non_additive` / `filters` / the time range are
    what the DataManager (Task 10) tests coverage against.
    """

    label: str
    description: str = ""          # the question that produced it, truncated to 200 chars
    grain: List[str] = field(default_factory=list)     # ["session_id"]
    columns: List[str] = field(default_factory=list)
    dtypes: Dict[str, str] = field(default_factory=dict)
    row_count: int = 0
    truncated: bool = False        # row_count hit the ceiling; totals may be understated
    sql: str = ""
    created_at: str = ""
    # -- the population this extract belongs to (Task 7) --------------------
    base_view: str = ""
    population_hash: str = ""      # "" -> the aggregate path; reconciles with nothing
    projection_hash: str = ""
    dimensions: List[str] = field(default_factory=list)      # the cube's GROUP BY
    non_additive: List[str] = field(default_factory=list)    # measures that cannot roll up
    filters: Dict[str, List[str]] = field(default_factory=dict)   # the SLICE, not the population
    time_column: str = ""
    time_start: str = ""          # MEASURED off the frame -- what the warehouse returned
    time_end: str = ""
    # What the turn ASKED for. Distinct from the measured pair above, which is
    # deliberately taken off the frame so coverage can never reuse a cube for a
    # window it did not actually contain. The measured pair is only populated
    # when the time column is one of the cube's own columns, so a cube filtered
    # to July but grouped by country records no window at all -- yet the user
    # plainly did give it a timeframe. That question, "has a period been
    # established in this conversation", is what these two answer.
    requested_time_start: str = ""
    requested_time_end: str = ""
    grain_violated: bool = False   # an ID-grain extract came back with duplicate keys

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExtractMeta":
        """Tolerant of sidecars written by an older version of this dataclass:
        unknown keys are dropped, missing ones take their defaults."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


class ExtractStore:
    def __init__(self, tenants_dir: str) -> None:
        self.tenants_dir = tenants_dir

    # -- paths ---------------------------------------------------------------
    def dir_for(self, tenant_id: str, conversation_id: str) -> str:
        _require_safe(tenant_id, "tenant_id")
        _require_safe(conversation_id, "conversation_id")
        return os.path.join(self.tenants_dir, tenant_id, "extracts", conversation_id)

    def path(self, tenant_id: str, conversation_id: str, label: str) -> str:
        _require_safe(label, "label")
        return os.path.join(self.dir_for(tenant_id, conversation_id), f"{label}.parquet")

    def _sidecar(self, tenant_id: str, conversation_id: str, label: str) -> str:
        _require_safe(label, "label")
        return os.path.join(self.dir_for(tenant_id, conversation_id), f"{label}.json")

    # -- write ---------------------------------------------------------------
    def put(self, tenant_id: str, conversation_id: str, meta: ExtractMeta,
            df: pd.DataFrame) -> str:
        """Write the rows, then the sidecar. In that order, deliberately: a
        half-written pair must never report as complete, and `meta()` keys off
        the parquet's existence."""
        target = self.path(tenant_id, conversation_id, meta.label)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        df.to_parquet(target, index=False, engine="pyarrow")
        with open(self._sidecar(tenant_id, conversation_id, meta.label), "w",
                  encoding="utf-8") as fh:
            json.dump(asdict(meta), fh)
        return target

    # -- read ----------------------------------------------------------------
    def load(self, tenant_id: str, conversation_id: str,
             label: str) -> Optional[pd.DataFrame]:
        target = self.path(tenant_id, conversation_id, label)
        if not os.path.exists(target):
            return None
        try:
            return pd.read_parquet(target)
        except Exception as exc:  # noqa: BLE001 - a bad extract degrades, never crashes
            logger.warning("unreadable extract %s for tenant %s: %s -- falling back to "
                           "re-running SQL", target, tenant_id, exc)
            return None

    def meta(self, tenant_id: str, conversation_id: str,
             label: str) -> Optional[ExtractMeta]:
        if not os.path.exists(self.path(tenant_id, conversation_id, label)):
            return None
        sidecar = self._sidecar(tenant_id, conversation_id, label)
        if not os.path.exists(sidecar):
            return None
        try:
            with open(sidecar, encoding="utf-8") as fh:
                return ExtractMeta.from_dict(json.load(fh))
        except Exception as exc:  # noqa: BLE001
            logger.warning("unreadable extract sidecar %s: %s", sidecar, exc)
            return None

    def list_metas(self, tenant_id: str, conversation_id: str) -> List[ExtractMeta]:
        try:
            directory = self.dir_for(tenant_id, conversation_id)
        except ValueError:
            return []
        if not os.path.isdir(directory):
            return []
        out: List[ExtractMeta] = []
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".json"):
                continue
            label = name[: -len(".json")]
            if not SAFE_ID.match(label):
                continue
            m = self.meta(tenant_id, conversation_id, label)
            if m is not None:
                out.append(m)
        return out

    def parquet_paths(self, tenant_id: str, conversation_id: str) -> Dict[str, str]:
        """label -> parquet path, for the sandbox and the DuckDB workspace."""
        return {m.label: self.path(tenant_id, conversation_id, m.label)
                for m in self.list_metas(tenant_id, conversation_id)}

    # -- delete --------------------------------------------------------------
    def delete_conversation(self, tenant_id: str, conversation_id: str) -> None:
        try:
            directory = self.dir_for(tenant_id, conversation_id)
        except ValueError:
            return
        shutil.rmtree(directory, ignore_errors=True)

    def sweep(self, retention_days: int, now: str = "") -> int:
        """Remove conversation directories whose newest sidecar is older than the
        cutoff. Returns the number of directories removed. `now` is injectable so
        the sweep is testable without freezing the clock."""
        if retention_days <= 0:
            return 0
        anchor = (_parse_iso(now) if now else None) or datetime.now(timezone.utc)
        cutoff = anchor - timedelta(days=retention_days)
        removed = 0
        if not os.path.isdir(self.tenants_dir):
            return 0
        for tenant_id in sorted(os.listdir(self.tenants_dir)):
            root = os.path.join(self.tenants_dir, tenant_id, "extracts")
            if not os.path.isdir(root):
                continue
            for conversation_id in sorted(os.listdir(root)):
                directory = os.path.join(root, conversation_id)
                if not os.path.isdir(directory):
                    continue
                newest = self._newest_created_at(directory)
                if newest is None or newest >= cutoff:
                    continue
                shutil.rmtree(directory, ignore_errors=True)
                removed += 1
        return removed

    def _newest_created_at(self, directory: str) -> Optional[datetime]:
        newest: Optional[datetime] = None
        for name in os.listdir(directory):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(directory, name), encoding="utf-8") as fh:
                    stamp = json.load(fh).get("created_at", "")
            except Exception:  # noqa: BLE001
                continue
            parsed = _parse_iso(stamp)
            if parsed is None:
                continue
            if newest is None or parsed > newest:
                newest = parsed
        return newest


def _parse_iso(stamp: str) -> Optional[datetime]:
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


__all__ = ["ExtractStore", "ExtractMeta", "SAFE_ID"]
