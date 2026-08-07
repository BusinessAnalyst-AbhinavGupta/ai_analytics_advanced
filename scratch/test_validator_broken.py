from core.schema_validator import SQLSchemaValidator

def test_broken_query_validation():
    print("🧪 Testing SQLSchemaValidator against the broken query...")
    validator = SQLSchemaValidator()
    
    broken_sql = """
    WITH base AS (
        SELECT 
            identifiers_sessionid,
            identifiers_user_id,
            action,
            identifiers_log_time
        FROM eshop_data.es_events_v2
        WHERE identifiers_log_time >= date_diff('day', 14, current_timestamp)
          AND lower(internalemployee) = 'no'
    ),
    checkout_initiated AS (
        SELECT DISTINCT identifiers_sessionid, identifiers_user_id
        FROM base
        WHERE action = 'onecheckoutinitiated'
    ),
    personal_info_dropped AS (
        SELECT DISTINCT identifiers_sessionid, identifiers_user_id
        FROM base
        WHERE action = 'dropped_at_personalinfo'
    ),
    successful_logins AS (
        SELECT DISTINCT identifiers_sessionid, identifiers_user_id
        FROM eshop_data.es_events_v2
        WHERE action = 'login_success'
          AND identifiers_log_time >= date_diff('day', 14, current_timestamp)
          AND lower(internalemployee) = 'no'
    )
    SELECT COUNT(DISTINCT sl.identifiers_user_id) AS successful_login_count
    FROM personal_info_dropped pd
    JOIN successful_logins sl ON pd.identifiers_sessionid = sl.identifiers_sessionid;
    """
    
    from core.query_generator import QueryGenerator
    q_gen = QueryGenerator()
    ctx = q_gen.retrieve_graph_context(question="checkout drop", table_filter="eshop_data.es_events_v2")
    valid_cols = {c["name"]: c for c in ctx.get("columns", [])}

    res = validator.validate_sql(
        sql=broken_sql,
        target_table="eshop_data.es_events_v2",
        valid_columns=valid_cols,
        dialect="AWS Athena / Presto"
    )
    
    print(f"Is Valid: {res.is_valid}")
    print(f"Errors Found: {len(res.errors)}")
    for i, err in enumerate(res.errors, 1):
        print(f"  [{i}] {err}")
    print("\nDiagnostic Prompt Generated for Re-compiler:")
    print(res.diagnostic_prompt)
    
    assert not res.is_valid, "Validator should have rejected this broken query!"
    print("\n✅ Validator successfully caught all broken patterns!")

if __name__ == "__main__":
    test_broken_query_validation()
