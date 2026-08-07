import os
import json
import time
import base64
import subprocess
import logging
import pandas as pd
from typing import Dict, Any, Optional
from core.table_ingestion import TableSchemaIngestion
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

class TableSchemaFetcher:
    """
    Auto-discovery and profiling service for physical root tables.
    Automatically checks if a table is profiled in Neo4j / schema_samples,
    and if missing, uses the active Chrome Metabase session to pull a sample,
    profile it, and ingest all physical columns into Neo4j.
    """
    
    def __init__(self, schema_samples_dir: str = "data/schema_samples"):
        self.schema_samples_dir = schema_samples_dir
        os.makedirs(self.schema_samples_dir, exist_ok=True)
        self.table_ingestion = TableSchemaIngestion()

    @staticmethod
    def clean_table_filename(table_name: str) -> str:
        return table_name.replace(".", "_").replace(" ", "_").replace('"', "").replace("`", "").lower()

    @staticmethod
    def is_chrome_running() -> bool:
        """Check if Google Chrome is running on macOS."""
        try:
            res = subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True, text=True)
            return res.returncode == 0
        except Exception:
            return False

    @staticmethod
    def execute_chrome_js(js_code_str: str) -> str:
        """Executes JavaScript specifically on the Metabase tab in Google Chrome."""
        b64 = base64.b64encode(js_code_str.encode("utf-8")).decode("ascii")
        runner = f'''
        tell application "Google Chrome"
            set foundTab to false
            set resultStr to ""
            repeat with w in windows
                repeat with t in tabs of w
                    if URL of t contains "metabase" then
                        set resultStr to (execute t javascript "try {{ eval(atob('{b64}')); }} catch (e) {{ 'ERROR: ' + e.toString(); }}")
                        set foundTab to true
                        exit repeat
                    end if
                end repeat
                if foundTab then exit repeat
            end repeat
            if not foundTab then
                error "NO_METABASE_TAB_FOUND"
            end if
            return resultStr
        end tell
        '''
        proc = subprocess.run(["osascript", "-e", runner], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "AppleScript execution error")
        return proc.stdout.strip()

    def is_table_already_ingested(self, table_name: str) -> bool:
        """Checks if the table is already profiled in Neo4j with physical columns."""
        clean_name = self.clean_table_filename(table_name)
        schema_json_path = os.path.join(self.schema_samples_dir, f"{clean_name}_schema.json")
        
        # Check local schema profile cache
        if os.path.exists(schema_json_path):
            try:
                with open(schema_json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("columns") and len(data["columns"]) > 0:
                        return True
            except Exception:
                pass

        # Check Neo4j Knowledge Graph
        try:
            driver = GraphDatabase.driver(self.table_ingestion.uri, auth=self.table_ingestion.auth)
            with driver.session(database=self.table_ingestion.database) as session:
                res = session.run("""
                MATCH (t:Table {name: $tbl})-[:HAS_COLUMN]->(c:Column)
                RETURN count(c) as cols
                """, tbl=table_name).single()
                if res and res["cols"] > 5: # If it has more than just a few query-level columns
                    driver.close()
                    return True
            driver.close()
        except Exception as e:
            logger.warning(f"Could not verify Neo4j table status for {table_name}: {e}")

        return False

    def fetch_sample_from_metabase(self, table_name: str, database_id: int = 59, limit: int = 100000) -> Optional[Dict[str, Any]]:
        """
        Extracts sample rows via active Chrome Metabase session using native MBQL endpoint.
        Returns profiled schema dictionary.
        """
        if not self.is_chrome_running():
            logger.info(f"Google Chrome is not running. Skipping auto-download for {table_name}.")
            return None

        clean_name = self.clean_table_filename(table_name)
        output_csv_path = os.path.join(self.schema_samples_dir, f"{clean_name}.csv")
        output_schema_json = os.path.join(self.schema_samples_dir, f"{clean_name}_schema.json")

        print(f"\n⚡ [Auto-Discovery Hook] Found new root table: {table_name}")
        print(f"📡 Fetching {limit:,} sample rows via Metabase API in Chrome...")

        query = f"SELECT * FROM {table_name} LIMIT {limit};"
        
        # Dispatch async fetch in Chrome context
        dispatch_js = f"""
        window.__auto_csv_res = "RUNNING";
        window.__auto_csv_data = null;
        (async function() {{
            try {{
                const queryPayload = {{
                    database: {database_id},
                    "lib/type": "mbql/query",
                    stages: [
                        {{
                            "lib/type": "mbql.stage/native",
                            native: "{query}",
                            "template-tags": []
                        }}
                    ]
                }};
                
                const res = await fetch('/api/dataset/csv', {{
                    method: 'POST',
                    credentials: 'include',
                    headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
                    body: new URLSearchParams({{ query: JSON.stringify(queryPayload) }})
                }});
                
                if (!res.ok) {{
                    window.__auto_csv_res = 'ERROR: ' + res.status + ' ' + (await res.text()).slice(0, 150);
                    return;
                }}
                
                const text = await res.text();
                window.__auto_csv_data = text;
                window.__auto_csv_res = 'SUCCESS: ' + text.length + ' bytes';
            }} catch(e) {{
                window.__auto_csv_res = 'EXCEPTION: ' + e.toString();
            }}
        }})();
        window.__auto_csv_res;
        """

        try:
            self.execute_chrome_js(dispatch_js)
        except Exception as e:
            logger.warning(f"Could not dispatch Metabase request in Chrome: {e}")
            return None

        # Wait for query execution and download stream
        max_wait_secs = 1200  # 20 minutes timeout
        status = "RUNNING"
        for _ in range(max_wait_secs // 2):
            time.sleep(2)
            try:
                status = self.execute_chrome_js("window.__auto_csv_res")
                if "SUCCESS" in status:
                    break
                elif "ERROR" in status or "EXCEPTION" in status:
                    logger.warning(f"Metabase auto-extract failed for {table_name}: {status}")
                    return None
            except Exception:
                pass

        if "SUCCESS" not in status:
            logger.warning(f"Metabase auto-extract timed out for {table_name} after {max_wait_secs}s.")
            return None

        # Stream data from browser memory to disk
        try:
            total_len_str = self.execute_chrome_js("window.__auto_csv_data ? window.__auto_csv_data.length.toString() : '0'")
            total_len = int(total_len_str)
            if total_len == 0:
                return None

            chunk_size = 500000
            with open(output_csv_path, "w", encoding="utf-8") as f:
                for offset in range(0, total_len, chunk_size):
                    chunk_js = f"window.__auto_csv_data.substring({offset}, {offset + chunk_size})"
                    chunk = self.execute_chrome_js(chunk_js)
                    f.write(chunk)

            print(f"✅ Downloaded {total_len:,} bytes to {output_csv_path}")

            # Profile with Pandas
            df = pd.read_csv(output_csv_path, low_memory=False)
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

            with open(output_schema_json, "w", encoding="utf-8") as f:
                json.dump(schema_info, f, indent=2)

            return schema_info

        except Exception as e:
            logger.error(f"Error streaming or profiling CSV for {table_name}: {e}")
            return None

    def ensure_table_schema_ingested(self, table_name: str, database_id: int = 59) -> bool:
        """
        Ensures the physical table and all its columns are in Neo4j.
        If missing, automatically pulls, profiles, and ingests.
        """
        table_name = table_name.strip()
        if not table_name:
            return False

        # 1. Check if already ingested
        if self.is_table_already_ingested(table_name):
            # Check if local schema JSON exists to ensure Neo4j has full column details
            clean_name = self.clean_table_filename(table_name)
            schema_json_path = os.path.join(self.schema_samples_dir, f"{clean_name}_schema.json")
            if os.path.exists(schema_json_path):
                try:
                    with open(schema_json_path, "r", encoding="utf-8") as f:
                        schema_data = json.load(f)
                    self.table_ingestion.ingest_schema(schema_data)
                except Exception:
                    pass
            return True

        # 2. Not found, auto-fetch from Metabase
        schema_info = self.fetch_sample_from_metabase(table_name, database_id=database_id)
        if schema_info:
            res = self.table_ingestion.ingest_schema(schema_info)
            print(f"🚀 [Auto-Discovery Hook] Successfully ingested full schema for {table_name} ({res['columns_ingested']} columns) into Neo4j!")
            return True

        return False
