from typing import List, Optional, Dict, Any
from enum import Enum
from pydantic import BaseModel, Field
from uuid import UUID

class JourneyStage(str, Enum):
    ACQUISITION = "Acquisition"
    DISCOVERY = "Discovery"
    CART = "Cart"
    CHECKOUT = "Checkout"
    SHARED = "Shared"

class MetricType(str, Enum):
    COUNT = "Count"
    SUM = "Sum"
    RATIO = "Ratio"
    AVERAGE = "Average"
    BOOLEAN = "Boolean"

class CanonicalEntity(BaseModel):
    """The identity of a business concept or metric."""
    id: UUID
    name_canonical: str
    labels: List[str] = []
    owner_group: str
    description: str

class LogicalDetail(BaseModel):
    """Details about how the data is calculated or grouped."""
    type: MetricType
    formula: str
    granularity: str
    is_derived: bool = False
    parent_metrics: List[str] = []

class SourceMapping(BaseModel):
    """The physical representation of the query content (SQL-lite)."""
    sql_reference_id: Optional[str] = None
    table_map: Dict[str, str] = {} # Alias -> Table Name
    query_filters: List[str] = []
    grouping_columns: List[str] = []
    # This keeps it independent of Neo4j while capturing the 'how'
    joins: List[str] = [] 

class CanonicalKnowledge(BaseModel):
    """The ultimate source of truth for any business query/metric."""
    id: UUID
    name_canonical: str
    journey_stage: JourneyStage
    description: str
    owner: str
    tags: List[str] = []
    
    # Logic & Analysis Details
    logic: LogicalDetail
    source_mapping: SourceMapping
    
    # Metadata for documentation & lineage
    metadata: Dict[str, Any] = {} 
    confidence_score: float = 1.0
    warnings: List[str] = []

class ColumnUsageContext(BaseModel):
    """Deep contextual role and predicate pattern for a specific column."""
    column_name: str
    role: str # e.g. "Funnel Step Action Identification", "Country Traffic Isolation", "Error Status"
    predicate_pattern: str # e.g. "action = 'contentFillOut' AND attr_form_name = 'registration form'"
    importance_weight: float = Field(default=0.8, ge=0.0, le=1.0)
    reasoning: str # Why this column and predicate are required for this business problem

class SqlIdiom(BaseModel):
    """Reusable structural SQL technique or design pattern."""
    name: str # e.g. "Session-Level Event Flagging CTE", "Funnel Step Union Aggregation"
    category: str # e.g. "Funnel Analysis", "Drop-off Attribution", "Error Diagnosis"
    description: str
    sql_skeleton: str
    when_to_use: str

class LearnedDomainRule(BaseModel):
    """Anti-pattern prevention, business constraint, or disambiguation rule."""
    rule_type: str # "DISAMBIGUATION", "FILTER_CONSTRAINT", "ANTI_PATTERN", "COMPUTED_METRIC"
    description: str
    reasoning: str
    example_sql: Optional[str] = None

class ColumnAlias(BaseModel):
    """Dynamically learned business alias or shorthand for a physical database column or expression."""
    physical_column: str
    alias: str
    expression: Optional[str] = None
    table_name: Optional[str] = None
    reasoning: Optional[str] = None

class DeepSqlReasoning(BaseModel):
    """Complete structured intelligence extracted from a query and its business context."""
    intent_name: str
    journey_stage: str
    business_goal: str
    reasoning_summary: str
    primary_table: str = "eshop_data.es_events_v2"
    root_tables: List[str] = Field(default_factory=lambda: ["eshop_data.es_events_v2"])
    column_usages: List[ColumnUsageContext] = []
    column_aliases: List[ColumnAlias] = []
    sql_idioms: List[SqlIdiom] = []
    learned_rules: List[LearnedDomainRule] = []
    extracted_metrics: List[Dict[str, Any]] = []
    canonical_golden_query: str
    dialect: str = "AWS Athena / Presto"

# To be used by the adapters later
class RawExtractionResult(BaseModel):
    """Raw results from either SQL Analysis or Metadata analysis."""
    data: Dict[str, Any]
    source_type: str  # "SQL" or "Metadata"
