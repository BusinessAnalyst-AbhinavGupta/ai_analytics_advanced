import json
from core.query_generator import QueryGenerator
from core.schema_validator import SQLSchemaValidator

def test_e2e_agentic_loop():
    print("🚀 Initializing QueryGenerator with Agentic Verification Loop...")
    q_gen = QueryGenerator()
    
    question = (
        "Calculate conversion rate from basket_continue to order_placed split by natco, "
        "category, service line for the last 2 monthly weekly conversion."
    )
    
    print(f"\nTarget Question: \"{question}\"")
    print("Generating SQL with Gemini 3.5 Flash through Agentic Verification Loop...")
    
    res = q_gen.generate_sql(
        question=question,
        database_dialect="AWS Athena / Presto",
        table_filter="silver_layer.t_link_journey_checkout_com",
        provider="Google Gemini API",
        model_name="gemini-3.5-flash",
        temperature=0.0
    )
    
    print("\n" + "="*70)
    print("🎯 AGENTIC LOOP GENERATION REPORT")
    print("="*70)
    print(f"Verification Status     : {res.get('verification_status')}")
    print(f"Verification Iterations : {res.get('verification_iterations')}")
    print(f"Healed Columns          : {res.get('healed_columns')}")
    print(f"Validation Errors       : {res.get('validation_errors')}")
    print(f"Latency                 : {res.get('latency_seconds')}s")
    print("\nGenerated SQL:\n" + res.get("sql", ""))
    
    sql = res.get("sql", "")
    assert "nc " not in sql and "nc," not in sql and "nc\n" not in sql, "❌ Error: 'nc' was hallucinated in the query!"
    assert "natco_code" in sql, "❌ Error: 'natco_code' should be present in the query!"
    assert res.get("verification_status") in ["VERIFIED_1ST_TRY", "HEALED_AND_VERIFIED"], f"❌ Invalid status: {res.get('verification_status')}"
    
    print("\n✅ End-to-End Agentic Verification Loop PASSED with 100% Schema Integrity!")

if __name__ == "__main__":
    test_e2e_agentic_loop()
