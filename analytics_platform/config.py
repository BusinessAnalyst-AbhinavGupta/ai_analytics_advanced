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
    policy: PolicySettings = field(default_factory=PolicySettings)
    data_dir: str = ""                 # synthetic / sampled warehouse loader dir
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

    def resolve_db_path(self) -> str:
        if self.db_path:
            return self.db_path
        return os.path.join(os.path.dirname(__file__), "..", "data", "platform.db")

    def effective_api_key(self) -> str:
        return self.llm_api_key or os.environ.get("ANALYTICS_LLM_API_KEY", "")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=os.environ.get("ANALYTICS_DB_PATH", ""),
            llm_provider=os.environ.get("ANALYTICS_LLM_PROVIDER", "null"),
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
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["policy"] = asdict(self.policy)
        return d