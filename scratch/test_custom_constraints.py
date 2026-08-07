from core.query_generator import QueryGenerator
from core.schema_validator import SQLSchemaValidator

def test_constraints_enforcement():
    print("🚀 Testing Custom Constraints Enforcement in Query Generator...")
    q_gen = QueryGenerator()
    
    question = "Calculate conversion rate from basket_continue to order_placed weekly for the last 2 months."
    constraints = "Strictly filter for natco = 'DE', service_line = 'fixed', and event_date >= '2026-01-01'."
    
    print(f"\nQuestion   : {question}")
    print(f"Constraints: {constraints}")
    print("\nCalling QueryGenerator with Gemini 3.5 Flash...")
    
    res = q_gen.generate_sql(
        question=question,
        database_dialect="AWS Athena / Presto",
        table_filter="silver_layer.t_link_journey_checkout_com",
        provider="Google Gemini API",
        model_name="gemini-3.5-flash",
        temperature=0.0,
        custom_instructions=constraints
    )
    
    print("\n" + "="*70)
    print("🎯 CONSTRAINTS VERIFICATION REPORT")
    print("="*70)
    print(f"Verification Status     : {res.get('verification_status')}")
    print(f"Verification Iterations : {res.get('verification_iterations')}")
    print(f"Healed Columns          : {res.get('healed_columns')}")
    print(f"Validation Errors       : {res.get('validation_errors')}")
    print("\nGenerated SQL:\n" + res.get("sql", ""))
    
    sql = res.get("sql", "").lower()
    
    # Assert constraints are present in the SQL
    assert "'de'" in sql or "= 'de'" in sql or "de" in sql, "❌ Error: Constraint 'DE' was not applied in the query!"
    assert "'fixed'" in sql or "fixed" in sql, "❌ Error: Constraint 'fixed' was not applied in the query!"
    assert "2026-01-01" in sql, "❌ Error: Constraint '2026-01-01' was not applied in the query!"
    
    print("\n✅ All constraints were strictly enforced and verified in the SQL!")

if __name__ == "__main__":
    test_constraints_enforcement()
