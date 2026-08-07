import subprocess
import json
import base64
import time
import os
import pandas as pd

def execute_chrome_js(js_code_str):
    b64 = base64.b64encode(js_code_str.encode("utf-8")).decode("ascii")
    runner = f'tell application "Google Chrome" to execute front window\'s active tab javascript "eval(atob(\'{b64}\'))"'
    proc = subprocess.run(["osascript", "-e", runner], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return proc.stdout.strip()

def download_table_csv(database_id: int, table_name: str, output_csv_path: str, limit: int = 10000):
    print(f"\n=======================================================")
    print(f" EXTRACTING SAMPLE FOR TABLE: {table_name}")
    print(f" Database ID: {database_id} | Limit: {limit}")
    print(f"=======================================================")
    
    query = f"SELECT * FROM {table_name} LIMIT {limit}"
    
    # 1. Trigger query in Metabase
    trigger_js = f"""
    window.__current_csv_data = null;
    window.__csv_error = null;
    window.__csv_ready = false;
    (async function() {{
        try {{
            const queryPayload = {{
                database: {database_id},
                type: "native",
                native: {{
                    query: "{query}"
                }}
            }};
            const res = await fetch('/api/dataset/csv', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
                body: new URLSearchParams({{ query: JSON.stringify(queryPayload) }})
            }});
            if (!res.ok) {{
                const errText = await res.text();
                window.__csv_error = 'Metabase query error: ' + errText;
                window.__csv_ready = true;
                return;
            }}
            window.__current_csv_data = await res.text();
            window.__csv_ready = true;
        }} catch(e) {{
            window.__csv_error = e.toString();
            window.__csv_ready = true;
        }}
    }})();
    """
    
    print(f"Sending query to Metabase...")
    execute_chrome_js(trigger_js)
    
    # 2. Wait for completion
    for sec in range(90):
        time.sleep(2)
        ready = execute_chrome_js("window.__csv_ready")
        if ready == "true":
            break
        print(f"  Waiting for query execution... ({sec*2}s)")
        
    err = execute_chrome_js("window.__csv_error")
    if err and err not in ("missing value", "null", ""):
        raise RuntimeError(f"Error querying {table_name}: {err}")
        
    length_str = execute_chrome_js("window.__current_csv_data ? window.__current_csv_data.length : 0")
    total_len = int(length_str) if length_str.isdigit() else 0
    print(f"Query returned {total_len:,} characters of CSV data.")
    
    if total_len == 0:
        raise RuntimeError("Received empty response from Metabase.")
        
    # 3. Stream chunks out of browser memory to disk
    os.makedirs(os.path.dirname(output_csv_path), exist_ok=True)
    chunk_size = 250000  # 250KB chunk safe for osascript
    
    with open(output_csv_path, "w", encoding="utf-8") as f:
        for offset in range(0, total_len, chunk_size):
            chunk_js = f"window.__current_csv_data.substring({offset}, {offset + chunk_size})"
            chunk = execute_chrome_js(chunk_js)
            f.write(chunk)
            print(f"  Downloaded chunk {offset:,} / {total_len:,} bytes...", end="\r")
            
    print(f"\nSuccessfully saved CSV to: {output_csv_path}")
    
    # 4. Generate Schema & Profile JSON
    df = pd.read_csv(output_csv_path, low_memory=False)
    print(f"Loaded DataFrame: {df.shape[0]} rows x {df.shape[1]} columns.")
    
    schema_info = {
        "table_name": table_name,
        "database_id": database_id,
        "row_count": len(df),
        "column_count": len(df.columns),
        "columns": {}
    }
    
    for col in df.columns:
        series = df[col]
        non_null = series.dropna()
        distinct_vals = non_null.unique()
        sample_vals = [str(x) for x in distinct_vals[:10]]
        
        schema_info["columns"][col] = {
            "name": col,
            "dtype": str(series.dtype),
            "null_count": int(series.isna().sum()),
            "null_pct": round(float(series.isna().mean() * 100), 2),
            "distinct_count": int(len(distinct_vals)),
            "sample_values": sample_vals
        }
        
    schema_json_path = output_csv_path.replace(".csv", "_schema.json")
    with open(schema_json_path, "w") as f:
        json.dump(schema_info, f, indent=2)
    print(f"Saved Schema & Profile to: {schema_json_path}")
    return schema_info

if __name__ == "__main__":
    os.makedirs("data/schema_samples", exist_ok=True)
    
    # Table 1: Checkout Journey Silver Table
    download_table_csv(
        database_id=59,
        table_name="silver_layer.t_link_journey_checkout_com",
        output_csv_path="data/schema_samples/silver_layer_t_link_journey_checkout_com.csv",
        limit=10000
    )
    
    # Table 2: E-shop Raw Events V2
    download_table_csv(
        database_id=59,
        table_name="eshop_data.es_events_v2",
        output_csv_path="data/schema_samples/eshop_data_es_events_v2.csv",
        limit=10000
    )
