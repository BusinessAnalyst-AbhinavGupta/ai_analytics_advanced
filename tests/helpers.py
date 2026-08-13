"""Test helpers: tiny isolated context per test."""
from __future__ import annotations

import os
import tempfile

from analytics_platform.config import Settings
from analytics_platform.execution.sampler import SamplerExecutor
from analytics_platform.observability import Observability
from analytics_platform.pipeline import Pipeline
from analytics_platform.stores import TenantStoreProvider
from analytics_platform.tenancy import TenantService

DEFAULT_TEST_TENANT = "t1"


class Ctx:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(source_dialect="duckdb", data_dir=self._tmp.name)
        # `self.db_path` is referenced by some tests: the control database, since
        # the legacy single combined-schema file is gone (Task 5).
        self.db_path = self.settings.resolve_control_db_path()
        self.stores = TenantStoreProvider(
            control_db_path=self.settings.resolve_control_db_path(),
            tenants_root=self.settings.resolve_tenants_root())
        self.tenants = TenantService(self.stores)
        self.obs = Observability(self.stores)
        self.executor = SamplerExecutor()
        self.pipeline = Pipeline(self.stores, settings=self.settings,
                                 tenant_service=self.tenants, executor=self.executor,
                                 observability=self.obs)

    @property
    def store(self):
        """Most tests use a single tenant; this is that tenant's database."""
        return self.stores.for_tenant(DEFAULT_TEST_TENANT)

    def close(self):
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