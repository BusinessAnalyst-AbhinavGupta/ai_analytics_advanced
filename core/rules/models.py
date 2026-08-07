"""
Domain Models for Business Validation & Observation Layer
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Any, Optional


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    WARNING = "WARNING"
    MEDIUM = "MEDIUM"
    INFO = "INFO"


@dataclass
class Observation:
    """A business observation generated when a business rule condition triggers."""
    rule_id: str
    rule_name: str
    category: str
    severity: Severity
    target_column: Optional[str]
    observation_text: str
    context_data: Dict[str, Any] = field(default_factory=dict)
    hypotheses: List[str] = field(default_factory=list)
    recommended_checks: List[str] = field(default_factory=list)
    confidence: str = "MEDIUM"


@dataclass
class RuleEvaluationResult:
    """Summary container of all evaluated business rules on a dataset."""
    total_rules_evaluated: int
    triggered_count: int
    observations: List[Observation] = field(default_factory=list)
    critical_count: int = 0
    warning_count: int = 0
    info_count: int = 0
