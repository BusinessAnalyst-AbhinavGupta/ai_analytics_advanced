"""Phase 9 — owner-facing scheduler.

A single background loop (serve-only) drives two owner-facing jobs on top of the
empty in-process API:

  * `_purge_logs_tick`  — weekly auto-trigger that purges `api_logs` older than
    `Settings.log_retention_days` (default 30 days). State is persisted in
    `scheduler_state` so a restart never re-runs a purge within the same interval.
  * `_junior_tick`      — hands work to `JuniorWorker`, which only runs inside its
    system-time window and enforces "one problem statement per hour", serial,
    never-parallel queries (see junior_worker.py).

Invariants preserved: read-only policy for queries, cookie stays in the browser,
and observability events are emitted for every tick (no credentials ever logged).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .database import Store
from .observability import Observability

log = logging.getLogger("ai_analytics.scheduler")

_DEFAULT_INTERVAL_S = 60.0  # how often the loop wakes to check dues

class Scheduler:
    """Background loop with persistent due-state. Offline-safe and testable."""

    def __init__(self, store: Store, observability: Optional[Observability] = None,
                 retention_days: int = 30, maintenance_interval_days: int = 7,
                 junior_worker: Optional[Any] = None,
                 interval_seconds: float = _DEFAULT_INTERVAL_S,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.obs = observability or Observability(store)
        self.retention_days = int(retention_days)
        self.maintenance_interval_days = int(maintenance_interval_days)
        self.junior = junior_worker
        self.interval_seconds = interval_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_purge_ts: Optional[float] = None
        self._last_junior_ts: Optional[float] = None

    # -- persisted due-state ------------------------------------------------- #
    def _get_state(self, key: str) -> Optional[float]:
        try:
            row = self.store.query_one(
                "SELECT value FROM scheduler_state WHERE key=?", (key,))
            if row and row["value"]:
                return float(row["value"])
        except Exception:
            pass
        return None

    def _set_state(self, key: str, value: float) -> None:
        try:
            self.store.execute(
                "INSERT INTO scheduler_state (key,value,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, str(value), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        except Exception:
            log.warning("scheduler_state write failed for %s", key)

    def last_purge_ts(self) -> Optional[float]:
        if self._last_purge_ts is None:
            self._last_purge_ts = self._get_state("maintenance_last_purge_ts")
        return self._last_purge_ts

    def junior_last_ts(self) -> Optional[float]:
        if self._last_junior_ts is None:
            self._last_junior_ts = self._get_state("junior_last_cycle_ts")
        return self._last_junior_ts
# -- jobs ----------------------------------------------------------------- #
    def purge_logs_once(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Weekly auto-trigger: if not purged in the interval, purge API logs."""
        now = now if now is not None else self._clock()
        with self._lock:
            last = self.last_purge_ts()
            if last is not None and (now - last) < self.maintenance_interval_days * 86400.0:
                return {"ran": False, "reason": "not_due",
                        "last_purge_ts": last,
                        "next_due_ts": last + self.maintenance_interval_days * 86400.0}
            result = self.obs.purge_logs(self.retention_days, dry_run=False,
                                         now=now)
            self._set_state("maintenance_last_purge_ts", now)
            self._last_purge_ts = now
            self.obs.event(tenant_id="", stage="maintenance.log_purge", actor="scheduler",
                           status="OK", meta={"retention_days": self.retention_days,
                                              "purged": result.get("expired_rows")})
            return {"ran": True, "reason": "due", **result,
                    "last_purge_ts": now,
                    "next_due_ts": now + self.maintenance_interval_days * 86400.0}

    def junior_once(self, tenant_id: str) -> Dict[str, Any]:
        """Delegate a single serial junior cycle to the worker (may skip)."""
        if self.junior is None:
            return {"ran": False, "reason": "no_worker"}
        result = self.junior.run_cycle(tenant_id)
        if result and result.get("ran"):
            self._set_state("junior_last_cycle_ts", self._clock())
            self._last_junior_ts = self._clock()
        return result

    # -- loop ----------------------------------------------------------------- #
    def tick(self) -> List[Dict[str, Any]]:
        """One wake: run due jobs (purge always, junior if a worker is registered
        with a default tenant). Kept serial; never runs jobs in parallel."""
        out: List[Dict[str, Any]] = []
        out.append(self.purge_logs_once())
        if self.junior is not None and getattr(self.junior, "tenant_id", None):
            out.append(self.junior_once(self.junior.tenant_id))
        return out

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # never kill the loop
                log.exception("scheduler tick failed")
            self._stop.wait(self.interval_seconds)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


__all__ = ["Scheduler", "_DEFAULT_INTERVAL_S"]