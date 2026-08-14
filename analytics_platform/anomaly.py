"""Proactive Anomaly Triggers and KPI management."""
from typing import Any, Dict, List, Optional

from .domain import new_id, now_iso
from .brain.embedding import Embedder
from .brain.index import BrainIndex
from .brain.store import CompanyBrain
from .stores import TenantStoreProvider

class AnomalyService:
    def __init__(self, stores: TenantStoreProvider, tenants, embedder: Optional[Embedder] = None):
        self.stores = stores
        self.tenants = tenants
        self.embedder = embedder
        self.brain = lambda t: CompanyBrain(
            stores.for_tenant(t), t,
            index=BrainIndex(stores.for_tenant(t), embedder=self.embedder))

    def list_kpis(self, tenant_id: str) -> List[Dict[str, Any]]:
        rows = self.stores.for_tenant(tenant_id).query_all(
            "SELECT * FROM kpis WHERE tenant_id = ? ORDER BY created_at DESC", (tenant_id,))
        return [dict(r) for r in rows]

    def add_kpi(self, tenant_id: str, name: str, description: str, sql_query: str, frequency: str = "daily", threshold: Optional[str] = None) -> Dict[str, Any]:
        self.tenants.require_tenant(tenant_id)
        kpi_id = new_id("kpi")
        self.stores.for_tenant(tenant_id).execute(
            "INSERT INTO kpis (id, tenant_id, name, description, sql_query, frequency, is_active, created_at, threshold) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (kpi_id, tenant_id, name, description, sql_query, frequency, now_iso(), threshold)
        )
        return self.get_kpi(tenant_id, kpi_id)

    def update_kpi(self, tenant_id: str, kpi_id: str, threshold: Optional[str]) -> Dict[str, Any]:
        self.tenants.require_tenant(tenant_id)
        self.stores.for_tenant(tenant_id).execute(
            "UPDATE kpis SET threshold = ? WHERE id = ? AND tenant_id = ?",
            (threshold, kpi_id, tenant_id)
        )
        return self.get_kpi(tenant_id, kpi_id)

    def get_kpi(self, tenant_id: str, kpi_id: str) -> Dict[str, Any]:
        row = self.stores.for_tenant(tenant_id).query_one(
            "SELECT * FROM kpis WHERE id = ? AND tenant_id = ?", (kpi_id, tenant_id))
        if not row:
            raise ValueError(f"KPI {kpi_id} not found")
        return dict(row)

    def suggest_creatable_metrics(self, tenant_id: str) -> List[Dict[str, str]]:
        """Suggest metrics based on approved queries."""
        q_nodes = self.brain(tenant_id).search("", kind="QUERY", usable_only=True, limit=50)
        out = []
        for n in q_nodes:
            sql = n.payload.get("sql", "")
            if sql:
                out.append({"name": n.title, "description": n.summary or "", "sql_query": sql})
        return out

    def evaluate_kpis(self, tenant_id: str, executor: Any, pipeline: Any) -> int:
        """Run KPIs against the executor. If anomalous, trigger a diagnostic run via pipeline ingest."""
        from .execution.base import ExecutionContext
        from .observability import new_trace
        kpis = self.list_kpis(tenant_id)
        triggered = 0
        for k in kpis:
            if not k.get("is_active") or not k.get("threshold"):
                continue
            try:
                ctx = ExecutionContext(tenant_id=tenant_id, question=f"Check KPI: {k['name']}", trace_id=new_trace(), row_limit=50, dialect="athena")
                res = executor.execute(k["sql_query"], ctx)
                
                if res and res.ok and res.data is not None and not res.data.empty:
                    val = res.data.iloc[0, 0]
                    if self._evaluate_threshold(val, k["threshold"]):
                        pipeline.ingest([{
                            "sql": k["sql_query"],
                            "title": f"Anomaly Triggered: {k['name']}",
                            "summary": f"Proactive monitoring detected an anomaly in {k['name']}. Value {val} breached threshold {k['threshold']}."
                        }], tenant_id, by="anomaly_trigger")
                        triggered += 1
            except Exception:
                pass
        return triggered

    def _evaluate_threshold(self, val: Any, threshold_str: str) -> bool:
        if not threshold_str:
            return False
        import re
        m = re.match(r'([<>=!]+)\s*([\d.]+)', str(threshold_str).strip())
        if not m:
            return False
        op, num_str = m.groups()
        try:
            num = float(num_str)
            val_float = float(val)
            if op == '<': return val_float < num
            if op == '<=': return val_float <= num
            if op == '>': return val_float > num
            if op == '>=': return val_float >= num
            if op == '=' or op == '==': return val_float == num
            if op == '!=': return val_float != num
        except Exception:
            return False
        return False
