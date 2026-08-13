-- ========================================================
-- 🎯 Business Problem: Build a Canonical Customer Journey from Clickstream Data
--    Iterative hierarchical strategy for median order calculation:
--      1) Top 10,000 sessions by distinct page-action depth per (service_line, natco, category)
--      2) Relative occurrence order:
--            - Page order within Session
--            - Action order within Page
--            - Label order within Action
--      3) Median position calculated relative to the previous hierarchy level
--      4) Hierarchical retention filters:
--            - Pages >= 10% of total sessions
--            - Actions >= 15% of page sessions
--            - Labels >= 22.5% of action sessions
--      5) Multi-level canonical sequence assignment (page_order -> action_order -> label_order)
-- ========================================================

WITH base AS (
    SELECT
          COALESCE(TRY_CAST(_timestamp AS TIMESTAMP), TIMESTAMP '1970-01-01') AS event_time
        , COALESCE(TRY_CAST(identifiers_log_time AS BIGINT), 0)               AS log_time_ms
        , COALESCE(TRY_CAST(identifiers_sessioneventcount AS BIGINT), 0)      AS sess_event_no
        , identifiers_sessionid
        , COALESCE(LOWER(TRIM(identifiers_page_name)), '')                    AS page_name
        , COALESCE(LOWER(TRIM(action)), '')                                   AS action_name
        , COALESCE(LOWER(TRIM(label)), '')                                    AS label_name
        , COALESCE(LOWER(TRIM(service_line)), LOWER(identifiers_page_category_2), 'unknown') AS service_line
        , COALESCE(LOWER(TRIM(nc)), 'unknown')                                AS natco
        , COALESCE(LOWER(TRIM(category)), 'unknown')                          AS category
    FROM eshop_data.es_events_v2
    WHERE TRY_CAST("date" AS DATE) >= date_add('day', -15, CURRENT_DATE)
      AND TRY_CAST("date" AS DATE) < CURRENT_DATE
      AND "year" BETWEEN year(date_add('day', -15, CURRENT_DATE)) AND year(CURRENT_DATE)
      AND identifiers_sessionid IS NOT NULL
      AND TRIM(identifiers_sessionid) <> ''
      AND COALESCE(LOWER(TRIM(category)), 'unknown') <> 'addonmanagement'
      AND NOT (action LIKE '%referer%')
      AND COALESCE(LOWER(TRIM(nc)), 'unknown') = 'de'
      AND LOWER(internalemployee) = 'no'
)

-- Step 1: Identify top 10,000 depth sessions
, session_depth AS (
    SELECT
          service_line
        , natco
        , category
        , identifiers_sessionid
        , COUNT(DISTINCT page_name) AS page_depth
        , COUNT(DISTINCT CONCAT(COALESCE(page_name,''), '|', COALESCE(action_name,''), '|', COALESCE(label_name,''))) AS journey_depth
    FROM base
    GROUP BY
          service_line
        , natco
        , category
        , identifiers_sessionid
)

, top_sessions AS (
    SELECT
          sd.service_line
        , sd.natco
        , sd.category
        , sd.identifiers_sessionid
        , sd.journey_depth
        , ROW_NUMBER() OVER (
              PARTITION BY sd.service_line, sd.natco, sd.category
              ORDER BY sd.page_depth DESC, sd.journey_depth DESC, sd.identifiers_sessionid
          ) AS rn
    FROM session_depth sd
)

, retained_sessions AS (
    SELECT
          ts.service_line
        , ts.natco
        , ts.category
        , ts.identifiers_sessionid
    FROM top_sessions ts
    WHERE ts.rn <= 10000
)

-- Step 2: Extract base session events for retained sessions
, session_events AS (
    SELECT
          b.service_line
        , b.natco
        , b.category
        , b.identifiers_sessionid
        , b.page_name
        , b.action_name
        , b.label_name
        , b.log_time_ms
        , b.sess_event_no
        , b.event_time
    FROM base b
    INNER JOIN retained_sessions rs
        ON b.identifiers_sessionid = rs.identifiers_sessionid
       AND b.service_line = rs.service_line
       AND b.natco = rs.natco
       AND b.category = rs.category
)

-- Step 3: Compute session-level first occurrence timestamps per hierarchy
, session_page_first AS (
    SELECT
          service_line, natco, category, identifiers_sessionid, page_name
        , MIN(log_time_ms) AS min_log_time
    FROM session_events
    GROUP BY service_line, natco, category, identifiers_sessionid, page_name
)

, session_action_first AS (
    SELECT
          service_line, natco, category, identifiers_sessionid, page_name, action_name
        , MIN(log_time_ms) AS min_log_time
    FROM session_events
    GROUP BY service_line, natco, category, identifiers_sessionid, page_name, action_name
)

, session_label_first AS (
    SELECT
          service_line, natco, category, identifiers_sessionid, page_name, action_name, label_name
        , MIN(log_time_ms) AS min_log_time
    FROM session_events
    GROUP BY service_line, natco, category, identifiers_sessionid, page_name, action_name, label_name
)

-- Step 4: Calculate relative rank within the previous hierarchy level
, session_page_seq AS (
    SELECT
          service_line, natco, category, identifiers_sessionid, page_name
        , DENSE_RANK() OVER (
              PARTITION BY service_line, natco, category, identifiers_sessionid
              ORDER BY min_log_time
          ) AS page_seq
    FROM session_page_first
)

, session_action_seq AS (
    SELECT
          service_line, natco, category, identifiers_sessionid, page_name, action_name
        , DENSE_RANK() OVER (
              PARTITION BY service_line, natco, category, identifiers_sessionid, page_name
              ORDER BY min_log_time
          ) AS action_seq
    FROM session_action_first
)

