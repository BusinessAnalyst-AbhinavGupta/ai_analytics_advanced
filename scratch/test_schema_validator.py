from core.schema_validator import SQLSchemaValidator

def test_validator():
    valid_cols = {
        "session_id": {"dtype": "string"},
        "natco_code": {"dtype": "string"},
        "category_name": {"dtype": "string"},
        "service_line_code": {"dtype": "string"},
        "event_time": {"dtype": "timestamp"},
        "action": {"dtype": "string"},
        "error_type": {"dtype": "string"}
    }
    learned_aliases = [
        {"alias": "natco", "physical_column": "natco_code", "table_name": "silver_layer.t_link_journey_checkout_com"},
        {"alias": "category", "physical_column": "category_name", "table_name": "silver_layer.t_link_journey_checkout_com"},
        {"alias": "service_line", "physical_column": "service_line_code", "table_name": "silver_layer.t_link_journey_checkout_com"}
    ]

    print("=== TEST 1: INVALID QUERY WITH HALLUCINATED 'nc' AND 'SAFE_DIVIDE' ===")
    bad_sql = """
    WITH funnel AS (
        SELECT 
            session_id,
            nc,
            SAFE_DIVIDE(1, 2) as ratio
        FROM silver_layer.t_link_journey_checkout_com
    )
    SELECT * FROM funnel
    """
    res1 = SQLSchemaValidator.validate_sql(
        sql=bad_sql,
        target_table="silver_layer.t_link_journey_checkout_com",
        valid_columns=valid_cols,
        learned_aliases=learned_aliases,
        dialect="AWS Athena / Presto"
    )
    print("Is Valid:", res1.is_valid)
    print("Errors:", res1.errors)
    print("Suggestions:", res1.suggestions)
    print("Diagnostic Prompt:\n", res1.diagnostic_prompt)
    assert not res1.is_valid
    assert any("nc" in e or "SAFE_DIVIDE" in e for e in res1.errors)
    print("✅ Test 1 Passed: Hallucinated 'nc' and 'SAFE_DIVIDE' caught!")

    print("\n=== TEST 2: VALID ATHENA SQL QUERY ===")
    good_sql = """
    WITH funnel AS (
        SELECT 
            session_id,
            natco_code AS natco,
            category_name AS category,
            service_line_code AS service_line,
            date_trunc('week', event_time) AS week_start,
            MAX(CASE WHEN action = 'basket_continue' THEN 1 ELSE 0 END) AS basket_continue
        FROM silver_layer.t_link_journey_checkout_com
        GROUP BY 1, 2, 3, 4, 5
    )
    SELECT 
        natco,
        category,
        service_line,
        week_start,
        COUNT(DISTINCT session_id) AS total_sessions
    FROM funnel
    GROUP BY 1, 2, 3, 4
    """
    res2 = SQLSchemaValidator.validate_sql(
        sql=good_sql,
        target_table="silver_layer.t_link_journey_checkout_com",
        valid_columns=valid_cols,
        learned_aliases=learned_aliases,
        dialect="AWS Athena / Presto"
    )
    print("Is Valid:", res2.is_valid)
    print("Errors:", res2.errors)
    assert res2.is_valid
    print("✅ Test 2 Passed: Compliant SQL correctly passes deterministic validator!")

if __name__ == "__main__":
    test_validator()
