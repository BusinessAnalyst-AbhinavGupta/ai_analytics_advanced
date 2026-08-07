from neo4j import GraphDatabase

driver = GraphDatabase.driver("neo4j://127.0.0.1:7687", auth=("neo4j", "password"))
with driver.session(database="neo4j") as session:
    res = session.run("""
        MATCH (t:Table)
        OPTIONAL MATCH (intent:BusinessIntent)-[:TARGETS_TABLE]->(t)
        OPTIONAL MATCH (gq:GoldenQuery)-[:USES_TABLE]->(t)
        RETURN t.name AS table_name, 
               count(DISTINCT intent) AS intent_count, 
               count(DISTINCT gq) AS golden_query_count
    """)
    records = list(res)
    print("Tables and their usage in Queries (Business Intents / Golden Queries):")
    for r in records:
        print(f" - {r['table_name']}: {r['intent_count']} Business Intents, {r['golden_query_count']} Golden Queries")

driver.close()
