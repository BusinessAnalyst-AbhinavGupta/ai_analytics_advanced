"""Test helpers: tiny isolated context per test."""
from __future__ import annotations

import os
import tempfile

from analytics_platform.config import Settings
from analytics_platform.database import Store
from analytics_platform.execution.sampler import SamplerExecutor
from analytics_platform.observability import Observability
from analytics_platform.pipeline import Pipeline
from analytics_platform.tenancy import TenantService


class Ctx:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test.db")
        self.settings = Settings(db_path=self.db_path, source_dialect="duckdb")
        self.store = Store(self.db_path)
        self.tenants = TenantService(self.store)
        self.obs = Observability(self.store)
        self.executor = SamplerExecutor()
        self.pipeline = Pipeline(self.store, settings=self.settings,
                                 tenant_service=self.tenants, executor=self.executor,
                                 observability=self.obs)

    def close(self):
        self.store.close()
        try:
            self._tmp.cleanup()
        except Exception:
            pass


def make_ctx(warehouse=None) -> Ctx:
    ctx = Ctx()
    if warehouse:
        ctx.executor.register_warehouse(warehouse)
    return ctx