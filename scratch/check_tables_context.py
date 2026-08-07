import json
import re

with open("extracted_data/metabase_tab_1236_cards_sql.json", "r") as f:
    data = json.load(f)

cards = data.get("extracted_cards", [])

for c in cards:
    sql = c.get("sql", "")
    for match in re.finditer(r"(?:FROM|JOIN)\s+([a-zA-Z0-9_\.]+)", sql, re.IGNORECASE):
        tbl = match.group(1)
        if tbl.lower() in ("eshop_data.es_events_v2", "natco_code", "purchase_flags"):
            print(f"Card #{c['card_id']} ({c['name']}) has: {match.group(0)}")
            # Show context
            start = max(0, match.start() - 40)
            end = min(len(sql), match.end() + 40)
            print("  Context:", sql[start:end].replace('\n', ' '))
