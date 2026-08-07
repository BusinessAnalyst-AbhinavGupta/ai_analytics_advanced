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

def run_applescript(script):
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if proc.returncode != 0:
        print("AppleScript Error:", proc.stderr)
    return proc.stdout.strip()

def run_metabase_pipeline():
    print("=" * 60)
    print("🚀 AUTOMATING METABASE QUERY & EXTRACTION FOR: eshop_data.es_events_arr_v2")
    print("=" * 60)

    target_url = "https://metabase.om.yo-digital.com/question/38588-test-question"
    
    # 1. Activate Chrome and navigate
    print(f"\n1. Ensuring Chrome is active and at URL: {target_url}")
    nav_script = f"""
    tell application "Google Chrome"
        activate
        if (count every window) = 0 then
            make new window
        end if
        set current_url to URL of active tab of front window
        if current_url does not contain "38588" then
            set URL of active tab of front window to "{target_url}"
        end if
    end tell
    """
    run_applescript(nav_script)
    time.sleep(5)

    # 2. Check for "Open Editor" or editor presence
    print("\n2. Checking for 'Open Editor' button or existing SQL editor...")
    js_open_editor = """
    (function() {
        // Look for open editor button
        const btns = Array.from(document.querySelectorAll('button, a, div[role="button"], span'));
        const openBtn = btns.find(b => (b.textContent || '').trim().toLowerCase() === 'open editor');
        if (openBtn) {
            openBtn.click();
            return "Clicked 'Open editor'";
        }
        return "Editor already open or button not needed";
    })();
    """
    res_editor = execute_chrome_js(js_open_editor)
    print(f"   Editor status: {res_editor}")
    time.sleep(2)

    # 3. Focus editor and put the query
    sql_query = "SELECT * FROM eshop_data.es_events_arr_v2 LIMIT 100000;"
    print(f"\n3. Setting SQL Query: {sql_query}")
    
    js_set_sql = f"""
    (function() {{
        try {{
            if (window.monaco && monaco.editor && monaco.editor.getModels().length > 0) {{
                monaco.editor.getModels()[0].setValue("{sql_query}");
                return "Monaco setValue success";
            }}
            const aceEl = document.querySelector('.ace_editor');
            if (aceEl && window.ace) {{
                ace.edit(aceEl).setValue("{sql_query}");
                return "Ace setValue success";
            }}
            return "Fallback to Keystrokes";
        }} catch(e) {{
            return "Error: " + e.toString();
        }}
    }})();
    """
    set_res = execute_chrome_js(js_set_sql)
    print(f"   Direct editor set result: {set_res}")

    # Fallback / Guarantee via Keystrokes & Cmd+Enter
    print("   Sending keystrokes to ensure replacement & trigger execution (Cmd+Enter)...")
    keystroke_script = f"""
    tell application "Google Chrome" to activate
    delay 0.5
    tell application "System Events"
        -- Select all in editor
        keystroke "a" using command down
        delay 0.2
        key code 51 -- Delete
        delay 0.2
        keystroke "{sql_query}"
        delay 0.5
        key code 36 using command down -- Cmd + Enter
    end tell
    """
    run_applescript(keystroke_script)
    print("   Query submitted!")

    # 4. Wait for query to complete and click Download button
    print("\n4. Monitoring query execution and looking for download icon at bottom-right...")
    
    # Record current downloads before clicking
    download_dir = os.path.expanduser("~/Downloads")
    initial_csvs = set(glob.glob(os.path.join(download_dir, "*.csv")))

    js_click_download = """
    window.__download_status = "SEARCHING";
    (function() {
        let attempts = 0;
        const interval = setInterval(() => {
            attempts++;
            try {
                // Look for download icon next to runtime (bottom right)
                const allButtons = Array.from(document.querySelectorAll('button, a, div[role="button"], span'));
                
                // Find download button: aria-label, title, or SVG path
                const dlBtn = allButtons.find(el => {
                    const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                    const title = (el.getAttribute('title') || '').toLowerCase();
                    const hasDlSvg = !!el.querySelector('svg[data-icon="download"]') || !!el.querySelector('svg');
                    const text = (el.textContent || '').toLowerCase();
                    return aria.includes('download') || title.includes('download') || (hasDlSvg && text.includes('download'));
                }) || document.querySelector('[data-testid="download-button"]') || document.querySelector('.icon-download');

                // If not found by label, look specifically around the bottom bar
                let targetBtn = dlBtn;
                if (!targetBtn) {
                    const svgs = Array.from(document.querySelectorAll('svg'));
                    for (const s of svgs) {
                        const parent = s.closest('button, a, div[role="button"]');
                        if (parent) {
                            const rect = parent.getBoundingClientRect();
                            // Check if near bottom right corner of viewport
                            if (rect.bottom > window.innerHeight - 150 && rect.right > window.innerWidth - 300) {
                                targetBtn = parent;
                                break;
                            }
                        }
                    }
                }

                if (targetBtn) {
                    targetBtn.click();
                    window.__download_status = "OPENED_DOWNLOAD_MENU";
                    
                    // Click CSV option
                    setTimeout(() => {
                        const options = Array.from(document.querySelectorAll('button, a, div, span, li'));
                        const csvBtn = options.find(o => {
                            const t = (o.textContent || '').trim().toLowerCase();
                            return t === '.csv' || t === 'csv' || t.includes('download csv');
                        });
                        if (csvBtn) {
                            csvBtn.click();
                            window.__download_status = "CLICKED_CSV";
                            clearInterval(interval);
                        }
                    }, 1200);
                }

                if (attempts > 180) { // 6 minutes timeout
                    clearInterval(interval);
                    window.__download_status = "TIMEOUT";
                }
            } catch(e) {
                window.__download_status = "ERROR: " + e.toString();
                clearInterval(interval);
            }
        }, 2000);
    })();
    """
    execute_chrome_js(js_click_download)

    clicked = False
    for i in range(180):
        time.sleep(2)
        status = execute_chrome_js("window.__download_status")
        print(f"   Polling status [{i*2}s]: {status}")
        if status == "CLICKED_CSV":
            print("   🎉 Successfully clicked Download CSV!")
            clicked = True
            break
        elif "ERROR" in str(status) or status == "TIMEOUT":
            print(f"   ❌ Stopped with status: {status}")
            break

    # 5. Wait for the new file in ~/Downloads
    print("\n5. Waiting for downloaded CSV in ~/Downloads...")
    new_csv_path = None
    for _ in range(60):
        time.sleep(2)
        current_csvs = set(glob.glob(os.path.join(download_dir, "*.csv")))
        diff = current_csvs - initial_csvs
        if diff:
            new_csv_path = max(diff, key=os.path.getmtime)
            # Check if download finished (file size > 0 and no .crdownload)
            crdownloads = glob.glob(os.path.join(download_dir, "*.crdownload"))
            if not crdownloads and os.path.getsize(new_csv_path) > 0:
                print(f"   📥 New file downloaded: {new_csv_path} ({os.path.getsize(new_csv_path):,} bytes)")
                break

    dest_csv = "data/schema_samples/eshop_data_es_events_arr_v2.csv"
    dest_schema_json = "data/schema_samples/eshop_data_es_events_arr_v2_schema.json"
    
    if new_csv_path and os.path.exists(new_csv_path):
        os.makedirs("data/schema_samples", exist_ok=True)
        shutil.copy(new_csv_path, dest_csv)
        print(f"   ✅ Copied sample to: {dest_csv}")
    else:
        print("   ⚠️ Could not automatically find new CSV in ~/Downloads. Checking if existing sample is present...")
        if not os.path.exists(dest_csv):
            print("   Please save the CSV to: data/schema_samples/eshop_data_es_events_arr_v2.csv")
            return

    # 6. Profile DataFrame and build schema JSON
    print("\n6. Profiling CSV dataset to build schema metadata...")
    df = pd.read_csv(dest_csv, low_memory=False)
    print(f"   Loaded {df.shape[0]:,} rows and {df.shape[1]} columns.")
    
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
    print(f"   ✅ Saved Schema & Profile to: {dest_schema_json}")

    # 7. Ingest into Neo4j Knowledge Graph
    print("\n7. Ingesting table schema into Neo4j Knowledge Graph...")
    ingestion = TableSchemaIngestion()
    res = ingestion.ingest_schema(schema_info)
    print(f"   ✅ Ingestion Complete: {res['table_name']} ({res['columns_ingested']} columns ingested, status: {res['status']})")

    # 8. Verify Graph State
    driver = GraphDatabase.driver("neo4j://127.0.0.1:7687", auth=("neo4j", "password"))
    with driver.session(database="neo4j") as session:
        t_count = session.run("MATCH (t:Table) RETURN count(t) as c").single()["c"]
        c_count = session.run("MATCH (c:Column) RETURN count(c) as c").single()["c"]
        r_count = session.run("MATCH ()-[r]->() RETURN count(r) as c").single()["c"]
        arr_cols = session.run("MATCH (t:Table {name: 'eshop_data.es_events_arr_v2'})-[:CONTAINS_COLUMN]->(c:Column) RETURN count(c) as c").single()["c"]
        
        print("\n=======================================================")
        print(" 🌟 NEO4J KNOWLEDGE GRAPH STATUS")
        print("=======================================================")
        print(f"  • Total Tables:                     {t_count}")
        print(f"  • Columns in eshop_data.es_events_arr_v2: {arr_cols}")
        print(f"  • Total Columns in Graph:           {c_count}")
        print(f"  • Total Relationships:              {r_count}")
        print("=======================================================")
    driver.close()

if __name__ == "__main__":
    run_metabase_pipeline()
