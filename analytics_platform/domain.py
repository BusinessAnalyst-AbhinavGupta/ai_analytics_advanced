"""Typed domain models shared across the platform."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field, fields
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


# -- the semantic + physical layer the analyst reads before writing SQL -------

PROFILE_CARDINALITY_CAP = 50     # <= this many distinct values -> store them all
PROFILE_TOP_VALUES = 20          # above the cap -> store this many, by frequency


@dataclass
class ColumnProfile:
    """What a column actually contains, measured rather than assumed.

    `distinct_count` is load-bearing beyond the prompt: it is the input to the
    cube cell-count guard, so an *absent* profile must stay distinguishable from
    one measured as low-cardinality. Absent profiles are absent, never defaulted
    to zero.
    """

    column: str
    dtype: str
    distinct_count: int
    null_fraction: float
    values: List[str]            # complete when values_complete, else top-N by frequency
    values_complete: bool        # distinct_count <= PROFILE_CARDINALITY_CAP AND sample not saturated
    min_value: str = ""          # populated for numeric / date / datetime columns
    max_value: str = ""
    profiled_at: str = ""
    # For each candidate grain key in this table, the share of keys carrying more
    # than one distinct value of THIS column. 0.0 means it is safe to carry onto
    # that grain as-is; anything above 0 means it needs an attribution rule.
    fanout_by_key: Dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ColumnProfile":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class AttributionRule:
    """How a multi-valued categorical collapses onto a grain key.

    This is a property of the base *population*, not of a question. Letting each
    question re-derive it means two questions can apply two rankings to the same
    sessions and produce two defensible-looking, mutually contradictory numbers
    -- so it lives inside the base view and inside its population_hash.
    """

    column: str                  # the multi-valued categorical, e.g. "service_line"
    grain: List[str] = field(default_factory=list)   # the key it collapses onto
    strategy: str = "most_frequent"   # highest_intent | most_frequent | latest | first
    priority_values: List[str] = field(default_factory=list)  # ranked, best first
    tiebreakers: List[str] = field(default_factory=list)      # ["event_count DESC", ...]
    source: str = ""             # "brain" (approved) | "llm" (proposed) | "default"
    rationale: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AttributionRule":
        return _typed_from_dict(cls, d)


@dataclass
class BaseView:
    """A governed ID-grain row population, inlined verbatim as a CTE.

    Athena here is read-only, so this is the client-side substitute for
    CREATE VIEW. The grain is an *identifier* grain, never a dimensional one: at
    session_id every question is a projection of one population, and a new
    dimension is a column added above an unchanged base rather than a rewrite.
    """

    name: str                       # "checkout_sessions"
    grain: List[str] = field(default_factory=list)   # ["session_id"] -- ID grain
    source_sql: str = ""            # the population: FROM/JOIN/WHERE, one row per grain key
    dimension_columns: List[str] = field(default_factory=list)  # legal GROUP BY columns
    measure_columns: List[str] = field(default_factory=list)
    attributions: List[AttributionRule] = field(default_factory=list)
    time_column: str = ""
    row_count_estimate: int = 0
    description: str = ""
    owner: str = ""
    aliases: List[str] = field(default_factory=list)
    # -- grain verification (Task 13) ---------------------------------------
    # Measured once per population_hash by an actual COUNT(*) vs COUNT(DISTINCT
    # grain) probe, never asserted by whoever wrote the SQL. A cube over an
    # unverified base is refused: GROUP BY deduplicates the dimension tuple
    # unconditionally, so a base emitting three rows per session_id yields a cube
    # where every cell is unique and every SUM is silently tripled. The stored
    # hash is what makes an edited source_sql re-probe automatically.
    grain_verified: bool = False
    grain_violation_ratio: float = 0.0
    # Rows whose grain key is NULL. Counted separately because they are NOT
    # duplicates: COUNT(DISTINCT k) ignores NULLs while GROUP BY k gives them a
    # group of their own, so they present exactly like fan-out and send whoever
    # reads the caveat hunting for duplicate keys that do not exist.
    grain_null_keys: int = 0
    grain_checked_at: str = ""
    grain_checked_hash: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BaseView":
        rules = d.get("attributions") or []
        if not isinstance(rules, list):
            raise ValueError("BaseView.attributions must be a list")
        view = _typed_from_dict(cls, {k: v for k, v in d.items() if k != "attributions"})
        view.attributions = [r if isinstance(r, AttributionRule) else AttributionRule.from_dict(r)
                             for r in rules]
        return view


@dataclass
class AnalysisArtifact:
    """The provenance record for one analytical turn.

    This is what makes an answer *reproducible* rather than merely plausible:
    which population it was computed over, which slice of it, what was reused,
    what ran where, and what was assumed. Build it incrementally through the
    turn rather than reconstructing it at the end from whatever happens to still
    be in scope -- a field filled from a stale local is worse than an empty one.

    `population_hash` is the field that has to be right. Everything else here is
    documentation; that one is a claim, and the reconcile endpoint lets a user
    act on it.
    """

    question: str = ""
    plan_rationale: str = ""
    # -- the population: what makes this answer comparable to another --------
    base_view: str = ""              # "" on the aggregate path
    population_hash: str = ""        # "" on the aggregate path: reconciles with nothing
    projection_hash: str = ""
    base_view_approved: bool = False       # False -> the figures are provisional
    base_view_grain_verified: bool = False  # from the Task 13 probe
    reconcilable: bool = False       # a population_hash exists AND the grain was verified
    slice_filters: Dict[str, List[str]] = field(default_factory=dict)  # NOT hashed
    dimensions: List[str] = field(default_factory=list)
    non_additive: List[str] = field(default_factory=list)
    supersedes: str = ""             # a narrower cube this turn replaced
    # -- the rest of the turn ------------------------------------------------
    semantics_used: List[str] = field(default_factory=list)
    unresolved_terms: List[str] = field(default_factory=list)
    requirement: Dict[str, Any] = field(default_factory=dict)
    coverage: Dict[str, Any] = field(default_factory=dict)
    datasets_used: List[str] = field(default_factory=list)
    warehouse_sql: List[str] = field(default_factory=list)
    workspace_sql: List[str] = field(default_factory=list)   # DuckDB, run locally
    python_code: List[str] = field(default_factory=list)
    result_summary: Any = None
    chart_spec: Optional[Dict[str, Any]] = None
    key_findings: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CubeMeasure:
    name: str            # "revenue"
    expr: str = ""       # what goes in the cube's SELECT: "SUM(revenue)"
    additive: bool = True   # can this roll up from the cube to a coarser grain?
    read_expr: str = ""  # how to read it back, when it differs from `name`


@dataclass
class CubeSpec:
    """What cut of a base view a question needs. `filters` is the SLICE -- it is
    recorded but deliberately NOT hashed, which is what makes 'question A
    filtered to Germany' and 'question B unfiltered' reconcilable."""

    base_name: str
    dimensions: List[str] = field(default_factory=list)
    measures: List[CubeMeasure] = field(default_factory=list)
    filters: Dict[str, List[str]] = field(default_factory=dict)
    time_column: str = ""
    time_start: str = ""
    time_end: str = ""


