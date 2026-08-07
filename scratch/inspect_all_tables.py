from core.graph_learner import GraphLearner

def inspect_all_tables():
    learner = GraphLearner()
    driver = learner._get_driver()
    with driver.session(database=learner.database) as session:
        res = session.run("MATCH (t:Table) RETURN t.name as name, t.database as db, t.row_count as rows").data()
        print("All Table nodes in Neo4j:")
        for r in res:
            print(f"  - {r['name']} (DB: {r['db']})")
    driver.close()

if __name__ == "__main__":
    inspect_all_tables()
