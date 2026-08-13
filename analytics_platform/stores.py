"""Per-tenant database routing.

Every tenant is a different company, so the isolation boundary is the database
file — not a `WHERE tenant_id = ?` clause. This module owns the mapping from a
tenant id to that company's own SQLite file, and refuses to hand back a database
whose recorded owner disagrees with the tenant being asked for.

Two planes:

* control — `tenants`, `scheduler_state`, `api_logs`, `auth_principals`. One file.
  The registry cannot live inside a tenant's database, because you would need to
  know the tenant in order to find the tenant.
* tenant — everything else, one file per company at `<root>/<tenant_id>/tenant.db`.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Dict, List, Optional

from .database import CONTROL_SCHEMA, TENANT_SCHEMA, Store
from .domain import now_iso

logger = logging.getLogger(__name__)

TENANT_DB_FILENAME = "tenant.db"

# A tenant id names a directory, so it is restricted to characters that cannot
# traverse or escape. Real ids look like `tnt_d23cd823d4c6` or `DTDL`.
_SAFE_TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class TenantIsolationError(Exception):
    """A tenant database's recorded owner does not match the tenant requested."""


def validate_tenant_id(tenant_id: str) -> str:
    if not tenant_id or not _SAFE_TENANT_ID.match(tenant_id) or tenant_id in (".", ".."):
        raise ValueError(f"unsafe tenant id {tenant_id!r}: must match "
                         f"{_SAFE_TENANT_ID.pattern}")
    return tenant_id


class TenantStoreProvider:
    def __init__(self, control_db_path: str, tenants_root: str):
        self.control_db_path = control_db_path
        self.tenants_root = os.path.abspath(tenants_root)
        self._control: Optional[Store] = None
        self._tenants: Dict[str, Store] = {}
        # Test seam: forces the next for_tenant() to open this exact file, which
        # is how the isolation guard is exercised.
        self._path_override: Optional[str] = None

    # -- control plane -------------------------------------------------------
    @property
    def control(self) -> Store:
        if self._control is None:
            self._control = Store(self.control_db_path, schema=CONTROL_SCHEMA)
            logger.info("control store opened at %s", self.control_db_path)
        return self._control

    # -- tenant plane --------------------------------------------------------
    def tenant_db_path(self, tenant_id: str) -> str:
        validate_tenant_id(tenant_id)
        path = os.path.abspath(
            os.path.join(self.tenants_root, tenant_id, TENANT_DB_FILENAME))
        if not path.startswith(self.tenants_root + os.sep):
            raise ValueError(f"tenant id {tenant_id!r} escapes {self.tenants_root}")
        return path

    def for_tenant(self, tenant_id: str) -> Store:
        """This company's database. Opens and binds it on first use."""
        validate_tenant_id(tenant_id)
        cached = self._tenants.get(tenant_id)
        if cached is not None:
            return cached

        path = self._path_override or self.tenant_db_path(tenant_id)
        self._path_override = None
        os.makedirs(os.path.dirname(path), exist_ok=True)

        store = Store(path, schema=TENANT_SCHEMA)
        self._bind_owner(store, tenant_id, path)
        self._tenants[tenant_id] = store
        logger.debug("tenant store for %s opened at %s", tenant_id, path)
        return store

    @staticmethod
    def _bind_owner(store: Store, tenant_id: str, path: str) -> None:
        """Record the owner, or refuse if this file belongs to someone else."""
        row = store.query_one("SELECT tenant_id FROM db_owner WHERE singleton = 1")
        if row is None:
            store.execute(
                "INSERT INTO db_owner (singleton, tenant_id, bound_at) VALUES (1,?,?)",
                (tenant_id, now_iso()))
            return
        owner = row["tenant_id"]
        if owner != tenant_id:
            store.close()
            raise TenantIsolationError(
                f"{path} belongs to tenant {owner!r}, refusing to open it as "
                f"{tenant_id!r}. Each tenant is a separate company and must have "
                f"its own database.")

    def known_tenants(self) -> List[str]:
        """Tenant ids that have a database on disk."""
        if not os.path.isdir(self.tenants_root):
            return []
        out: List[str] = []
        for entry in sorted(os.listdir(self.tenants_root)):
            candidate = os.path.join(self.tenants_root, entry, TENANT_DB_FILENAME)
            if os.path.exists(candidate):
                out.append(entry)
        return out

    def close_all(self) -> None:
        for tenant_id, store in list(self._tenants.items()):
            try:
                store.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("closing store for %s failed: %s", tenant_id, exc)
        self._tenants.clear()
        if self._control is not None:
            try:
                self._control.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("closing control store failed: %s", exc)
            self._control = None
