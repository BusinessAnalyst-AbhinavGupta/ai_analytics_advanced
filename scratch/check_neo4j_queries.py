from core.graph_learner import GraphLearner

def inspect_queries_in_graph():
    print("🔍 Inspecting Query Knowledge stored in Neo4j...")
    learner = GraphLearner()
    
    # 1. Golden queries
    gq = learner.get_golden_queries(limit=5)
    print(f"\n1. Verified Golden Queries in Graph: {len(gq)}")
    for i, q in enumerate(gq, 1):
        print(f"   [{i}] Title: {q.get('question')} | Stage: {q.get('stage_name')}")
        sql_preview = q.get('sql', '').replace('\n', ' ')[:100]
        print(f"       SQL: {sql_preview}...")

    # 2. SQL Idioms
    idioms = learner.get_sql_idioms(limit=5)
    print(f"\n2. SQL Idioms in Graph: {len(idioms)}")
    for i, idiom in enumerate(idioms, 1):
        print(f"   [{i}] Name: {idiom.get('name')} [{idiom.get('category')}]")
        print(f"       Description: {idiom.get('description')}")

    # 3. Dynamic Aliases
    aliases = learner.get_learned_aliases(limit=6)
    print(f"\n3. Learned Column Aliases & Term Synapses: {len(aliases)}")
    for a in aliases:
        print(f"   - {a.get('alias')} -> {a.get('physical_column')} (Expression: {a.get('expression')})")

    # 4. Total Query Nodes in Neo4j
    driver = learner._get_driver()
    with driver.session(database=learner.database) as session:
        res = session.run("""
            MATCH (n)
            RETURN labels(n) as label, count(n) as count
        """).data()
        print("\n4. Neo4j Node Counts:")
        for r in res:
            print(f"   - {r['label']}: {r['count']}")
    driver.close()

if __name__ == "__main__":
    inspect_queries_in_graph()
