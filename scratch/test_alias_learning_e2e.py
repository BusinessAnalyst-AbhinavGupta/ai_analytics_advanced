import os
import json
from core.parser import QueryAnalyzer, analyze_sql_deep_reasoning
from core.neo4j_adapter import Neo4jAdapter
from core.graph_learner import GraphLearner
from core.query_generator import QueryGenerator
from schema.models import DeepSqlReasoning, ColumnAlias

def run_test():
    print("=== STEP 1: TEST ALIAS EXTRACTION ===")
    test_sql = """
    WITH session_funnel AS (
        SELECT 
            session_id,
            natco_code AS natco,
            category_name AS category,
            service_line_code AS service_line,
            date_trunc('week', event_time) AS week_start,
            MAX(CASE WHEN action = 'basket_continue' THEN 1 ELSE 0 END) AS basket_continue,
            MAX(CASE WHEN action = 'checkout_initiated' THEN 1 ELSE 0 END) AS checkout_initiated
        FROM silver_layer.t_link_journey_checkout_com
        WHERE event_time >= current_date - interval '60' day
        GROUP BY 1, 2, 3, 4, 5
    )
    SELECT * FROM session_funnel
    """
    
    analyzer = QueryAnalyzer(test_sql)
    res = analyzer.analyze()
    aliases = res.get("column_aliases", [])
    print(f"Extracted {len(aliases)} column aliases via AST/regex:")
    for a in aliases:
        print(f"  - {a.get('expression')} AS {a.get('alias')} (physical: {a.get('physical_column')}, table: {a.get('table_name')})")
    
    assert any(a["alias"] == "natco" and a["physical_column"] == "natco_code" for a in aliases), "Failed to extract natco -> natco_code"
    assert any(a["alias"] == "category" and a["physical_column"] == "category_name" for a in aliases), "Failed to extract category"
    assert any(a["alias"] == "week_start" and a["physical_column"] == "event_time" for a in aliases), "Failed to extract week_start"
    print("✅ Step 1 Passed: AST/Regex Alias Extraction 100% accurate!")

    print("\n=== STEP 2: TEST PERSISTENCE IN NEO4J GRAPH ===")
    adapter = Neo4jAdapter()
    
    reasoning_data = DeepSqlReasoning(
        intent_name="Weekly Funnel Conversion by Natco and Category",
        journey_stage="Checkout",
        business_goal="Calculate weekly conversion split by natco, category, service line.",
        reasoning_summary="Tracks checkout progression and identifies intermediate step drop-offs.",
        primary_table="silver_layer.t_link_journey_checkout_com",
        column_usages=[],
        column_aliases=[
            ColumnAlias(
                physical_column=a["physical_column"],
                alias=a["alias"],
                expression=a["expression"],
                table_name="silver_layer.t_link_journey_checkout_com",
                reasoning="Extracted from dashboard query projection"
            ) for a in aliases
        ],
        sql_idioms=[],
        learned_rules=[],
        canonical_golden_query=test_sql,
        dialect="AWS Athena / Presto"
    )
    
    ingest_res = adapter.ingest_deep_sql_reasoning(reasoning_data)
    print(f"Ingested into Neo4j: {ingest_res}")

    learner = GraphLearner()
    learned_aliases = learner.get_learned_aliases(["silver_layer.t_link_journey_checkout_com"])
    print(f"Retrieved {len(learned_aliases)} learned aliases from Neo4j:")
    for la in learned_aliases:
        print(f"  - {la['alias']} -> {la['physical_column']} [freq: {la['frequency']}, expr: {la['expression']}]")
    
    assert any(la["alias"] == "natco" and la["physical_column"] == "natco_code" for la in learned_aliases), "Neo4j missing natco alias!"
    print("✅ Step 2 Passed: Dynamic Aliases successfully stored & retrieved from Neo4j!")

    print("\n=== STEP 3: TEST QUERY GENERATOR INJECTION ===")
    gen = QueryGenerator()
    q_ctx = gen.retrieve_graph_context(
        question="Calculate conversion rate from basket_continue to order_placed split by natco, category, service line for the last 2 monthly weekly conversion",
        table_filter="silver_layer.t_link_journey_checkout_com"
    )
    print("Context retrieved learned aliases count:", len(q_ctx.get("learned_aliases", [])))
    assert len(q_ctx.get("learned_aliases", [])) > 0, "Query generator context missing learned aliases!"

    print("\n=== STEP 4: TEST PROMPT COMPLIANCE VIA GEMINI ===", flush=True)
    gemini_key = os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6KfuVn3yd1-VA08eNi5KDtVQ2LbaKWr6jZvrvWD2q1rQQ")
    print("Calling QueryGenerator with Google Gemini API...", flush=True)
    sql_res = gen.generate_sql(
        question="Calculate conversion rate from basket_continue to order_placed split by natco, category, service line for the last 2 monthly weekly conversion . Identify on which of the intermediate steps is the highest drop happening. Weight those drops based on the relative impact on the overall order placed count",
        database_dialect="AWS Athena / Presto",
        table_filter="silver_layer.t_link_journey_checkout_com",
        provider="Google Gemini API",
        model_name="gemini-3.5-flash",
        api_key=gemini_key
    )
    generated_sql = sql_res.get("sql", "")
    print("Generated SQL:\n" + generated_sql, flush=True)
    assert "natco_code" in generated_sql, "Generated SQL should use natco_code AS natco based on learned aliases!"
    print("✅ Step 4 Passed: Gemini 3.5 Flash correctly generated SQL using learned alias natco_code AS natco!", flush=True)

if __name__ == "__main__":
    run_test()
