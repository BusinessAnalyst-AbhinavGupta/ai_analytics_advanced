from core.graph_learner import GraphLearner

def cleanup_neo4j():
    learner = GraphLearner()
    driver = learner._get_driver()
    with driver.session(database=learner.database) as session:
        # 1. Delete spurious Table nodes without columns or database
        del_tbl_res = session.run("""
            MATCH (t:Table)
            WHERE t.database IS NULL AND NOT (t)-[:HAS_COLUMN]->()
            DETACH DELETE t
            RETURN count(t) as deleted_tables
        """).single()
        print(f"🧹 Deleted spurious Table nodes: {del_tbl_res['deleted_tables']}")
        
        # 2. Delete empty/corrupt LearnedRule nodes
        del_rule_res = session.run("""
            MATCH (r:LearnedRule)
            WHERE r.description IS NULL AND r.rule_text IS NULL
            DETACH DELETE r
            RETURN count(r) as deleted_rules
        """).single()
        print(f"🧹 Deleted empty LearnedRule nodes: {del_rule_res['deleted_rules']}")

        # 3. Verify remaining physical tables
        rem_tables = session.run("""
            MATCH (t:Table)
            RETURN t.name as name, t.database as db, [(t)-[:HAS_COLUMN]->(c) | c.name] as cols
        """).data()
        print(f"\nRemaining verified Table nodes ({len(rem_tables)}):")
        for rt in rem_tables:
            print(f"  ✅ {rt['name']} (DB: {rt['db']}, {len(rt['cols'])} columns)")

    driver.close()

if __name__ == "__main__":
    cleanup_neo4j()
