import os
import json
import pandas as pd
from typing import Dict, Any, List, Optional
from neo4j import GraphDatabase

class TableSchemaIngestion:
    """
    Profiles table data from CSV or schema JSON and ingests
    :Table and :Column nodes and relationships into the Neo4j Knowledge Graph.
    """
    
    def __init__(self, uri: str = None, auth: tuple = None, database: str = "neo4j"):
        self.uri = uri or os.getenv("NEO4J_URI", "neo4j://127.0.0.1:7687")
        self.auth = auth or (os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "password"))
        self.database = database or "neo4j"
        os.makedirs("data/schema_samples", exist_ok=True)
        
    def profile_csv(self, csv_file_or_path, table_name: str, database_name: str = "Athena") -> Dict[str, Any]:
        """Profiles a CSV file or uploaded file buffer and returns a rich schema dictionary."""
        df = None
        encodings = ["utf-8", "utf-8-sig", "latin1", "cp1252", "iso-8859-1"]
        
        for enc in encodings:
            try:
                if hasattr(csv_file_or_path, "seek"):
                    csv_file_or_path.seek(0)
                df = pd.read_csv(csv_file_or_path, low_memory=False, encoding=enc, on_bad_lines="skip")
                break
            except Exception:
                continue
                
        if df is None:
            for enc in ["utf-8", "latin1"]:
                try:
                    if hasattr(csv_file_or_path, "seek"):
                        csv_file_or_path.seek(0)
                    df = pd.read_csv(csv_file_or_path, low_memory=False, sep=None, engine="python", encoding=enc, on_bad_lines="skip")
                    break
                except Exception:
                    continue
                    
        if df is None:
            raise ValueError(f"Could not parse CSV file. Please verify it is a valid CSV or UTF-8/Latin-1 text file.")
            
        if hasattr(csv_file_or_path, "seek"):
            csv_file_or_path.seek(0)
            
        schema_info = {
            "table_name": table_name,
            "database_name": database_name,
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
            
        return schema_info

    def generate_cypher(self, schema_info: Dict[str, Any]) -> List[str]:
        """Generates Cypher queries for Table and Column nodes and connections."""
        queries = []
        tbl_name = schema_info["table_name"]
        db_name = schema_info.get("database_name", "Athena")
        row_cnt = schema_info.get("row_count", 0)
        col_cnt = schema_info.get("column_count", 0)
        
        # 1. Merge Table node
        tbl_query = (
            f"MERGE (t:Table {{name: '{tbl_name}'}})\n"
            f"SET t.database = '{db_name}',\n"
            f"    t.row_count = {row_cnt},\n"
            f"    t.column_count = {col_cnt},\n"
            f"    t.last_updated = datetime()"
        )
        queries.append(tbl_query)
        
        # 2. Merge Column nodes and connect to Table
        for col_name, col_data in schema_info.get("columns", {}).items():
            dtype = col_data.get("dtype", "string")
            null_pct = col_data.get("null_pct", 0.0)
            distinct_cnt = col_data.get("distinct_count", 0)
            samples_str = json.dumps(col_data.get("sample_values", [])).replace("'", "\\'")
            
            col_query = (
                f"MERGE (c:Column {{id: '{tbl_name}.{col_name}'}})\n"
                f"SET c.name = '{col_name}',\n"
                f"    c.table_name = '{tbl_name}',\n"
                f"    c.dtype = '{dtype}',\n"
                f"    c.null_pct = {null_pct},\n"
                f"    c.distinct_count = {distinct_cnt},\n"
                f"    c.sample_values = '{samples_str}'\n"
                f"WITH c\n"
                f"MATCH (t:Table {{name: '{tbl_name}'}})\n"
                f"MERGE (t)-[:HAS_COLUMN]->(c)"
            )
            queries.append(col_query)
            
        # 3. Connect existing Metric nodes that reference this table in their SQL or metadata
        link_query = (
            f"MATCH (m:Metric)\n"
            f"WHERE m.description CONTAINS '{tbl_name}' OR m.name CONTAINS '{tbl_name}'\n"
            f"MATCH (t:Table {{name: '{tbl_name}'}})\n"
            f"MERGE (m)-[:USES_TABLE]->(t)"
        )
        queries.append(link_query)
        
        return queries

    def ingest_schema(self, schema_info: Dict[str, Any]) -> Dict[str, Any]:
        """Executes the Cypher statements to insert Table and Column nodes into Neo4j."""
        queries = self.generate_cypher(schema_info)
        driver = GraphDatabase.driver(self.uri, auth=self.auth)
        
        try:
            with driver.session(database=self.database) as session:
                def work(tx):
                    tx_results = []
                    for q in queries:
                        res = tx.run(q)
                        tx_results.append(res.consume())
                    return tx_results
                session.execute_write(work)
        finally:
            driver.close()
            
        return {
            "table_name": schema_info["table_name"],
            "columns_ingested": len(schema_info.get("columns", {})),
            "status": "SUCCESS"
        }
