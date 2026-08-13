-- ========================================================
-- 🎯 Funnel Conversion Analysis Template
--    Calculates step-by-step progression and drop-off rates across
--    the canonical happy path nodes identified by canonical_happy_flow.sql.
-- ========================================================

WITH base_events AS (
    SELECT
          identifiers_sessionid
        , COALESCE(LOWER(TRIM(identifiers_page_name)), '') AS page_name
        , COALESCE(LOWER(TRIM(action)), '')                AS action_name
        , COALESCE(LOWER(TRIM(label)), '')                 AS label_name
        , COALESCE(TRY_CAST(identifiers_log_time AS BIGINT), 0) AS log_time_ms
    FROM eshop_data.es_events_v2
    WHERE TRY_CAST("date" AS DATE) >= date_add('day', -15, CURRENT_DATE)
      AND TRY_CAST("date" AS DATE) < CURRENT_DATE
      AND identifiers_sessionid IS NOT NULL
      AND LOWER(internalemployee) = 'no'
)

-- Step 1: Track timestamp of first occurrence of each happy flow node per session
, step_occurrences AS (
    SELECT
          identifiers_sessionid
        -- Step 1 (Start Point)
        , MIN(CASE WHEN page_name = '{{STEP_1_PAGE}}' AND action_name = '{{STEP_1_ACTION}}' AND ('{{STEP_1_LABEL}}' = '' OR label_name = '{{STEP_1_LABEL}}') THEN log_time_ms END) AS step_1_time
        -- Step 2
        , MIN(CASE WHEN page_name = '{{STEP_2_PAGE}}' AND action_name = '{{STEP_2_ACTION}}' AND ('{{STEP_2_LABEL}}' = '' OR label_name = '{{STEP_2_LABEL}}') THEN log_time_ms END) AS step_2_time
        -- Step N (End Point)
        , MIN(CASE WHEN page_name = '{{STEP_N_PAGE}}' AND action_name = '{{STEP_N_ACTION}}' AND ('{{STEP_N_LABEL}}' = '' OR label_name = '{{STEP_N_LABEL}}') THEN log_time_ms END) AS step_n_time
    FROM base_events
    GROUP BY identifiers_sessionid
)

-- Step 2: Enforce sequential funnel order (Step 2 must occur after Step 1, etc.)
, funnel_stages AS (
    SELECT
          identifiers_sessionid
        , CASE WHEN step_1_time IS NOT NULL THEN 1 ELSE 0 END AS reached_step_1
        , CASE WHEN step_1_time IS NOT NULL AND step_2_time > step_1_time THEN 1 ELSE 0 END AS reached_step_2
        , CASE WHEN step_1_time IS NOT NULL AND step_2_time > step_1_time AND step_n_time > step_2_time THEN 1 ELSE 0 END AS reached_step_n
    FROM step_occurrences
)

-- Step 3: Aggregate funnel metrics & drop-offs
SELECT
      SUM(reached_step_1) AS step_1_sessions
    , SUM(reached_step_2) AS step_2_sessions
    , SUM(reached_step_n) AS step_n_sessions
    , ROUND(CAST(SUM(reached_step_2) AS DOUBLE) / NULLIF(SUM(reached_step_1), 0) * 100, 2) AS step_1_to_2_conv_pct
    , ROUND(CAST(SUM(reached_step_n) AS DOUBLE) / NULLIF(SUM(reached_step_2), 0) * 100, 2) AS step_2_to_n_conv_pct
    , ROUND(CAST(SUM(reached_step_n) AS DOUBLE) / NULLIF(SUM(reached_step_1), 0) * 100, 2) AS overall_funnel_conv_pct
FROM funnel_stages;
