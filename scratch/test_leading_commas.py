import re

def format_leading_commas(sql: str) -> str:
    """
    Ensures that SQL projections and column lists within SELECT blocks use leading commas (comma-first style),
    making it easy for analysts to comment out individual lines without comma syntax errors.
    Safely preserves comments, function calls (COALESCE, date_diff, etc.), and CTE structures.
    """
    if not sql:
        return sql
        
    lines = sql.split("\n")
    new_lines = []
    in_select_block = False
    
    for i, line in enumerate(lines):
        stripped = line.strip()
        
        # Check pure comments
        if stripped.startswith("--") or stripped.startswith("/*"):
            new_lines.append(line)
            continue
            
        # Detect SELECT clause
        if re.match(r"^\s*SELECT\b", line, re.IGNORECASE):
            in_select_block = True
            new_lines.append(line)
            continue
            
        # Detect end of SELECT clause (FROM, WHERE, GROUP BY, etc., at top-level or matching CTE)
        if in_select_block and re.match(r"^\s*(FROM|WHERE|GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|UNION|WINDOW|\))\b", line, re.IGNORECASE):
            in_select_block = False
            new_lines.append(line)
            continue

        if in_select_block:
            # Handle comments attached at the end of the line e.g., "identifiers_user_id, -- comment"
            if len(new_lines) > 0:
                prev_line = new_lines[-1]
                
                # Separate trailing comment if any from prev_line
                comment_part = ""
                code_part = prev_line
                if "--" in prev_line:
                    comment_idx = prev_line.find("--")
                    comment_part = prev_line[comment_idx:]
                    code_part = prev_line[:comment_idx].rstrip()
                
                code_stripped = code_part.strip()
                if code_stripped.endswith(",") and not re.match(r"^\s*SELECT\b", code_part, re.IGNORECASE):
                    # Remove trailing comma
                    comma_idx = code_part.rfind(",")
                    cleaned_code = code_part[:comma_idx] + code_part[comma_idx+1:].rstrip()
                    new_lines[-1] = (cleaned_code + " " + comment_part).rstrip() if comment_part else cleaned_code
                    
                    # Add leading comma to current line if not already starting with one
                    if not stripped.startswith(","):
                        indent_match = re.match(r"^(\s*)", line)
                        indent = indent_match.group(1) if indent_match else "    "
                        content = line.strip()
                        if len(indent) >= 2:
                            line = indent[:-2] + ", " + content
                        else:
                            line = indent + ", " + content
            new_lines.append(line)
        else:
            new_lines.append(line)
            
    return "\n".join(new_lines)

# Test 1: User's exact sample query
test_sql_user = """-- ========================================================
-- 🎯 Business Problem: In the checkout journey of the users who dropped between checkout initiated and personal info, how many of them did a successful login?
-- ========================================================

WITH base AS (
    SELECT 
        identifiers_sessionid,
        identifiers_user_id,
        action,
        identifiers_log_time
    FROM eshop_data.es_events_v2
    WHERE identifiers_log_time >= date_diff('day', 14, current_timestamp) -- Filter for last 2 weeks
      AND lower(internalemployee) = 'no' -- Exclude internal employees
),
checkout_initiated AS (
    SELECT DISTINCT 
        identifiers_sessionid, 
        identifiers_user_id
    FROM base
    WHERE action = 'onecheckoutinitiated'
),
personal_info AS (
    SELECT DISTINCT 
        identifiers_sessionid
    FROM base
    WHERE action = 'personalinfopage'
)
SELECT 
    COUNT(DISTINCT c.identifiers_user_id) AS total_users,
    COUNT(DISTINCT c.identifiers_sessionid) AS total_sessions
FROM checkout_initiated c
LEFT JOIN personal_info p ON c.identifiers_sessionid = p.identifiers_sessionid
WHERE p.identifiers_sessionid IS NULL;"""

res = format_leading_commas(test_sql_user)
print("=== Formatted User Query ===")
print(res)

# Test 2: Query already having leading commas
already_formatted = """SELECT 
      identifiers_sessionid
    , identifiers_user_id
    , action
FROM eshop_data.es_events_v2;"""

res2 = format_leading_commas(already_formatted)
assert res2 == already_formatted, "Should not alter already formatted query"
print("\n✓ Idempotency test passed!")
