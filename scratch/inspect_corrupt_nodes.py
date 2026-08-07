from core.graph_learner import GraphLearner

def inspect_and_clean_neo4j():
    learner = GraphLearner()
    driver = learner._get_driver()
    with driver.session(database=learner.database) as session:
        # Check Tables
        all_tables = session.run("MATCH (t:Table) RETURN t.name as name, t.database as db, [(t)-[:HAS_COLUMN]->(c) | c.name] as cols").data()
        print(f"Total Table nodes: {len(all_tables)}")
        spurious_tables = []
        valid_tables = []
        for t in all_tables:
            if not t["db"] and len(t["cols"]) == 0:
                spurious_tables.append(t["name"])
            else:
                valid_tables.append((t["name"], t["db"], len(t["cols"])))
                
        print(f"Valid Tables ({len(valid_tables)}):")
        for vt in valid_tables:
            print(f"  ✅ {vt[0]} (DB: {vt[1]}, Columns: {vt[2]})")
            
        print(f"Spurious Tables to purge ({len(spurious_tables)}):")
        print(f"  ❌ {spurious_tables}")
        
        # Check Learned Rules
        all_rules = session.run("MATCH (r:LearnedRule) RETURN id(r) as node_id, r.id as id, r.rule_text as text, r.table_name as table").data()
        print(f"\nTotal LearnedRule nodes: {len(all_rules)}")
        null_rules = [r["node_id"] for r in all_rules if not r.get("text")]
        valid_rules = [r for r in all_rules if r.get("text")]
        print(f"Null/Corrupt Learned Rules count: {len(null_rules)}")
        print(f"Valid Learned Rules count: {len(valid_rules)}")
        for vr in valid_rules:
            print(f"  ✅ ID: {vr.get('id')} | Table: {vr.get('table')} | Text: {vr.get('text')}")
            
        # Check Golden Queries
        all_gq = session.run("MATCH (g:GoldenQuery) RETURN g.id as id, g.question as q, g.journey_stage as stage, g.tables as tables").data()
        print(f"\nTotal GoldenQuery nodes: {len(all_gq)}")
        for g in all_gq:
            print(f"  ⭐ ID: {g.get('id')} | Stage: {g.get('stage')} | Q: {g.get('q')}")

    driver.close()

if __name__ == "__main__":
    inspect_and_clean_neo4j()
