import subprocess
import base64
import time
import os
import json
import pandas as pd
from core.table_ingestion import TableSchemaIngestion
from neo4j import GraphDatabase

def execute_chrome_js(js_code_str):
    b64 = base64.b64encode(js_code_str.encode("utf-8")).decode("ascii")
    runner = f'tell application "Google Chrome" to execute front window\'s active tab javascript "eval(atob(\'{b64}\'))"'
    proc = subprocess.run(["osascript", "-e", runner], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return proc.stdout.strip()

print("=" * 60)
print("📥 STREAMING 28.4 MB CSV FROM BROWSER MEMORY TO DISK")
print("=" * 60)

total_len_str = execute_chrome_js("window.__csv_exported.length.toString()")
total_len = int(total_len_str)
print(f"Total CSV Characters to Stream: {total_len:,}")

output_csv_path = "data/schema_samples/eshop_data_es_events_arr_v2.csv"
os.makedirs(os.path.dirname(output_csv_path), exist_ok=True)

chunk_size = 500000  # 500 KB per chunk
with open(output_csv_path, "w", encoding="utf-8") as f:
    for offset in range(0, total_len, chunk_size):
        chunk_js = f"window.__csv_exported.substring({offset}, {offset + chunk_size})"
        chunk = execute_chrome_js(chunk_js)
        f.write(chunk)
        print(f"  Streamed {min(offset + chunk_size, total_len):,} / {total_len:,} bytes...", end="\r")

print(f"\n✅ Successfully written 100k sample rows to: {output_csv_path}")

# Profile dataset and create schema JSON
print("\n📊 Profiling data sample and generating Schema JSON...")
df = pd.read_csv(output_csv_path, low_memory=False)
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

schema_json_path = "data/schema_samples/eshop_data_es_events_arr_v2_schema.json"
with open(schema_json_path, "w") as f:
    json.dump(schema_info, f, indent=2)
print(f"✅ Schema JSON saved to: {schema_json_path}")

# Ingest into Neo4j
print("\n🧠 Ingesting eshop_data.es_events_arr_v2 schema into Neo4j Knowledge Graph...")
ingestion = TableSchemaIngestion()
res = ingestion.ingest_schema(schema_info)
print(f"✅ Ingested table: {res['table_name']} with {res['columns_ingested']} columns. Status: {res['status']}")

# Verify Neo4j
driver = GraphDatabase.driver("neo4j://127.0.0.1:7687", auth=("neo4j", "password"))
with driver.session(database="neo4j") as session:
    t_cnt = session.run("MATCH (t:Table) RETURN count(t) as c").single()["c"]
    c_cnt = session.run("MATCH (c:Column) RETURN count(c) as c").single()["c"]
    arr_cols = session.run("MATCH (t:Table {name: 'eshop_data.es_events_arr_v2'})-[:CONTAINS_COLUMN]->(c:Column) RETURN count(c) as c").single()["c"]
    arr_table = session.run("MATCH (t:Table {name: 'eshop_data.es_events_arr_v2'}) RETURN t.name as name, t.row_count as rows, t.column_count as cols").single()
    
    print("\n=======================================================")
    print(" 🌟 NEO4J KNOWLEDGE GRAPH STATUS")
    print("=======================================================")
    print(f"  • Ingested Table:                   {arr_table['name']}")
    print(f"  • Sample Rows Profiled:             {arr_table['rows']:,}")
    print(f"  • Table Columns Ingested:           {arr_table['cols']}")
    print(f"  • Total Tables in Knowledge Graph:  {t_cnt}")
    print(f"  • Total Columns in Knowledge Graph: {c_cnt}")
    print("=======================================================")
driver.close()
