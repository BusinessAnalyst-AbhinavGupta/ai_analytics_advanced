"""Test helpers: tiny isolated context per test."""
from __future__ import annotations

import os
import tempfile

from analytics_platform.config import Settings
from analytics_platform.database import Store
from analytics_platform.execution.sampler import SamplerExecutor
from analytics_platform.observability import Observability
from analytics_platform.pipeline import Pipeline
from analytics_platform.stores import TenantStoreProvider
from analytics_platform.tenancy import TenantService


class Ctx:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test.db")
        self.settings = Settings(db_path=self.db_path, source_dialect="duckdb",
                                 data_dir=self._tmp.name)
        # `self.store` is the legacy single combined-schema file: still used by
        # services (Pipeline and beyond) that haven't been threaded onto the
        # per-tenant provider yet. `self.stores` is the new control/tenant split,
        # used by TenantService/Observability/Scheduler.
        self.store = Store(self.db_path)
        self.stores = TenantStoreProvider(
            control_db_path=self.settings.resolve_control_db_path(),
            tenants_root=self.settings.resolve_tenants_root())
        self.tenants = TenantService(self.stores)
        self.obs = Observability(self.stores)
        self.executor = SamplerExecutor()
        self.pipeline = Pipeline(self.store, settings=self.settings,
                                 tenant_service=self.tenants, executor=self.executor,
                                 observability=self.obs)

    def close(self):
        self.store.close()
        self.stores.close_all()
        try:
            self._tmp.cleanup()
        except Exception:
            pass


def make_ctx(warehouse=None) -> Ctx:
    ctx = Ctx()
    if warehouse:
        ctx.executor.register_warehouse(warehouse)
    return ctx