import json
import re

with open("extracted_data/metabase_tab_1236_cards_sql.json", "r") as f:
    data = json.load(f)

cards = data.get("extracted_cards", [])

all_base_tables = set()
card_table_mapping = []

for c in cards:
    sql = c.get("sql", "")
    if not sql:
        continue
    
    # Extract CTE names defined via "WITH cte_name AS (" or ", cte_name AS ("
    cte_pattern = r"(?:WITH|,)\s*([a-zA-Z0-9_]+)\s+AS\s*\("
    ctes = set(m.lower() for m in re.findall(cte_pattern, sql, re.IGNORECASE))
    
    # Extract FROM / JOIN targets
    from_join_pattern = r"(?:FROM|JOIN)\s+([a-zA-Z0-9_\.]+)"
    raw_tables = re.findall(from_join_pattern, sql, re.IGNORECASE)
    
    tables_for_card = set()
    for t in raw_tables:
        t_clean = t.strip().lower()
        if t_clean not in ctes and not t_clean.startswith("(") and t_clean not in ("select", "unnest", "lateral", "values"):
            # If it's qualified with schema (e.g. silver_layer.xxx or bronze.xxx or catalog.xxx)
            tables_for_card.add(t.strip())
            all_base_tables.add(t.strip())
            
    card_table_mapping.append({
        "card_id": c.get("card_id"),
        "name": c.get("name"),
        "ctes": list(ctes),
        "base_tables": list(tables_for_card)
    })

print("=" * 60)
print(f"IDENTIFIED BASE TABLES ACROSS ALL {len(cards)} QUERIES:")
print("=" * 60)
for t in sorted(all_base_tables):
    print(f"  • {t}")

print("\n--- SAMPLE CARD TO BASE TABLE MAPPING ---")
for m in card_table_mapping[:8]:
    print(f"Card #{m['card_id']} \"{m['name']}\":")
    print(f"   CTEs: {m['ctes']}")
    print(f"   Base Tables: {m['base_tables']}")