, session_label_seq AS (
    SELECT
          service_line, natco, category, identifiers_sessionid, page_name, action_name, label_name
        , DENSE_RANK() OVER (
              PARTITION BY service_line, natco, category, identifiers_sessionid, page_name, action_name
              ORDER BY min_log_time
          ) AS label_seq
    FROM session_label_first
)

-- Step 5: Total retained sessions per combination
, combo_totals AS (
    SELECT
          service_line
        , natco
        , category
        , COUNT(*) AS total_sessions
    FROM retained_sessions
    GROUP BY
          service_line
        , natco
        , category
)

-- Step 6: Compute median relative order & session counts at each hierarchy level
, page_stats AS (
    SELECT
          service_line, natco, category, page_name
        , COUNT(*) AS page_sessions
        , approx_percentile(page_seq, 0.5) AS page_median_seq
    FROM session_page_seq
    GROUP BY service_line, natco, category, page_name
)

, action_stats AS (
    SELECT
          service_line, natco, category, page_name, action_name
        , COUNT(*) AS action_sessions
        , approx_percentile(action_seq, 0.5) AS action_median_seq
    FROM session_action_seq
    GROUP BY service_line, natco, category, page_name, action_name
)

, label_stats AS (
    SELECT
          service_line, natco, category, page_name, action_name, label_name
        , COUNT(*) AS label_sessions
        , approx_percentile(label_seq, 0.5) AS label_median_seq
    FROM session_label_seq
    GROUP BY service_line, natco, category, page_name, action_name, label_name
)

-- Step 7: Filter rare observations based on share of previous hierarchy
, filtered_pages AS (
    SELECT
          ps.service_line, ps.natco, ps.category, ps.page_name
        , ps.page_sessions, ps.page_median_seq, ct.total_sessions
    FROM page_stats ps
    INNER JOIN combo_totals ct
        ON ps.service_line = ct.service_line
       AND ps.natco = ct.natco
       AND ps.category = ct.category
    WHERE CAST(ps.page_sessions AS DOUBLE) / NULLIF(ct.total_sessions, 0) >= 0.10
)

, filtered_actions AS (
    SELECT
          ast.service_line, ast.natco, ast.category, ast.page_name, ast.action_name
        , ast.action_sessions, ast.action_median_seq, fp.page_sessions, fp.total_sessions
    FROM action_stats ast
    INNER JOIN filtered_pages fp
        ON ast.service_line = fp.service_line
       AND ast.natco = fp.natco
       AND ast.category = fp.category
       AND ast.page_name = fp.page_name
    WHERE CAST(ast.action_sessions AS DOUBLE) / NULLIF(fp.page_sessions, 0) >= 0.15
)

, filtered_labels AS (
    SELECT
          lst.service_line, lst.natco, lst.category, lst.page_name, lst.action_name, lst.label_name
        , lst.label_sessions, lst.label_median_seq, fa.action_sessions, fa.page_sessions, fa.total_sessions
    FROM label_stats lst
    INNER JOIN filtered_actions fa
        ON lst.service_line = fa.service_line
       AND lst.natco = fa.natco
       AND lst.category = fa.category
       AND lst.page_name = fa.page_name
       AND lst.action_name = fa.action_name
    WHERE CAST(lst.label_sessions AS DOUBLE) / NULLIF(fa.action_sessions, 0) >= 0.225
)

-- Step 8: Assign deterministic canonical order numbers at each level
, page_order AS (
    SELECT
          fp.*
        , DENSE_RANK() OVER (
              PARTITION BY fp.service_line, fp.natco, fp.category
              ORDER BY fp.page_median_seq, fp.page_sessions DESC, fp.page_name
          ) AS page_order
    FROM filtered_pages fp
)

, action_order AS (
    SELECT
          fa.*
        , po.page_order
        , DENSE_RANK() OVER (
              PARTITION BY fa.service_line, fa.natco, fa.category, fa.page_name
              ORDER BY fa.action_median_seq, fa.action_sessions DESC, fa.action_name
          ) AS action_order
    FROM filtered_actions fa
    INNER JOIN page_order po
        ON fa.service_line = po.service_line
       AND fa.natco = po.natco
       AND fa.category = po.category
       AND fa.page_name = po.page_name
)

, label_order AS (
    SELECT
          fl.*
        , ao.page_order
        , ao.action_order
        , DENSE_RANK() OVER (
              PARTITION BY fl.service_line, fl.natco, fl.category, fl.page_name, fl.action_name
              ORDER BY fl.label_median_seq, fl.label_sessions DESC, fl.label_name
          ) AS label_order
    FROM filtered_labels fl
    INNER JOIN action_order ao
        ON fl.service_line = ao.service_line
       AND fl.natco = ao.natco
       AND fl.category = ao.category
       AND fl.page_name = ao.page_name
       AND fl.action_name = ao.action_name
)

-- Final Canonical Journey Path Output
SELECT
      lo.service_line
    , lo.natco
    , lo.category
    , lo.page_order
    , lo.action_order
    , lo.label_order
    , lo.page_name
    , lo.action_name
    , lo.label_name
    , lo.label_sessions
    , lo.action_sessions
    , lo.page_sessions
    , lo.total_sessions
    , ROUND(CAST(lo.page_sessions AS DOUBLE) / NULLIF(lo.total_sessions, 0), 4) AS page_share
    , ROUND(CAST(lo.action_sessions AS DOUBLE) / NULLIF(lo.page_sessions, 0), 4) AS action_share
    , ROUND(CAST(lo.label_sessions AS DOUBLE) / NULLIF(lo.action_sessions, 0), 4) AS label_share
FROM label_order lo
ORDER BY
      lo.service_line
    , lo.natco
    , lo.category
    , lo.page_order
    , lo.action_order
    , lo.label_order;
