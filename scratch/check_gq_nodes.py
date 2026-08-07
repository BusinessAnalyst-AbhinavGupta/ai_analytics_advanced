from core.graph_learner import GraphLearner

def check_gq():
    learner = GraphLearner()
    driver = learner._get_driver()
    with driver.session(database=learner.database) as session:
        res = session.run("MATCH (q:VerifiedGoldenQuery) RETURN q.id, q.name, q.question, q.journey_stage").data()
        print(f"Total VerifiedGoldenQuery nodes: {len(res)}")
        for r in res:
            print(f"  ⭐ {r}")
    driver.close()

if __name__ == "__main__":
    check_gq()
