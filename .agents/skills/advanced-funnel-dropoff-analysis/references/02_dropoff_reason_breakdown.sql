-- ========================================================
-- Business Problem: For sessions that reached Step A but never reached Step B,
--   summarize what happened ON the Step A page itself -- explicit errors,
--   failed submissions, rejections, and abandonment signals -- ranked by how
--   many dropped sessions showed each signal. This is the "why" behind the
--   segment-level counts in 01_dropoff_summary_by_segment.sql: it tells you
--   whether drop-off looks like a technical failure (payment errors, 3-D
--   Secure declines, backend rejections) or a passive exit with no error
--   signal at all (the user simply left).
--
-- A dropped session can show more than one signal (e.g. an error AND a
-- passive pageview), so sessions_affected counts are not mutually exclusive
-- across buckets -- each bucket answers "how many dropped sessions showed
-- this signal", not "how many dropped sessions were caused only by this".
--
-- Parameters: same as 01_dropoff_summary_by_segment.sql
--   $natco_code, $lookback_days, $step_a_page, $step_b_action
-- ========================================================
WITH step_a_events AS (
    SELECT session_id, event_timestamp,
           ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY event_timestamp ASC) AS rn
    FROM silver_layer.t_link_journey_checkout_com
    WHERE LOWER(natco_code) = '$natco_code'
      AND CAST(event_date AS DATE) BETWEEN CAST(date_add('day', -$lookback_days, CURRENT_DATE) AS DATE)
                                       AND CAST(date_add('day', -1, CURRENT_DATE) AS DATE)
      AND LOWER(page_name) = '$step_a_page'
),
step_a_sessions AS (
    SELECT session_id FROM step_a_events WHERE rn = 1
),
step_b_sessions AS (
    SELECT DISTINCT session_id
    FROM silver_layer.t_link_journey_checkout_com
    WHERE LOWER(natco_code) = '$natco_code'
      AND CAST(event_date AS DATE) BETWEEN CAST(date_add('day', -$lookback_days, CURRENT_DATE) AS DATE)
                                       AND CAST(date_add('day', -1, CURRENT_DATE) AS DATE)
      AND LOWER(action) = '$step_b_action'
),
dropped_sessions AS (
    SELECT a.session_id
    FROM step_a_sessions a
    LEFT JOIN step_b_sessions b ON b.session_id = a.session_id
    WHERE b.session_id IS NULL
),
reason_bucketed AS (
    SELECT
        d.session_id,
        CASE
            WHEN LOWER(e.error_type) IS NOT NULL AND LOWER(e.error_type) <> '' THEN 'technical error: ' || LOWER(e.error_type)
            WHEN LOWER(e.action) LIKE '%failed%' THEN 'explicit failure: ' || LOWER(e.action)
            WHEN LOWER(e.action) LIKE '%rejected%' THEN 'rejected: ' || LOWER(e.action)
            WHEN LOWER(e.action) LIKE '%adjustmentrequired%' THEN 'requires manual adjustment: ' || LOWER(e.action)
            WHEN LOWER(e.action) LIKE '3dsecure%' THEN '3-D Secure step: ' || LOWER(e.action)
            ELSE 'no error signal (passive exit)'
        END AS reason_bucket
    FROM dropped_sessions d
    JOIN silver_layer.t_link_journey_checkout_com e
      ON e.session_id = d.session_id
     AND LOWER(e.natco_code) = '$natco_code'
     AND LOWER(e.page_name) = '$step_a_page'
     AND CAST(e.event_date AS DATE) BETWEEN CAST(date_add('day', -$lookback_days, CURRENT_DATE) AS DATE)
                                        AND CAST(date_add('day', -1, CURRENT_DATE) AS DATE)
)
SELECT
    reason_bucket,
    COUNT(DISTINCT session_id) AS sessions_affected
FROM reason_bucketed
GROUP BY reason_bucket
ORDER BY sessions_affected DESC
;
