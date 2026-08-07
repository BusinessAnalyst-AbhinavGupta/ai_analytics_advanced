from neo4j import GraphDatabase

driver = GraphDatabase.driver("neo4j://127.0.0.1:7687", auth=("neo4j", "password"))
with driver.session(database="neo4j") as session:
    res = session.run("""
        MATCH (c:Column)
        WHERE c.table_name IS NOT NULL
        WITH DISTINCT c.table_name AS col_tbl
        OPTIONAL MATCH (t:Table {name: col_tbl})
        WHERE t IS NULL
        RETURN col_tbl
    """)
    records = list(res)
    missing_tables = [r["col_tbl"] for r in records if r["col_tbl"] is not None]
    
    if missing_tables:
        print("Root tables found on Columns but MISSING a Table node:")
        for tbl in set(missing_tables):
            print(f" - {tbl}")
    else:
        print("No other missing root tables found based on Column nodes.")

driver.close()
