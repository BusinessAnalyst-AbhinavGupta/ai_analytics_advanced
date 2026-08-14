"""Platform configuration (dataclass, no secrets at rest in code)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class PolicySettings:
    allow_ddl_dml: bool = False       # hard default: read-only
    default_row_limit: int = 50000
    require_date_filter_tables: List[str] = field(default_factory=list)
    block_multi_statement: bool = True
    forge_dialect: str = "athena"        # sqlglot dialect used for validation


@dataclass
class Settings:
    app_name: str = "ai-analytics-platform"
    db_path: str = ""                  # empty -> default ./data/platform.db
    llm_provider: str = "null"         # null | openrouter | gemini | ollama
    llm_model: str = "deepseek/deepseek-v4-flash-0731"
    llm_api_key: str = ""              # prefer env ANALYTICS_LLM_API_KEY
    ollama_base_url: str = "http://localhost:11434"
    source_dialect: str = "athena"      # dialect the SQL is authored in (executors transpile if needed)
    # Brain retrieval -------------------------------------------------------
    embedding_enabled: bool = True      # False -> lexical-only retrieval (logged)
    embedding_model: str = "BAAI/bge-small-en-v1.5"   # ~130MB; large variant is 1.3GB
    embedding_query_prefix: str = (      # BGE retrieval models expect this on queries only
        "Represent this sentence for searching relevant passages: ")
    policy: PolicySettings = field(default_factory=PolicySettings)
    data_dir: str = field(default_factory=lambda: os.environ.get("ANALYTICS_DATA_DIR", "")) # synthetic / sampled warehouse loader dir
    metabase_live: bool = False        # gate for live Metabase executors/tests (ANALYTICS_MB_LIVE=1)
    metabase_base_url: str = ""        # Metabase URL (informational; same-origin fetch uses the tab)
    metabase_database_id: Any = ""     # Metabase DB id (str or int) to query
    metabase_expected_host: str = ""   # hostname guard (anti-tenant-bleed)
    # P8 governance ---------------------------------------------------------
    auth_secret: str = ""              # HMAC secret for issue/verify (env ANALYTICS_AUTH_SECRET)
    auth_enabled: bool = False         # when True, guarded routes require an Authorization token
    oidc_issuer: str = ""              # optional OIDC issuer to trust (verify-sig seam; no secret at rest)
    cost_per_1k_input: float = 0.30    # USD per 1k tokens for usage metering
    cost_per_1k_output: float = 1.20
    # Phase 9 — owner-facing observability + background junior ------------------
    log_retention_days: int = 30        # API logs kept this long before purge
    maintenance_interval_days: int = 7  # weekly auto-trigger of log clean-up
    scheduler_enabled: bool = False     # background scheduler (serve-only; env WATCHER)
    junior_work_start: str = "10:00"   # junior background window start (system time)
    junior_work_end: str = "19:00"      # junior background window end
    junior_min_interval_minutes: int = 60  # one problem statement per hour max
    junior_daily_cap: int = 3           # at most N junior analyses per UTC day
    junior_review_backlog_max: int = 3  # pause generating once N analyses await senior review
    # CP-15: low-level exploratory analyses auto-fold to FINDINGs, capped so schema
    # probes never spam the Brain; high-level runs may spawn supporting low-level
    # probes, bounded per high-level cycle and exempt from the daily/rate caps.
    junior_autopromote_cap: int = 500  # max approved FINDINGs per tenant from auto-accept
    junior_supporting_cap: int = 5     # max on-the-spot supporting probes per high-level run
    junior_llm_cache_ttl_minutes: int = 60  # LLM enrichment cached per tenant (bill guard)
    llm_daily_cap: int = 20         # at most N LLM generations per tenant per UTC day (persisted)
    junior_human_signoff_days: int = 7 # initial window: every analysis -> human review

    def resolve_db_path(self) -> str:
        """DEPRECATED: the single shared database. Use resolve_control_db_path()
        and resolve_tenants_root(). Retained so `adopt-db` can find a legacy file."""
        if self.data_dir:
            return os.path.join(self.data_dir, "platform.db")
        return self.db_path or "data/platform.db"

    def resolve_control_db_path(self) -> str:
        """The cross-tenant registry database (tenants, scheduler, api logs)."""
        if self.data_dir:
            return os.path.join(self.data_dir, "control.db")
        return "data/control.db"

    def resolve_tenants_root(self) -> str:
        """Directory holding one database per company: <root>/<tenant_id>/tenant.db."""
        if self.data_dir:
            return os.path.join(self.data_dir, "tenants")
        return "tenants"

    def effective_api_key(self) -> str:
        """LLM key resolution: explicit override, then ANALYTICS_LLM_API_KEY, then
        the operator's OpenRouter key (OPENROUTER_API_KEY). Never stored at rest."""
        return (self.llm_api_key
                or os.environ.get("ANALYTICS_LLM_API_KEY", "")
                or os.environ.get("OPENROUTER_API_KEY", ""))

    @classmethod
    def from_env(cls) -> "Settings":
        # Default the LLM provider to OpenRouter when the operator only exports an
        # OpenRouter key (and no explicit provider override) — this machine does.
        _provider = os.environ.get("ANALYTICS_LLM_PROVIDER", "").strip()
        if not _provider and os.environ.get("OPENROUTER_API_KEY"):
            _provider = "openrouter"
        return cls(
            data_dir=os.environ.get("ANALYTICS_DATA_DIR", ""),
            db_path=os.environ.get("ANALYTICS_DB_PATH", ""),
            llm_provider=_provider or "null",
            llm_model=os.environ.get("ANALYTICS_LLM_MODEL", "deepseek/deepseek-v4-flash-0731"),
            llm_api_key=os.environ.get("ANALYTICS_LLM_API_KEY", ""),
            ollama_base_url=os.environ.get("ANALYTICS_OLLAMA_URL", "http://localhost:11434"),
            metabase_live=os.environ.get("ANALYTICS_MB_LIVE") == "1",
            metabase_base_url=os.environ.get("ANALYTICS_MB_HOST", ""),
            metabase_database_id=os.environ.get("ANALYTICS_MB_DATABASE_ID", ""),
            metabase_expected_host=os.environ.get("ANALYTICS_MB_EXPECTED_HOST", ""),
            auth_secret=os.environ.get("ANALYTICS_AUTH_SECRET", ""),
            auth_enabled=os.environ.get("ANALYTICS_AUTH_ENABLED") == "1",
            oidc_issuer=os.environ.get("ANALYTICS_OIDC_ISSUER", ""),
            cost_per_1k_input=float(os.environ.get("ANALYTICS_COST_PER_1K_IN", "0.30")),
            cost_per_1k_output=float(os.environ.get("ANALYTICS_COST_PER_1K_OUT", "1.20")),
            log_retention_days=int(os.environ.get("ANALYTICS_LOG_RETENTION_DAYS", "30")),
            maintenance_interval_days=int(os.environ.get("ANALYTICS_MAINT_INTERVAL_DAYS", "7")),
            scheduler_enabled=os.environ.get("ANALYTICS_WATCHER") == "1",
            junior_work_start=os.environ.get("ANALYTICS_JUNIOR_WORK_START", "10:00"),
            junior_work_end=os.environ.get("ANALYTICS_JUNIOR_WORK_END", "19:00"),
            junior_min_interval_minutes=int(
                os.environ.get("ANALYTICS_JUNIOR_MIN_INTERVAL_MINUTES", "60")),
            junior_daily_cap=int(
                os.environ.get("ANALYTICS_JUNIOR_DAILY_CAP", "3")),
            junior_review_backlog_max=int(
                os.environ.get("ANALYTICS_JUNIOR_REVIEW_BACKLOG_MAX", "3")),
            junior_llm_cache_ttl_minutes=int(
                os.environ.get("ANALYTICS_JUNIOR_LLM_CACHE_TTL_MINUTES", "60")),
            junior_autopromote_cap=int(
                os.environ.get("ANALYTICS_JUNIOR_AUTOPROMOTE_CAP", "500")),
            junior_supporting_cap=int(
                os.environ.get("ANALYTICS_JUNIOR_SUPPORTING_CAP", "5")),
            llm_daily_cap=int(
                os.environ.get("ANALYTICS_LLM_DAILY_CAP", "20")),
            junior_human_signoff_days=int(
                os.environ.get("ANALYTICS_JUNIOR_HUMAN_SIGNOFF_DAYS", "7")),
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["policy"] = asdict(self.policy)
        return d