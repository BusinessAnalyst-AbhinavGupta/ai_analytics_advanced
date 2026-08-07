from typing import List, Dict, Any
import re

class SQLAnalysisEngine:
    """Analyzes raw SQL to extract logical structures independent of output format."""
    
    def __init__(self, sql: str):
        self.raw_sql = sql
        self.base_query = ""
        self.metrics = []
        self.segments = "Checkout"

    def analyze(self) -> Dict[str, Any]:
        """Extracts the logical components of a query."""
        # Identify the base CTE or common subquery
        # Using regex to find the 'base' block as it is standard in these dashboards
        base_match = re.search(r"WITH\s+base\s+\((.*)\)\s*,\s*|WITH\s+base\s*:\s*\((.*)\)", self.raw_sql, re.DOTALL | re.IGNORECASE)
        # Fallback to simpler search if standard regex fails
        if not base_match:
            base_match = list(filter(lambda x: "base" in x and "SELECT" in x, 
                                    [line for line in self.raw_sql.split('\n')]))
        
        # We find the boundaries of the 'base' block to isolate filtering logic
        # Supports both trailing commas `),` and leading commas `)\n,`
        parts = re.split(r"\)\s*,|\)\s*\n\s*,", self.raw_sql)
        main_body = parts[1] if len(parts) > 1 else self.raw_sql

        # Determine Step segments from "Case When" logic
        # This identifies calculations like `CASE WHEN page_name LIKE '%BASKET%' ...`
        distinct_steps = []
        case_statements = re.findall(r"COUNT\s*\(DISTINCT\s+CASE\s+WHEN\s+(.*?)\s+THEN\s+session_id\s+END\)", main_body, re.DOTALL | re.IGNORECASE)
        
        for item in case_statements:
            # Clean up noise like newlines and extra spaces
            cleaned = " ".join(item.split())
            distinct_steps.append({
                "logic": cleaned,
                "raw_clause": item
            })

        return {
            "base_query_block": self.raw_sql.split('base_agg')[0], # Capture the core filterer
            "metric_definitions": distinct_steps,
            "journey_stage": self.segments
        }

def parse_analytics_logic(sql: str) -> Dict[str, Any]:
    engine = SQLAnalysisEngine(sql)
    result = engine.analyze()
    return result
