import subprocess
import json
import base64
import time

def execute_chrome_js(js_code_str):
    b64 = base64.b64encode(js_code_str.encode("utf-8")).decode("ascii")
    runner = f'tell application "Google Chrome" to execute front window\'s active tab javascript "try {{ eval(atob(\'{b64}\')); }} catch (e) {{ window.__metabase_table_sample = e.toString(); }}"'
    proc = subprocess.run(["osascript", "-e", runner], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return proc.stdout.strip()

run_query_js = """
window.__metabase_table_sample = null;
(async function() {
    try {
        const queryPayload = {
            database: 59,
            type: "native",
            native: {
                query: "SELECT * FROM eshop_data.es_events_arr_v2 LIMIT 100000"
            }
        };
        
        const res = await fetch('/api/dataset', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(queryPayload)
        });
        
        if (!res.ok) {
            window.__metabase_table_sample = "HTTP Error on /api/dataset: " + res.status;
            return;
        }
        
        const data = await res.json();
        
        if (data.error) {
            window.__metabase_table_sample = "Metabase Error: " + data.error;
            return;
        }
        
        window.__metabase_table_sample = JSON.stringify({
            status: "success",
            row_count: data.data && data.data.rows ? data.data.rows.length : 0,
            preview: data.data && data.data.rows ? data.data.rows.slice(0, 2) : "No rows"
        });
    } catch(e) {
        window.__metabase_table_sample = "JS Error: " + e.toString();
    }
})();
"""

print("Executing query on /api/dataset directly...")
try:
    execute_chrome_js(run_query_js)

    val = None
    for sec in range(120):
        time.sleep(2)
        val = execute_chrome_js("window.__metabase_table_sample")
        if val and val not in ("missing value", "null", ""):
            break

    print("Metabase Query Response:", val[:500] if val else "Still missing value")
except Exception as e:
    print(f"Exception: {e}")
