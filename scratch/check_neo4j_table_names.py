from neo4j import GraphDatabase

driver = GraphDatabase.driver("neo4j://127.0.0.1:7687", auth=("neo4j", "password"))
with driver.session(database="neo4j") as session:
    res = session.run("""
        MATCH (t:Table)
        RETURN t.name AS name, size(t.name) AS name_len
    """)
    for r in res:
        print(f"Table name: '{r['name']}', Length: {r['name_len']}")

driver.close()
