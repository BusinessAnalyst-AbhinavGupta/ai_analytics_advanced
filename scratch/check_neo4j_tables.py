from neo4j import GraphDatabase
import os

driver = GraphDatabase.driver("neo4j://127.0.0.1:7687", auth=("neo4j", "password"))
with driver.session(database="neo4j") as session:
    res = session.run("""
        MATCH (t:Table)
        RETURN t.name AS table_name, t.database_name AS database_name, t.schema_name AS schema_name
        ORDER BY t.database_name, t.schema_name, t.name
    """)
    print("Tables currently aware of in Neo4j:")
    for row in res:
        db = row.get("database_name", "")
        sch = row.get("schema_name", "")
        tbl = row.get("table_name", "")
        fqn = tbl
        if sch:
            fqn = f"{sch}.{fqn}"
        if db:
            fqn = f"{db}.{fqn}"
        print(f"- {fqn} (Node Name: {tbl})")

driver.close()
