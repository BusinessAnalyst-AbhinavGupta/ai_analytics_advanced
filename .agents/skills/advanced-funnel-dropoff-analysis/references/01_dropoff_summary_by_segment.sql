-- ========================================================
-- Business Problem: Aggregated Step A -> Step B drop-off, by service_line x category
--   Given a funnel entry point ("Step A", e.g. a specific page) and a completion
--   event ("Step B", e.g. an order-placed action), find every session that
--   reached Step A within the natco/lookback window and report how many of
--   those sessions went on to reach Step B -- broken down by service_line and
--   category, worst drop-off first. This answers "why are users dropping off
--   after X and not reaching Y, broken down by service line and category"
--   style questions directly.
--
-- Parameters (see SKILL.md "Parameters" section for the domain mapping used
-- to fill these from a natural-language question):
--   $natco_code     -- lowercase natco code, e.g. 'de'
--   $lookback_days  -- integer number of days to look back, e.g. 30
--   $step_a_page    -- exact page_name marking funnel entry, e.g. 'checkout/consent'
--   $step_b_action  -- exact action marking funnel completion, e.g. 'purchasesuccess'
-- ========================================================
WITH step_a_events AS (
    SELECT
        session_id,
        service_line,
        category,
        event_timestamp,
        ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY event_timestamp ASC) AS rn
    FROM silver_layer.t_link_journey_checkout_com
    WHERE LOWER(natco_code) = '$natco_code'
      AND CAST(event_date AS DATE) BETWEEN CAST(date_add('day', -$lookback_days, CURRENT_DATE) AS DATE)
                                       AND CAST(date_add('day', -1, CURRENT_DATE) AS DATE)
      AND LOWER(page_name) = '$step_a_page'
),
step_a_sessions AS (
    -- One representative service_line/category per session: whichever was
    -- attached to that session's first Step A event.
    SELECT
        session_id,
        COALESCE(NULLIF(service_line, ''), '(unspecified)') AS service_line,
        COALESCE(NULLIF(category, ''), '(unspecified)') AS category
    FROM step_a_events
    WHERE rn = 1
),
step_b_sessions AS (
    SELECT DISTINCT session_id
    FROM silver_layer.t_link_journey_checkout_com
    WHERE LOWER(natco_code) = '$natco_code'
      AND CAST(event_date AS DATE) BETWEEN CAST(date_add('day', -$lookback_days, CURRENT_DATE) AS DATE)
                                       AND CAST(date_add('day', -1, CURRENT_DATE) AS DATE)
      AND LOWER(action) = '$step_b_action'
)
SELECT
    a.service_line,
    a.category,
    COUNT(*) AS sessions_reached_step_a,
    COUNT(b.session_id) AS sessions_reached_step_b,
    COUNT(*) - COUNT(b.session_id) AS sessions_dropped_off,
    ROUND(100.0 * (COUNT(*) - COUNT(b.session_id)) / COUNT(*), 1) AS dropoff_rate_pct
FROM step_a_sessions a
LEFT JOIN step_b_sessions b ON b.session_id = a.session_id
GROUP BY a.service_line, a.category
ORDER BY sessions_dropped_off DESC
;
