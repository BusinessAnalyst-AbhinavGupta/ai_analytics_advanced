from typing import List, Dict, Any
from schema.models import CanonicalKnowledge, LogicalDetail, SourceMapping

class KnowledgeNormalizer:
    """Ensures that variations in naming are resolved to core business entities."""
    
    def __init__(self):
        # This would typically be loaded from a config or DB
        self.synonyms = {
            "conversion": ["cvr", "%", "rate"],
            "checkout": ["check_out", "pay_page"],
            "basket": ["cart", "bag"]
        }

    def normalize_name(self, name: str) -> str:
        """Normales names into a standard format."""
        name_lower = name.lower()
        for standard, aliases in self.synonyms.items():
            if name_lower in [standard] + aliases or any(a in name_lower for a in aliases):
                return standard
        return name

    def process(self, raw_metrics: List[Dict]) -> List[dict]:
        """Translates parsed metrics into normalized items."""
        normalized = []
        for m in raw_metrics:
            name = self.normalize_name(m['name'])
            # Logic remains untouched because it is technically a different layer, 
            # but we ensure the naming is consistent for reporting.
            normalized.append({
                "name": name,
                "logic": m['raw_logic']
            })
        return normalized

def build_canonical(parsed_data: Dict[str, Any], metadata: Dict[str, Any] = None) -> CanonicalKnowledge:
    """Final assembly of the Canonical Knowledge JSON."""
    from uuid import uuid4
    from schema.models import JourneyStage
    
    if metadata is None:
        metadata = {}
        
    # 1. Extract Core Data
    metrics = parsed_data.get("extracted_metrics", [])
    core_data = parsed_data.get("common_context", {})
    
    # 2. Process Metrics through Normalizer
    normalizer = KnowledgeNormalizer()
    normalized_metrics = normalizer.process(metrics)

    # 3. Determine Journey Stage
    raw_stage = metadata.get("journey_stage") or metadata.get("journey_stage_or_page") or "Checkout"
    matched_stage = JourneyStage.SHARED
    for stage_enum in JourneyStage:
        if stage_enum.value.lower() == str(raw_stage).strip().lower():
            matched_stage = stage_enum
            break

    # 4. Parse Tags
    raw_tags = metadata.get("tags", [])
    if isinstance(raw_tags, str):
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    elif isinstance(raw_tags, list):
        tags = [str(t).strip() for t in raw_tags if str(t).strip()]
    else:
        tags = ["Primary"]
    if not tags:
        tags = ["Primary"]

    # 5. Extract metadata fields
    service_line = metadata.get("service_line", "General")
    category = metadata.get("category", "Analytics")
    natco = metadata.get("natco", "Global")
    owner = metadata.get("owner", "Analytics Team")
    description = metadata.get("description") or f"Analytics metric for {service_line} ({category}) in {natco}"
    
    name_canonical = "Checkout Funnel"
    if metrics:
        name_canonical = metrics[0].get("name", "Analytics Metric")

    # 6. Construct the final model
    return CanonicalKnowledge(
        id=uuid4(),
        name_canonical=name_canonical,
        journey_stage=matched_stage,
        description=description,
        owner=owner,
        tags=tags,
        logic=LogicalDetail(
            type="Count", # Defaulting as the query returns a list of count metrics
            formula="SELECT ... FROM base",
            granularity="None"
        ),
        source_mapping=SourceMapping(
            table_map={},
            query_filters=[f"Rule: {r}" for r in core_data.get("base_filters", [])]
        ),
        metadata={
            "service_line": service_line,
            "category": category,
            "natco": natco,
            "journey_stage_or_page": raw_stage,
            "user_metadata": metadata
        }
    )

