import re

def format_leading_commas(sql: str) -> str:
    """
    Ensures that SQL projections, column lists, and CTE definitions use leading commas (comma-first style),
    making it easy for analysts to comment out individual lines or entire CTEs without comma syntax errors.
    Safely preserves comments, function calls (COALESCE, date_diff, etc.), and CTE structures.
    """
    if not sql:
        return sql

    # 1. Format CTE definitions: Change "),\n<cte_name> AS (" to ")\n\n, <cte_name> AS ("
    def replace_cte_comma(match):
        comment = match.group(1) or ""
        cte_name = match.group(2)
        if comment.strip():
            return f") {comment.strip()}\n\n, {cte_name} AS ("
        return f")\n\n, {cte_name} AS ("

    sql = re.sub(r"\)\s*,\s*(--[^\n]*)?\n\s*([a-zA-Z0-9_]+)\s+AS\s*\(", replace_cte_comma, sql, flags=re.IGNORECASE)

    # 2. Format SELECT projections (column-by-column leading commas)
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

# Test idempotency and comments
test_with_comments = """WITH cte1 AS (
    SELECT 
        a,
        b
    FROM table1
), -- comment after cte1

cte2 AS (
    SELECT 
        c,
        d
    FROM table2
)

SELECT 
    a,
    c
FROM cte1
JOIN cte2 ON cte1.id = cte2.id;"""

res = format_leading_commas(test_with_comments)
print(res)

# Check idempotency
res2 = format_leading_commas(res)
assert res == res2, "Should be idempotent"
print("\n✓ Idempotent test passed!")
