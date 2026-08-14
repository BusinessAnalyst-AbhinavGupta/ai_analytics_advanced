"""Typed domain models shared across the platform."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class TenantStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PROVISIONING = "PROVISIONING"
    SUSPENDED = "SUSPENDED"


class DataSourceKind(str, Enum):
    METABASE_BROWSER = "metabase_browser"   # open-browser cookie session (default)
    DIRECT_DB = "direct_db"                  # optional, future
    METABASE_API = "metabase_api"            # optional, future


class ReviewStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    APPROVED_WITH_CAVEATS = "APPROVED_WITH_CAVEATS"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    REJECTED = "REJECTED"
    STALE = "STALE"
    SUPERSEDED = "SUPERSEDED"
    ARCHIVED = "ARCHIVED"

    def is_usable(self) -> bool:
        return self in (ReviewStatus.APPROVED, ReviewStatus.APPROVED_WITH_CAVEATS)


class NodeKind(str, Enum):
    METRIC = "METRIC"
    DEFINITION = "DEFINITION"
    FINDING = "FINDING"
    QUERY = "QUERY"
    JOIN_RULE = "JOIN_RULE"
    BUSINESS_RULE = "BUSINESS_RULE"
    IDIOM = "IDIOM"       # reusable SQL pattern/skeleton (from the prototype knowledge graph)
    EXTERNAL = "EXTERNAL" # external-research claim (never auto-promotes to company fact)


class AnswerMode(str, Enum):
    DIRECT_FROM_APPROVED_KNOWLEDGE = "DIRECT_FROM_APPROVED_KNOWLEDGE"
    REFRESHED_APPROVED_QUERY = "REFRESHED_APPROVED_QUERY"
    ADAPTED_APPROVED_QUERY = "ADAPTED_APPROVED_QUERY"
    NEW_LOW_RISK_ANALYSIS = "NEW_LOW_RISK_ANALYSIS"
    REQUIRES_SENIOR_REVIEW = "REQUIRES_SENIOR_REVIEW"
    CANNOT_ANSWER = "CANNOT_ANSWER"
    SKILL_EXECUTED_ANALYSIS = "SKILL_EXECUTED_ANALYSIS"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"


class RunStatus(str, Enum):
    PLANNED = "PLANNED"
    POLICY_REJECTED = "POLICY_REJECTED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


@dataclass
class CompanyTarget:
    name: str
    description: str = ""
    category: str = "growth"          # growth | margin | funnel | retention | risk | efficiency | satisfaction
    priority: int = 1
    owner: str = ""
    time_horizon: str = "quarterly"
    target_value: Optional[float] = None
    metric_refs: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    last_reviewed: str = ""
    sql_query: str = ""
    threshold: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AnalystAI:
    """Per-analyst AI capability toggle + model selection (config panel).

    `enabled=False` turns that analyst's autonomous AI off. For the senior this
    means its workload falls to a human, who performs the senior role through the
    same interface (see SeniorService). Model fields are non-secret (provider +
    model id); any API key is injected at runtime from env, never stored here.
    """

    role: str = "junior"          # junior | senior | stakeholder
    enabled: bool = True
    provider: str = ""            # e.g. openrouter | gemini | ollama
    model: str = ""               # e.g. deepseek/deepseek-v4-flash-0731

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Junior question-depth levels (human-controlled on the senior tab). Higher depth
# asks deeper business questions + hypotheses; lower depth asks basic ones.
JUNIOR_DEPTH_LABELS = {0: "basic", 1: "standard", 2: "advanced"}


def clamp_junior_depth(level: Any) -> int:
    """Clamp a junior depth value to the valid range 0..2."""
    try:
        v = int(level)
    except (TypeError, ValueError):
        v = 1
    return max(0, min(2, v))


@dataclass
class AnalystConfig:
    """All three analysts' AI + model config for one tenant.

    `junior_depth` is the depth/competency level the human controls on the senior
    tab: promoted -> deeper business questions + hypotheses; demoted -> basic ones.
    `human_signoff_days` is the initial window (default 7) during which *every*
    junior analysis requires an explicit human review, even when the senior AI is on.
    """

    tenant_id: str
    junior: AnalystAI = field(default_factory=lambda: AnalystAI("junior"))
    senior: AnalystAI = field(default_factory=lambda: AnalystAI("senior"))
    stakeholder: AnalystAI = field(default_factory=lambda: AnalystAI("stakeholder"))
    junior_depth: int = 1           # 0=basic | 1=standard | 2=advanced
    human_signoff_days: int = 7     # initial window: every analysis -> human review
    updated_at: str = ""

    @property
    def depth_label(self) -> str:
        return JUNIOR_DEPTH_LABELS.get(clamp_junior_depth(self.junior_depth), "standard")

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "tenant_id": self.tenant_id,
            "junior": self.junior.to_dict(),
            "senior": self.senior.to_dict(),
            "stakeholder": self.stakeholder.to_dict(),
            "junior_depth": clamp_junior_depth(self.junior_depth),
            "depth_label": self.depth_label,
            "human_signoff_days": int(self.human_signoff_days),
            "updated_at": self.updated_at,
        }
        return d


@dataclass
class CompanyProfile:
    tenant_id: str
    name: str = ""
    industry: str = ""
    region: str = ""
    description: str = ""             # what the business does
    customers: str = ""               # who its customers are
    product: str = ""                 # what the product is
    value_creation: str = ""          # how it creates value
    revenue_model: str = ""           # how it makes money
    targets: List[CompanyTarget] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    competitors: List[str] = field(default_factory=list)
    preferred_metrics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["targets"] = [t.to_dict() for t in self.targets]
        return d


@dataclass
class Tenant:
    id: str
    name: str
    region: str = "global"
    llm_provider: str = "null"        # null | openrouter | gemini | ollama ...
    retention_days: int = 90
    status: TenantStatus = TenantStatus.ACTIVE
    created_at: str = field(default_factory=now_iso)
    purpose: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = d["status"].value if d["status"] else d["status"]
        return d


@dataclass
class DataSource:
    id: str
    tenant_id: str
    name: str
    kind: DataSourceKind
    dialect: str = "ANSI"
    connected: bool = True
    tables: List[str] = field(default_factory=list)
    config: Dict[str, Any] = field(default_factory=dict)   # NEVER store credentials here
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["kind"] = d["kind"].value if d["kind"] else d["kind"]
        return d


@dataclass
class KnowledgeNode:
    id: str
    tenant_id: str
    kind: NodeKind
    status: ReviewStatus = ReviewStatus.CANDIDATE
    version: int = 1
    title: str = ""
    summary: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    confidence: Dict[str, float] = field(default_factory=dict)  # evidence/review/definition/freshness/...
    evidence_ref: str = ""            # links to a query / analysis id
    source_ref: str = ""              # legacy card / file / question
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    created_by: str = "system"
    reviewed_by: str = ""
    review_notes: str = ""
    supersedes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["kind"] = d["kind"].value
        d["status"] = d["status"].value
        return d


@dataclass
class PolicyDecision:
    allowed: bool
    reasons: List[str] = field(default_factory=list)
    approved_sql: str = ""

    @property
    def denied(self) -> bool:
        return not self.allowed


@dataclass
class PythonPolicyDecision:
    allowed: bool
    reasons: List[str] = field(default_factory=list)
    approved_code: str = ""

    @property
    def denied(self) -> bool:
        return not self.allowed


@dataclass
class Question:
    id: str
    tenant_id: str
    text: str
    mode_budget: str = "low_cost"
    created_at: str = field(default_factory=now_iso)


@dataclass
class AnalysisRun:
    id: str
    tenant_id: str
    trace_id: str
    question_id: str
    question_text: str
    sql: str
    dialect: str
    executor: str
    status: RunStatus = RunStatus.PLANNED
    answer_mode: Optional[AnswerMode] = None
    level: Optional[str] = None          # CP-15: "low" exploratory | "high" hypothesis/RCA
    category: Optional[str] = None       # taxonomy id (schema/fill_rate/success_trend/breakdown/.../rca/hypothesis)
    supportive_of: Optional[str] = None  # parent high-level run this low-level probe supports
    review_status: ReviewStatus = ReviewStatus.CANDIDATE
    generated_at: str = field(default_factory=now_iso)
    execution_ms: float = 0.0
    row_count: int = 0
    profile_summary: Dict[str, Any] = field(default_factory=dict)
    rule_triggers: List[Dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    facts: List[str] = field(default_factory=list)
    hypotheses: List[str] = field(default_factory=list)
    uncertainties: List[str] = field(default_factory=list)
    next_actions: List[str] = field(default_factory=list)
    insights: List[Dict[str, Any]] = field(default_factory=list)  # {text, novel, kind}
    assumptions: List[str] = field(default_factory=list)
    cost_estimate: float = 0.0
    policy_reasons: List[str] = field(default_factory=list)
    source_node_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = d["status"].value if d["status"] else d["status"]
        d["answer_mode"] = d["answer_mode"].value if d["answer_mode"] else d["answer_mode"]
        d["review_status"] = d["review_status"].value if d["review_status"] else d["review_status"]
        return d