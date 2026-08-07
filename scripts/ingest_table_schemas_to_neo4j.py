import os
import json
from core.table_ingestion import TableSchemaIngestion

def ingest_base_tables():
    print("Ingesting base table schemas into Neo4j Knowledge Graph...")
    ingestion = TableSchemaIngestion()
    
    # 1. Ingest silver_layer.t_link_journey_checkout_com
    schema_path_1 = "data/schema_samples/silver_layer_t_link_journey_checkout_com_schema.json"
    with open(schema_path_1) as f:
        s1 = json.load(f)
    res1 = ingestion.ingest_schema(s1)
    print(f"Ingested {res1['table_name']}: {res1['columns_ingested']} columns. Status: {res1['status']}")
    
    # 2. Ingest eshop_data.es_events_v2
    schema_path_2 = "data/schema_samples/eshop_data_es_events_v2_schema.json"
    with open(schema_path_2) as f:
        s2 = json.load(f)
    res2 = ingestion.ingest_schema(s2)
    print(f"Ingested {res2['table_name']}: {res2['columns_ingested']} columns. Status: {res2['status']}")
    
    # 3. Connect Metrics to Columns where applicable
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver("neo4j://127.0.0.1:7687", auth=("neo4j", "password"))
    with driver.session(database="neo4j") as session:
        # Link metrics to checkout silver table
        session.run("""
        MATCH (m:Metric), (t:Table {name: 'silver_layer.t_link_journey_checkout_com'})
        MERGE (m)-[:USES_TABLE]->(t)
        """)
        
        # Link checkout step metrics to page_name column
        session.run("""
        MATCH (m:Metric), (c:Column {id: 'silver_layer.t_link_journey_checkout_com.page_name'})
        MERGE (m)-[:USES_COLUMN]->(c)
        """)
        
        # Link action metrics to action column
        session.run("""
        MATCH (m:Metric), (c:Column {id: 'silver_layer.t_link_journey_checkout_com.action'})
        MERGE (m)-[:USES_COLUMN]->(c)
        """)
        
        # Count stats
        tables_cnt = session.run("MATCH (t:Table) RETURN count(t) as c").single()["c"]
        cols_cnt = session.run("MATCH (c:Column) RETURN count(c) as c").single()["c"]
        rels_cnt = session.run("MATCH ()-[r]->() RETURN count(r) as c").single()["c"]
        metrics_cnt = session.run("MATCH (m:Metric) RETURN count(m) as c").single()["c"]
        
        print("\n=======================================================")
        print(" NEO4J KNOWLEDGE GRAPH STATUS")
        print("=======================================================")
        print(f"  • Total Metrics:       {metrics_cnt}")
        print(f"  • Total Tables:        {tables_cnt}")
        print(f"  • Total Columns:       {cols_cnt}")
        print(f"  • Total Relationships: {rels_cnt}")
        print("=======================================================")
    driver.close()

if __name__ == "__main__":
    ingest_base_tables()
