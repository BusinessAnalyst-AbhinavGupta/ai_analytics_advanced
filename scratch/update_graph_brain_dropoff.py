from core.graph_learner import GraphLearner

def persist_funnel_dropoff_rules():
    print("🧠 Updating Neo4j Knowledge Graph Brain with Funnel Drop-off Guardrails & Golden Query...")
    learner = GraphLearner()
    driver = learner._get_driver()
    
    with driver.session(database=learner.database) as session:
        # 1. Register Funnel Drop-off Logic Guardrail
        session.run("""
            MERGE (r:CorrectionRule {id: 'rule_funnel_dropoff_derived'})
            SET r.rule_text = "Funnel drop-off is NEVER a logged action name (e.g. 'dropped_at_personalinfo' does not exist). Drop-offs must be computed as a cohort using session aggregation: MAX(CASE WHEN step_1 THEN 1 ELSE 0 END) = 1 AND MAX(CASE WHEN step_2 THEN 1 ELSE 0 END) = 0.",
                r.rule_type = "FUNNEL_LOGIC_GUARDRAIL",
                r.table_name = "eshop_data.es_events_v2",
                r.invalid_term = "dropped_at_personalinfo",
                r.correct_term = "MAX(CASE WHEN ...)",
                r.error_snippet = "Hallucinating drop-off event strings instead of cohort subtraction",
                r.created_at = datetime(),
                r.times_applied = 5,
                r.weight = 1.0
            WITH r
            MATCH (t:Table) WHERE t.name IN ['eshop_data.es_events_v2', 'silver_layer.t_link_journey_checkout_com']
            MERGE (t)-[:HAS_CORRECTION_RULE]->(r)
        """)
        print("✅ Added CorrectionRule: 'rule_funnel_dropoff_derived'")

        # 2. Register Athena date_diff and epoch time Guardrail
        session.run("""
            MERGE (r:CorrectionRule {id: 'rule_athena_date_diff_syntax'})
            SET r.rule_text = "In AWS Athena / Presto SQL, date_diff('day', timestamp1, timestamp2) takes two timestamps and returns an integer. To filter for the last N days, use current_timestamp - interval 'N' day. In es_events_v2, identifiers_log_time is epoch ms string, so filter with: from_unixtime(CAST(identifiers_log_time AS BIGINT)/1000) >= current_timestamp - interval '14' day.",
                r.rule_type = "SYNTAX_GUARDRAIL",
                r.table_name = "eshop_data.es_events_v2",
                r.invalid_term = "date_diff('day', 14, current_timestamp)",
                r.correct_term = "current_timestamp - interval '14' day",
                r.created_at = datetime(),
                r.times_applied = 5,
                r.weight = 1.0
            WITH r
            MATCH (t:Table {name: 'eshop_data.es_events_v2'})
            MERGE (t)-[:HAS_CORRECTION_RULE]->(r)
        """)
        print("✅ Added CorrectionRule: 'rule_athena_date_diff_syntax'")

        # 3. Register SqlIdiom for Funnel Drop-off Cohort Analysis
        session.run("""
            MERGE (i:SqlIdiom {name: 'Derived Funnel Drop-off Cohort (Two-Stage Conditional Flagging)'})
            SET i.category = 'Funnel Analysis',
                i.description = 'Calculates drop-offs between Funnel Step 1 and Step 2 by aggregating session events with MAX(CASE...) and filtering Step 1 = 1 AND Step 2 = 0 in outer query.',
                i.when_to_use = 'When the analyst asks for users or sessions that dropped between two funnel steps and wants to analyze their characteristics or crossover behavior.',
                i.sql_skeleton = 'WITH session_funnel AS (
    SELECT 
        identifiers_sessionid,
        MAX(CASE WHEN <step_1_condition> THEN 1 ELSE 0 END) AS is_step_1,
        MAX(CASE WHEN <step_2_condition> THEN 1 ELSE 0 END) AS is_step_2,
        MAX(CASE WHEN <secondary_condition> THEN 1 ELSE 0 END) AS is_secondary_event
    FROM <target_table>
    WHERE <time_and_scope_filters>
    GROUP BY identifiers_sessionid
)
SELECT 
    COUNT(DISTINCT identifiers_sessionid) AS total_dropped_sessions,
    COUNT(DISTINCT CASE WHEN is_secondary_event = 1 THEN identifiers_sessionid END) AS dropped_sessions_with_event
FROM session_funnel
WHERE is_step_1 = 1 AND is_step_2 = 0;'
            WITH i
            MERGE (stage:JourneyStage {name: 'Checkout'})
            MERGE (stage)-[:RECOMMENDS_IDIOM]->(i)
        """)
        print("✅ Added SqlIdiom: 'Derived Funnel Drop-off Cohort'")

        # 4. Save VerifiedGoldenQuery Node using Cypher parameter
        golden_sql = """WITH session_funnel AS (
    SELECT 
        identifiers_sessionid,
        COALESCE(NULLIF(identifiers_user_id, ''), 'guest') AS user_id,
        MAX(CASE WHEN action = 'onecheckoutinitiated' OR lower(identifiers_page_name) LIKE '%checkout/account%' THEN 1 ELSE 0 END) AS is_checkout_initiated,
        MAX(CASE WHEN lower(identifiers_page_name) LIKE '%checkout/personalinfo%' OR (action = 'checkoutStepViewed' AND lower(label) LIKE '%personal%') THEN 1 ELSE 0 END) AS is_personal_info,
        MAX(CASE WHEN (action = 'checkoutSubStepSubmitted' AND lower(label) LIKE '%login%') OR (action = 'clickInteractions' AND lower(label) LIKE '%login%') THEN 1 ELSE 0 END) AS is_login_success
    FROM eshop_data.es_events_v2
    WHERE from_unixtime(CAST(identifiers_log_time AS BIGINT) / 1000) >= current_timestamp - interval '14' day
      AND lower(COALESCE(internalemployee, 'no')) = 'no'
    GROUP BY identifiers_sessionid, COALESCE(NULLIF(identifiers_user_id, ''), 'guest')
),
dropped_cohort AS (
    SELECT 
        identifiers_sessionid,
        user_id,
        is_login_success
    FROM session_funnel
    WHERE is_checkout_initiated = 1
      AND is_personal_info = 0
)
SELECT 
    COUNT(DISTINCT identifiers_sessionid) AS total_dropped_sessions,
    COUNT(DISTINCT CASE WHEN is_login_success = 1 THEN identifiers_sessionid END) AS dropped_sessions_with_login,
    ROUND(COUNT(DISTINCT CASE WHEN is_login_success = 1 THEN identifiers_sessionid END) * 100.0 / NULLIF(COUNT(DISTINCT identifiers_sessionid), 0), 2) AS login_rate_among_dropped_pct
FROM dropped_cohort;"""

        session.run("""
            MERGE (gq:VerifiedGoldenQuery {id: 'gq_checkout_personalinfo_drop_login'})
            SET gq.name = "Dropped users between checkout initiated and personal info with successful login",
                gq.journey_stage = "Checkout",
                gq.intent = "Funnel drop-off and login analysis between checkout start and personal info",
                gq.dialect = "AWS Athena / Presto",
                gq.last_verified = datetime(),
                gq.weight = 1.0,
                gq.sql = $sql
            WITH gq
            MATCH (t:Table {name: 'eshop_data.es_events_v2'})
            MERGE (gq)-[:USES_TABLE]->(t)
        """, sql=golden_sql)
        print("✅ Added VerifiedGoldenQuery: 'gq_checkout_personalinfo_drop_login'")

    driver.close()
    print("\n🎉 Neo4j Knowledge Graph Brain update completed successfully!")

if __name__ == "__main__":
    persist_funnel_dropoff_rules()
