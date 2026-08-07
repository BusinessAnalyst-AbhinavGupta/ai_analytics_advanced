from core.schema_validator import SQLSchemaValidator
from core.llm_gateway import LLMGateway

def test_forced_repair_flow():
    print("Testing simulated repair loop on an initial draft with 'nc'...")
    
    # 1. Simulate bad draft
    bad_draft = """
    WITH session_funnel AS (
        SELECT 
            session_id,
            nc AS natco,
            category,
            service_line,
            MAX(CASE WHEN action = 'basket_continue' THEN 1 ELSE 0 END) AS basket_continue,
            MAX(CASE WHEN action = 'order_placed' THEN 1 ELSE 0 END) AS order_placed
        FROM silver_layer.t_link_journey_checkout_com
        GROUP BY session_id, nc, category, service_line
    )
    SELECT natco, category, service_line, COUNT(session_id) FROM session_funnel GROUP BY 1, 2, 3
    """
    
    valid_cols_dict = {
        "session_id": {"dtype": "string"},
        "natco_code": {"dtype": "string"},
        "category": {"dtype": "string"},
        "service_line": {"dtype": "string"},
        "event_date": {"dtype": "string"},
        "action": {"dtype": "string"}
    }
    
    learned_aliases = [
        {"alias": "natco", "physical_column": "natco_code", "table_name": "silver_layer.t_link_journey_checkout_com"}
    ]
    
    # Run Validator
    val_res = SQLSchemaValidator.validate_sql(
        sql=bad_draft,
        target_table="silver_layer.t_link_journey_checkout_com",
        valid_columns=valid_cols_dict,
        learned_aliases=learned_aliases,
        dialect="AWS Athena / Presto"
    )
    
    print("Validator Detected Errors:", val_res.errors)
    print("Validator Suggestions:", val_res.suggestions)
    assert not val_res.is_valid
    assert any(s["invalid_term"] == "nc" for s in val_res.suggestions)
    
    # Formulate Diagnostic Prompt
    diagnostic_prompt = val_res.diagnostic_prompt
    print("\nDiagnostic Prompt Sent to Model:\n" + diagnostic_prompt)
    
    # Call Gemini to heal
    heal_res = LLMGateway.generate(
        prompt=f"{diagnostic_prompt}\n\nDraft SQL to fix:\n```sql\n{bad_draft}\n```",
        system_prompt="You are a SQL Compiler. Fix all invalid columns in the draft SQL strictly using the suggestions.",
        provider="Google Gemini API",
        model="gemini-3.5-flash",
        temperature=0.0
    )
    
    healed_text = heal_res.get("text", "")
    print("\nHealed Response Snippet:\n" + healed_text[:400])
    assert "natco_code" in healed_text
    print("\n✅ Simulated Forced Repair Test PASSED!")

if __name__ == "__main__":
    test_forced_repair_flow()
