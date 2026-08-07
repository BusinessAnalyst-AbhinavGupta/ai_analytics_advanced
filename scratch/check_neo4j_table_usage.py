from neo4j import GraphDatabase

driver = GraphDatabase.driver("neo4j://127.0.0.1:7687", auth=("neo4j", "password"))
with driver.session(database="neo4j") as session:
    # Look for Tables connected to Queries, Dashboards, or Metrics
    res = session.run("""
        MATCH (t:Table)-[r]-(other)
        WHERE 'Metric' IN labels(other) OR 'Query' IN labels(other) OR 'Dashboard' IN labels(other) OR 'Card' IN labels(other) OR 'Stage' IN labels(other)
        RETURN DISTINCT t.name AS table_name, labels(other) AS used_in_labels
    """)
    records = list(res)
    if records:
        print("Tables explicitly used in queries/metrics/stages:")
        for r in records:
            print(f" - {r['table_name']} (Used in {r['used_in_labels']})")
    else:
        print("No explicit relationships found to Metric/Query/Card/Stage.")
        
        # Alternatively, let's just count how many columns of each table are linked to metrics
        res2 = session.run("""
            MATCH (t:Table)-[:HAS_COLUMN]->(c:Column)-[r]-(m:Metric)
            RETURN DISTINCT t.name AS table_name
        """)
        rec2 = list(res2)
        if rec2:
            print("Tables with columns used in Metrics:")
            for r in rec2:
                print(f" - {r['table_name']}")
        else:
            print("Could not find any indirect usage via columns either.")

driver.close()
