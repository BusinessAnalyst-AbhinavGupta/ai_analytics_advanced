from neo4j import GraphDatabase

driver = GraphDatabase.driver("neo4j://127.0.0.1:7687", auth=("neo4j", "password"))
with driver.session(database="neo4j") as session:
    res = session.run("""
        MATCH (c:Column)
        WHERE c.table_name IS NOT NULL
        WITH DISTINCT trim(c.table_name) AS col_tbl
        OPTIONAL MATCH (t:Table {name: col_tbl})
        WHERE t IS NULL
        RETURN col_tbl
    """)
    records = list(res)
    missing_tables = [r["col_tbl"] for r in records if r["col_tbl"] is not None]
    
    physical_misses = [tbl for tbl in missing_tables if "." in tbl or "es_events" in tbl or "t_link" in tbl]
    cte_misses = [tbl for tbl in missing_tables if "." not in tbl and "es_events" not in tbl and "t_link" not in tbl]
    
    print("Actual Database Tables missing a Table Node:")
    for tbl in set(physical_misses):
        print(f" - {tbl}")
        
    print(f"\\n(Plus {len(set(cte_misses))} CTEs/Aliases incorrectly identified as table sources by the LLM, e.g., {', '.join(list(set(cte_misses))[:5])})")

driver.close()
