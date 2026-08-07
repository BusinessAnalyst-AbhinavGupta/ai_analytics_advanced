from core.query_generator import QueryGenerator
import json

def test_ctx():
    q_gen = QueryGenerator()
    question = "In the checkout journey of the users who dropped between checkout initiated and personal info, how many of them did a successful login? data of last 2 weeks only"
    
    ctx = q_gen.retrieve_graph_context(question=question, table_filter="Auto-Detect All Tables")
    
    print("Tables returned:", [t["name"] for t in ctx["tables"]])
    print("Detected stage:", ctx.get("detected_stage"))
    print("Golden queries count:", len(ctx.get("golden_queries", [])))
    for g in ctx.get("golden_queries", []):
        print("  - Golden query:", g.get("question"))
    print("Learned rules count:", len(ctx.get("learned_rules", [])))
    for r in ctx.get("learned_rules", []):
        print("  - Rule:", r.get("rule_text"))
    print("Sql idioms count:", len(ctx.get("sql_idioms", [])))
    for i in ctx.get("sql_idioms", []):
        print("  - Idiom:", i.get("name"))

if __name__ == "__main__":
    test_ctx()
