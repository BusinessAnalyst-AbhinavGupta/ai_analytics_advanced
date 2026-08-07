"""Company Brain: governed, versioned knowledge with lifecycle + confidence."""
from .store import CompanyBrain, BrainConflict, VALID_TRANSITIONS

__all__ = ["CompanyBrain", "BrainConflict", "VALID_TRANSITIONS"]