@dataclass
class CubeSQL:
    ok: bool
    sql: str = ""
    population_hash: str = ""
    projection_hash: str = ""
    estimated_cells: int = 0
    measures: List[CubeMeasure] = field(default_factory=list)   # after the AVG rewrite
    columns: List[str] = field(default_factory=list)
    non_additive: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    error: str = ""                 # set when the guard refuses
    offending_dimensions: List[str] = field(default_factory=list)


@dataclass
class TurnPlan:
    """What this turn decided, and who decided each part.

    The LLM picks the population and states the cut it needs. Everything else --
    whether the base is valid, what its population_hash is, whether the cube
    fits, whether the workspace already covers it -- is decided in code. `path`
    comes from the DataManager's verdict, never from the model.
    """

    path: str = "aggregate"       # reuse | widen | retrieve | aggregate
    analysis: str = "python"      # workspace_sql | python -- how to compute, LLM's choice
    # Did the QUESTION name a time window, as opposed to the planner inferring
    # one? Defaults True so a planner that never reports it behaves exactly as
    # before -- a missing field must not turn every turn into an interrogation.
    timeframe_stated: bool = True
    df_label: str = ""            # the cube to compute over, from the verdict
    base_view: Optional["BaseView"] = None
    base_view_approved: bool = False      # False -> the answer is provisional
    cube: Optional["CubeSpec"] = None     # what the LLM asked for
    cube_sql: Optional["CubeSQL"] = None  # composed + hashed + guarded
    requirement: Optional[Any] = None     # DataRequirement handed to the DataManager
    verdict: Optional[Any] = None         # CoverageVerdict it returned
    grain: List[str] = field(default_factory=list)
    dimensions: List[str] = field(default_factory=list)
    measures: List[CubeMeasure] = field(default_factory=list)
    # The profiles the cube was sized against. Carried so a widen can RE-compose
    # against the same real cardinalities -- recomposing against a permissive
    # stand-in would silently under-estimate the cell count and skip paging.
    profiles: Dict[str, Any] = field(default_factory=dict)
    time_window: str = ""
    rationale: str = ""
    caveats: List[str] = field(default_factory=list)
    # Proposed only when the planner is authoring a NEW base view this turn; on an
    # existing base these are already baked in and inherited.
    attributions: List[AttributionRule] = field(default_factory=list)


