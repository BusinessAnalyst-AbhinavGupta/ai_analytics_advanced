import json
import re

with open("extracted_data/metabase_tab_1236_cards_sql.json", "r") as f:
    data = json.load(f)

cards = data.get("extracted_cards", [])

physical_tables = {}

def strip_sql_comments(sql_text):
    # Remove block comments /* ... */
    sql_text = re.sub(r"/\*.*?\*/", "", sql_text, flags=re.DOTALL)
    # Remove line comments -- ...
    sql_text = re.sub(r"--[^\n]*", "", sql_text)
    return sql_text

for c in cards:
    sql = c.get("sql", "")
    if not sql:
        continue
    
    cleaned_sql = strip_sql_comments(sql)
    
    # Find all CTE names
    cte_matches = re.findall(r"(?:WITH|,)\s*([a-zA-Z0-9_]+)\s+AS\s*\(", cleaned_sql, re.IGNORECASE)
    ctes = set(m.lower() for m in cte_matches)
    
    # Find all FROM and JOIN clauses
    from_join = re.findall(r"(?:FROM|JOIN)\s+([a-zA-Z0-9_\.]+)", cleaned_sql, re.IGNORECASE)
    for tbl in from_join:
        tbl_clean = tbl.strip()
        tbl_lower = tbl_clean.lower()
        if tbl_lower in ctes or tbl_lower in ("select", "unnest", "lateral", "values", "json_table"):
            continue
        if tbl_clean not in physical_tables:
            physical_tables[tbl_clean] = []
        physical_tables[tbl_clean].append(c.get("card_id"))

print("=" * 60)
print(f"VERIFIED PHYSICAL BASE TABLES ACROSS ALL {len(cards)} QUERIES:")
print("=" * 60)
for tbl, card_ids in physical_tables.items():
    print(f"  • Table: '{tbl}' (Referenced in {len(card_ids)} questions: {card_ids[:5]}...)")
