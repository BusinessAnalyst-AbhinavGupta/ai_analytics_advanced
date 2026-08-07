from core.graph_learner import GraphLearner

def inspect_golden_and_idioms():
    learner = GraphLearner()
    driver = learner._get_driver()
    with driver.session(database=learner.database) as session:
        print("--- Golden Queries ---")
        gqs = session.run("MATCH (g:GoldenQuery) RETURN g.id, g.question, g.journey_stage, g.tables").data()
        for g in gqs:
            print(f"  ID: {g['g.id']} | Stage: {g['g.journey_stage']} | Tables: {g['g.tables']} | Q: {g['g.question']}")
            
        print("\n--- SQL Idioms ---")
        idioms = session.run("MATCH (i:SqlIdiom) RETURN i.name, i.category, i.journey_stage").data()
        for i in idioms:
            print(f"  Name: {i['i.name']} | Cat: {i['i.category']} | Stage: {i['i.journey_stage']}")
            
        print("\n--- Learned Rules ---")
        rules = session.run("MATCH (r:LearnedRule) RETURN r.id, r.table_name, r.rule_text, r.rule_type").data()
        for r in rules:
            print(f"  ID: {r['r.id']} | Table: {r['r.table_name']} | Rule: {r['r.rule_text']}")
    driver.close()

if __name__ == "__main__":
    inspect_golden_and_idioms()
