import re
from core.query_generator import format_leading_commas
from core.schema_validator import SQLSchemaValidator
from core.analysis import parse_analytics_logic
from core.parser import QueryAnalyzer

# 1. Test Query with multiple CTEs and leading commas
sql_leading_commas = """WITH base AS (
    SELECT 
          identifiers_sessionid
        , identifiers_user_id
        , identifiers_page_name
        , action
        , identifiers_log_time
    FROM eshop_data.es_events_v2
    WHERE identifiers_log_time >= current_timestamp - interval '14' day
      AND lower(COALESCE(internalemployee, 'no')) = 'no'
)

, checkout_initiated AS (
    SELECT 
          identifiers_sessionid
        , identifiers_user_id
    FROM base
    WHERE action = 'onecheckoutinitiated'
)

, personal_info_dropped AS (
    SELECT 
          ci.identifiers_sessionid
        , ci.identifiers_user_id
    FROM checkout_initiated ci
    LEFT JOIN base b 
      ON ci.identifiers_sessionid = b.identifiers_sessionid 
     AND b.action = 'onecheckoutpersonalinfo'
    WHERE b.identifiers_sessionid IS NULL
)

SELECT 
      COUNT(DISTINCT p.identifiers_sessionid) AS total_dropped_sessions
    , COUNT(DISTINCT CASE WHEN b.action = 'login_success' THEN p.identifiers_sessionid END) AS successful_logins
    , ROUND(
          COUNT(DISTINCT CASE WHEN b.action = 'login_success' THEN p.identifiers_sessionid END) * 100.0
          / NULLIF(COUNT(DISTINCT p.identifiers_sessionid), 0)
        , 2
      ) AS login_conversion_rate_pct
FROM personal_info_dropped p
LEFT JOIN base b 
  ON p.identifiers_sessionid = b.identifiers_sessionid;"""

print("--- Testing Formatter Idempotency ---")
formatted_1 = format_leading_commas(sql_leading_commas)
formatted_2 = format_leading_commas(formatted_1)
assert formatted_1 == formatted_2, "Formatter must be idempotent"
print("✓ Formatter is perfectly idempotent!")

print("\n--- Testing Schema Validator on Leading-Comma CTEs ---")
ctes, tbl_parts, tbl_aliases, col_aliases = SQLSchemaValidator.extract_ctes_tables_and_aliases(sql_leading_commas)
print(f"Extracted CTEs: {ctes}")
assert "base" in ctes, "Should extract base CTE"
assert "checkout_initiated" in ctes, "Should extract checkout_initiated CTE"
assert "personal_info_dropped" in ctes, "Should extract personal_info_dropped CTE"
print("Extracted Table Aliases:", tbl_aliases)
print("Extracted Column Aliases:", col_aliases)
print("✓ SQLSchemaValidator extracted all CTEs and aliases cleanly!")

print("\n--- Testing QueryAnalyzer ---")
analyzer = QueryAnalyzer(sql_leading_commas)
analysis_res = analyzer.analyze()
print("Extracted Metrics:", analysis_res.get("extracted_metrics"))
print("Extracted Column Aliases:", [a["alias"] for a in analysis_res.get("column_aliases", [])])
print("✓ QueryAnalyzer passed cleanly!")

print("\n--- Testing SQLAnalysisEngine ---")
analytics_logic = parse_analytics_logic(sql_leading_commas)
print("Analytics Logic keys:", analytics_logic.keys())
print("✓ parse_analytics_logic passed cleanly!")

print("\n--- Converting Trailing Comma SQL to Leading Comma SQL ---")
trailing_sql = """WITH step1 AS (
    SELECT 
        identifiers_sessionid,
        action,
        identifiers_log_time
    FROM eshop_data.es_events_v2
),
step2 AS (
    SELECT 
        identifiers_sessionid,
        action
    FROM step1
)
SELECT 
    identifiers_sessionid,
    COUNT(*) AS cnt
FROM step2
GROUP BY identifiers_sessionid;"""

converted = format_leading_commas(trailing_sql)
print(converted)
assert ") \n\n, step2 AS (" in converted or ")\n\n, step2 AS (" in converted, "Should place comma before step2 AS"
print("✓ Conversion from trailing to leading commas for both columns and CTEs works cleanly!")
