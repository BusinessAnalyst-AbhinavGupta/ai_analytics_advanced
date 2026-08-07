from typing import List, Dict, Any, Optional
from dataclasses import dataclass

@dataclass
class CanonicalKnowledge:
    id: str
    name_canonical: str
    description: str
    journey_stage: str
    logic_type: str
    metrics: List[dict]
    base_filters: List[str]
    confidence: float = 1.0

class Normalizer:
    def __init__(self):
        # Standardize naming across different analyst inputs
        self.synonyms = {
            "basket": ["cart", "bag"],
            "checkout": ["check_out", "payment"]
        }

    def normalize(self, name: str) -> str:
        name_lower = name.lower()
        for standard, aliases in self.synonyms.items():
            if name_lower == standard or any(a in name_lower for a in aliases):
                return standard
        return name

    def process_metrics(self, raw_metrics: List[dict]) -> List[dict]:
        processed = []
        for m in raw_metrics:
            norm_name = self.normalize(m['name'])
            processed.append({
                "name": norm_name,
                "logic": m['logic']
            })
        return processed
