from core.query_generator import QueryGenerator

def test_dropoff_generation():
    print("🚀 Generating query for drop-off between checkout and personal info...")
    q_gen = QueryGenerator()
    
    question = "In the checkout journey of the users who dropped between checkout initiated and personal info, how many of them did a successful login? data of last 2 weeks only"
    
    res = q_gen.generate_sql(
        question=question,
        database_dialect="AWS Athena / Presto",
        table_filter="eshop_data.es_events_v2",
        provider="Google Gemini API",
        model_name="gemini-3.5-flash",
        temperature=0.0
    )
    
    print("\n" + "="*70)
    print("🎯 GENERATION REPORT")
    print("="*70)
    print(f"Status     : {res.get('verification_status')}")
    print(f"Iterations : {res.get('verification_iterations')}")
    print(f"Healed     : {res.get('healed_columns')}")
    print(f"Errors     : {res.get('validation_errors')}")
    print("\nGenerated SQL:\n" + res.get("sql", ""))
    
    sql = res.get("sql", "").lower()
    
    assert "dropped_at_personalinfo" not in sql, "❌ Error: Found hallucinated dropped_at_personalinfo action!"
    assert "dropped" not in sql or "dropped_cohort" in sql or "dropped_sessions" in sql, "❌ Error: Unchecked dropped action"
    print("\n✅ Query generation passed with verified funnel cohort logic!")

if __name__ == "__main__":
    test_dropoff_generation()
