from core.graph_learner import GraphLearner

def inspect_checkout_actions():
    learner = GraphLearner()
    driver = learner._get_driver()
    with driver.session(database=learner.database) as session:
        # Check sample values of action in es_events_v2 and silver table
        res = session.run("""
            MATCH (t:Table)-[:HAS_COLUMN]->(c:Column)
            WHERE c.name IN ['action', 'page_name', 'label', 'stage', 'internalemployee', 'identifiers_log_time', 'event_date']
            RETURN t.name as table_name, c.name as col_name, c.sample_values as samples
        """).data()
        print("Column sample values:")
        for r in res:
            print(f"Table: {r['table_name']} | Col: {r['col_name']} | Samples: {r['samples']}")
            
        # Check golden queries or idioms related to login or checkout
        gq = session.run("""
            MATCH (gq:VerifiedGoldenQuery)
            WHERE toLower(gq.name) CONTAINS 'login' OR toLower(gq.sql) CONTAINS 'login' OR toLower(gq.name) CONTAINS 'checkout'
            RETURN gq.name as name, gq.sql as sql
            LIMIT 5
        """).data()
        print(f"\nMatching Golden Queries in Graph: {len(gq)}")
        for g in gq:
            print(f"--- Golden Query: {g['name']} ---")
            print(g['sql'][:300] + "...\n")
    driver.close()

if __name__ == "__main__":
    inspect_checkout_actions()