@dataclass
class ReconcileResult:
    same_population: bool
    population_hash_a: str = ""
    population_hash_b: str = ""
    measure: str = ""
    value_a: Optional[float] = None
    value_b: Optional[float] = None
    agrees: bool = False
    explanation: str = ""           # written for a human; lands in the API response


@dataclass
class SemanticMetric:
    """What a metric MEANS -- not where its columns live.

    `filters` is the load-bearing field: those predicates are applied to every
    query touching this metric whether or not the user mentioned them, which is
    why they belong inside a governed base view rather than being bolted on as a
    per-question slice.
    """

    name: str                       # "conversion_rate"
    definition: str = ""            # "completed_applications / eligible_applications"
    grain: List[str] = field(default_factory=list)          # the grain it is valid at
    dimensions: List[str] = field(default_factory=list)     # what it may be sliced by
    source_tables: List[str] = field(default_factory=list)
    filters: List[str] = field(default_factory=list)        # ALWAYS applied
    caveats: List[str] = field(default_factory=list)
    freshness: str = ""             # "daily, T+1"
    owner: str = ""
    aliases: List[str] = field(default_factory=list)        # "CVR", "conversion"

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SemanticMetric":
        return _typed_from_dict(cls, d)


@dataclass
class SemanticDimension:
    name: str
    column: str = ""
    source_tables: List[str] = field(default_factory=list)
    description: str = ""
    values: List[str] = field(default_factory=list)   # from the column profile when known
    aliases: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SemanticDimension":
        return _typed_from_dict(cls, d)


def _typed_from_dict(cls, d: Dict[str, Any]):
    """Build a dataclass from a stored payload, dropping unknown keys and
    rejecting values whose type does not match the declared one.

    A hand-edited or older-schema node must degrade to 'not usable' rather than
    producing a metric whose `grain` is the string "session_id" -- which would
    iterate as characters and silently corrupt every downstream decision.
    """
    out: Dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in d:
            continue
        value = d[f.name]
        declared = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", "")
        if declared.startswith("List[") and not isinstance(value, list):
            raise ValueError(f"{cls.__name__}.{f.name} must be a list, got {type(value).__name__}")
        if declared.startswith("Dict[") and not isinstance(value, dict):
            raise ValueError(f"{cls.__name__}.{f.name} must be a dict, got {type(value).__name__}")
        if declared == "str" and not isinstance(value, str):
            raise ValueError(f"{cls.__name__}.{f.name} must be a str, got {type(value).__name__}")
        out[f.name] = value
    return cls(**out)


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