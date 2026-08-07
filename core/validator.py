from typing import List, Tuple
from schema.models import CanonicalKnowledge

class ValidationError(Exception):
    """Exception raised when a parsed object fails quality audits."""
    pass

class Validator:
    """
    Validation Layer: Ensures the internal representation (Canonical Knowledge) 
    is consistent and complete before it is allowed to be pushed into any database.
    """
    @staticmethod
    def validate(data: CanonicalKnowledge) -> Tuple[bool, List[str]]:
        warnings = []
        is_valid = True

        # Integrity Checks
        if not data.name_canonical or len(data.name_canonical) < 3:
            warnings.append("WARNING: canonical name is too short or missing.")

        # Check if formula is set
        if not data.logic or not data.logic.formula:
            warnings.append("CRITICAL: No metric logic was extracted from the source SQL.")
            is_valid = False

        # Confidence thresholds
        if data.confidence_score < 0.9:
            warnings.append(f"WARNING: Low confidence score ({data.confidence_score}) - Verify manual review.")

        # Check for essential attributes
        if not data.description:
            warnings.append("Warning: Missing business description.")

        return is_valid, warnings
