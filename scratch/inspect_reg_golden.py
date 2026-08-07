from core.graph_learner import GraphLearner

def inspect_registration_and_checkout_golden():
    learner = GraphLearner()
    driver = learner._get_driver()
    with driver.session(database=learner.database) as session:
        res = session.run("""
            MATCH (gq:VerifiedGoldenQuery)
            WHERE toLower(gq.name) CONTAINS 'registration' OR toLower(gq.name) CONTAINS 'checkout' OR toLower(gq.sql) CONTAINS 'personalinfo' OR toLower(gq.sql) CONTAINS 'account'
            RETURN gq.name as name, gq.sql as sql
            LIMIT 5
        """).data()
        for r in res:
            print("="*60)
            print("NAME:", r["name"])
            print("="*60)
            print(r["sql"])
    driver.close()

if __name__ == "__main__":
    inspect_registration_and_checkout_golden()
