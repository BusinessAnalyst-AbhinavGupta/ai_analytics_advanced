from neo4j import GraphDatabase

driver = GraphDatabase.driver("neo4j://127.0.0.1:7687", auth=("neo4j", "password"))
with driver.session(database="neo4j") as session:
    res = session.run("""
        MATCH (n)
        WHERE any(k in keys(n) WHERE toString(n[k]) CONTAINS 'es_events_arr_v2')
        RETURN labels(n) AS labels, n
    """)
    records = list(res)
    if records:
        print("Found nodes mentioning es_events_arr_v2:")
        for r in records:
            print(f"Labels: {r['labels']}")
            print(f"Node: {r['n']}")
    else:
        print("No nodes found containing es_events_arr_v2 in any property.")

driver.close()
