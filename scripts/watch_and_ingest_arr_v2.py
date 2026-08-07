import subprocess
import base64
import time
import os
import glob
import shutil
import json
import pandas as pd
from core.table_ingestion import TableSchemaIngestion
from neo4j import GraphDatabase

def execute_chrome_js(js_code_str):
    b64 = base64.b64encode(js_code_str.encode("utf-8")).decode("ascii")
    runner = f'tell application "Google Chrome" to execute front window\'s active tab javascript "try {{ eval(atob(\'{b64}\')); }} catch (e) {{ return \'ERROR: \' + e.toString(); }}"'
    proc = subprocess.run(["osascript", "-e", runner], capture_output=True, text=True)
    if proc.returncode != 0:
        return "OSASCRIPT_ERROR: " + proc.stderr
    return proc.stdout.strip()

print("=" * 60)
print("🚀 MONITORING QUERY EXECUTION & TRIGGERING CSV DOWNLOAD")
print("=" * 60)

download_dir = os.path.expanduser("~/Downloads")
initial_csvs = set(glob.glob(os.path.join(download_dir, "*.csv")))

js_watch_and_download = """
window.__dl_process_state = "WATCHING";
(function() {
    let seconds = 0;
    const interval = setInterval(() => {
        seconds++;
        try {
            // Check for completion by looking for the download icon or time display in footer
            const svgs = Array.from(document.querySelectorAll('svg'));
            
            // Find the download button (usually in bottom-right corner)
            let dlBtn = null;
            
            // Look for SVG or button containing download
            for (const s of svgs) {
                const parent = s.closest('button, a, div[role="button"]');
                if (parent) {
                    const aria = (parent.getAttribute('aria-label') || '').toLowerCase();
                    const title = (parent.getAttribute('title') || '').toLowerCase();
                    const rect = parent.getBoundingClientRect();
                    
                    // Located at bottom right
                    if (rect.bottom > window.innerHeight - 120 && rect.right > window.innerWidth - 300) {
                        if (aria.includes('download') || title.includes('download') || s.innerHTML.includes('M') || s.getAttribute('data-icon') === 'download') {
                            dlBtn = parent;
                            break;
                        }
                    }
                }
            }
            
            // Fallback: any button in bottom right corner that is not the right-sidebar toggle
            if (!dlBtn) {
                const rightBottoms = Array.from(document.querySelectorAll('button, div[role="button"]')).filter(b => {
                    const rect = b.getBoundingClientRect();
                    return rect.bottom > window.innerHeight - 100 && rect.right > window.innerWidth - 120 && rect.right < window.innerWidth - 10;
                });
                if (rightBottoms.length > 0) {
                    dlBtn = rightBottoms[0];
                }
            }

            if (dlBtn) {
                window.__dl_process_state = "FOUND_DOWNLOAD_BUTTON";
                dlBtn.click();
                
                // Once clicked, popover menu opens with CSV / XLSX / JSON options
                setTimeout(() => {
                    const options = Array.from(document.querySelectorAll('button, a, div, span, li, p'));
                    const csvOption = options.find(o => {
                        const txt = (o.textContent || '').trim().toLowerCase();
                        return txt === '.csv' || txt === 'csv' || txt.includes('csv');
                    });
                    
                    if (csvOption) {
                        csvOption.click();
                        window.__dl_process_state = "CLICKED_CSV_DOWNLOAD";
                        clearInterval(interval);
                    } else {
                        window.__dl_process_state = "POPOVER_OPENED_LOOKING_FOR_CSV";
                    }
                }, 1000);
            }

            if (seconds > 180) { // 6 mins max
                clearInterval(interval);
                window.__dl_process_state = "TIMEOUT_AFTER_6_MINS";
            }
        } catch(e) {
            window.__dl_process_state = "ERROR: " + e.toString();
            clearInterval(interval);
        }
    }, 2000);
})();
"""

execute_chrome_js(js_watch_and_download)

print("⏳ Waiting for query to finish execution and download button to be clicked...")
for i in range(180):
    time.sleep(2)
    state = execute_chrome_js("window.__dl_process_state")
    print(f"  Status [{i*2}s]: {state}")
    if state == "CLICKED_CSV_DOWNLOAD":
        print("🎉 Download button clicked successfully!")
        break
    elif "ERROR" in str(state) or "TIMEOUT" in str(state):
        print(f"❌ Stopped: {state}")
        break

# Wait for file to land in Downloads
print("\n📥 Monitoring ~/Downloads for newly generated CSV...")
downloaded_file = None
for _ in range(60):
    time.sleep(2)
    current_csvs = set(glob.glob(os.path.join(download_dir, "*.csv")))
    diff = current_csvs - initial_csvs
    if diff:
        candidate = max(diff, key=os.path.getmtime)
        crdown = glob.glob(os.path.join(download_dir, "*.crdownload"))
        if not crdown and os.path.getsize(candidate) > 0:
            downloaded_file = candidate
            print(f"✅ Found downloaded CSV: {downloaded_file} ({os.path.getsize(downloaded_file):,} bytes)")
            break

dest_csv = "data/schema_samples/eshop_data_es_events_arr_v2.csv"
dest_schema_json = "data/schema_samples/eshop_data_es_events_arr_v2_schema.json"

if downloaded_file and os.path.exists(downloaded_file):
    os.makedirs("data/schema_samples", exist_ok=True)
    shutil.copy(downloaded_file, dest_csv)
    print(f"✅ Copied to project: {dest_csv}")
else:
    print("⚠️ File not automatically detected in ~/Downloads yet.")
    if not os.path.exists(dest_csv):
        print("Please ensure the CSV is placed at:", dest_csv)
        exit(1)

# Profile DataFrame and create Schema JSON
print("\n📊 Profiling data sample and generating Schema JSON...")
df = pd.read_csv(dest_csv, low_memory=False)
print(f"Loaded DataFrame: {df.shape[0]:,} rows x {df.shape[1]} columns.")

schema_info = {
    "table_name": "eshop_data.es_events_arr_v2",
    "database_id": 59,
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
    
with open(dest_schema_json, "w") as f:
    json.dump(schema_info, f, indent=2)
print(f"✅ Schema JSON saved to: {dest_schema_json}")

# Ingest into Neo4j
print("\n🧠 Ingesting eshop_data.es_events_arr_v2 into Neo4j Knowledge Graph...")
ingestion = TableSchemaIngestion()
res = ingestion.ingest_schema(schema_info)
print(f"✅ Ingested table: {res['table_name']} with {res['columns_ingested']} columns. Status: {res['status']}")

# Verify
driver = GraphDatabase.driver("neo4j://127.0.0.1:7687", auth=("neo4j", "password"))
with driver.session(database="neo4j") as session:
    t_cnt = session.run("MATCH (t:Table) RETURN count(t) as c").single()["c"]
    c_cnt = session.run("MATCH (c:Column) RETURN count(c) as c").single()["c"]
    arr_cnt = session.run("MATCH (t:Table {name: 'eshop_data.es_events_arr_v2'})-[:CONTAINS_COLUMN]->(c:Column) RETURN count(c) as c").single()["c"]
    
    print("\n=======================================================")
    print(" 🌟 NEO4J KNOWLEDGE GRAPH STATUS")
    print("=======================================================")
    print(f"  • Total Tables in Graph:             {t_cnt}")
    print(f"  • Columns in eshop_data.es_events_arr_v2: {arr_cnt}")
    print(f"  • Total Columns in Graph:            {c_cnt}")
    print("=======================================================")
driver.close()
