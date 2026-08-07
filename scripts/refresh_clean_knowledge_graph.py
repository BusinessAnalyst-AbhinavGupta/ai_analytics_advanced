"""
Clean Refresh Script for Neo4j Knowledge Graph
1. Clears legacy/duplicate nodes from early test runs
2. Sets up strict uniqueness constraints (JourneyStage, Table, Column, SqlIdiom, GoldenQuery)
3. Ingests base physical schemas (eshop_data.es_events_v2 & silver_layer.t_link_journey_checkout_com)
4. Distills and ingests all 87 SQL cards from Metabase Dashboard 1452 with full Deep Reasoning
"""

import os
import json
import time
from neo4j import GraphDatabase
from core.table_ingestion import TableSchemaIngestion
from scripts.distill_and_ingest_dashboard_1452 import run_distillation

NEO4J_URI = os.getenv("NEO4J_URI", "neo4j://127.0.0.1:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")
NEO4J_DB = os.getenv("NEO4J_DATABASE", "neo4j")

def clean_and_rebuild_graph():
    print("================================================================")
    print("🚀 STARTING COMPLETE NEO4J KNOWLEDGE GRAPH CLEAN REFRESH")
    print("================================================================")
    
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    
    # 1. Clear Graph
    print("\n🧹 Step 1: Wiping legacy & duplicate nodes...")
    with driver.session(database=NEO4J_DB) as s:
        s.run("MATCH (n) DETACH DELETE n")
        print("   ✅ Graph wiped successfully.")
        
        # 2. Setup Constraints
        print("\n🔒 Step 2: Creating strict uniqueness constraints & indexes...")
        constraints = [
            "CREATE CONSTRAINT IF NOT EXISTS FOR (t:Table) REQUIRE t.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (c:Column) REQUIRE c.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (s:JourneyStage) REQUIRE s.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (i:SqlIdiom) REQUIRE i.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (b:BusinessIntent) REQUIRE b.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (g:VerifiedGoldenQuery) REQUIRE g.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (r:LearnedRule) REQUIRE r.description IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (m:Metric) REQUIRE m.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (sl:ServiceLine) REQUIRE sl.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (cat:Category) REQUIRE cat.name IS UNIQUE"
        ]
        for c in constraints:
            try:
                s.run(c)
            except Exception as e:
                print(f"   Note on constraint: {e}")
        print("   ✅ Uniqueness constraints established.")
    driver.close()
    
    # 3. Ingest Physical Base Schemas
    print("\n🗄️ Step 3: Ingesting base physical table schemas...")
    ingestion = TableSchemaIngestion(uri=NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD), database=NEO4J_DB)
    
    # Ingest silver layer
    p1 = "data/schema_samples/silver_layer_t_link_journey_checkout_com_schema.json"
    if os.path.exists(p1):
        with open(p1) as f:
            s1 = json.load(f)
        res1 = ingestion.ingest_schema(s1)
        print(f"   • {res1['table_name']}: {res1['columns_ingested']} columns ingested.")
        
    # Ingest eshop_data.es_events_v2
    p2 = "data/schema_samples/eshop_data_es_events_v2_schema.json"
    if os.path.exists(p2):
        with open(p2) as f:
            s2 = json.load(f)
        res2 = ingestion.ingest_schema(s2)
        print(f"   • {res2['table_name']}: {res2['columns_ingested']} columns ingested.")
        
    # 4. Ingest Metabase Dashboard 1452 Cards
    print("\n🧠 Step 4: Running Deep SQL Reasoning Distillation on Dashboard 1452...")
    dash_file = "extracted_data/metabase_1452_full_dashboard.json"
    if not os.path.exists(dash_file):
        raise FileNotFoundError(f"Dashboard file {dash_file} not found!")
        
    run_distillation(dash_file, max_workers=8)
    
    # 5. Final Summary Statistics
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session(database=NEO4J_DB) as s:
        total_nodes = s.run("MATCH (n) RETURN count(n) as c").single()["c"]
        total_rels = s.run("MATCH ()-[r]->() RETURN count(r) as c").single()["c"]
        tbl_cnt = s.run("MATCH (t:Table) RETURN count(t) as c").single()["c"]
        col_cnt = s.run("MATCH (c:Column) RETURN count(c) as c").single()["c"]
        stg_cnt = s.run("MATCH (s:JourneyStage) RETURN count(s) as c").single()["c"]
        int_cnt = s.run("MATCH (b:BusinessIntent) RETURN count(b) as c").single()["c"]
        idm_cnt = s.run("MATCH (i:SqlIdiom) RETURN count(i) as c").single()["c"]
        rul_cnt = s.run("MATCH (r:LearnedRule) RETURN count(r) as c").single()["c"]
        gld_cnt = s.run("MATCH (g:VerifiedGoldenQuery) RETURN count(g) as c").single()["c"]
        syn_cnt = s.run("MATCH ()-[r:SYNAPSE_REINFORCED]->() RETURN count(r) as c").single()["c"]
        
    driver.close()
    
    print("\n================================================================")
    print("✨ COMPLETE REFRESH SUCCESSFUL! FINAL GRAPH SUMMARY:")
    print("================================================================")
    print(f"  • Total Nodes:              {total_nodes}")
    print(f"  • Total Relationships:      {total_rels}")
    print(f"  • Physical Tables:          {tbl_cnt}")
    print(f"  • Profiled Columns:         {col_cnt}")
    print(f"  • Distinct Journey Stages:  {stg_cnt}")
    print(f"  • Business Intents:         {int_cnt}")
    print(f"  • SQL Idioms:               {idm_cnt}")
    print(f"  • Domain Rules:             {rul_cnt}")
    print(f"  • Golden Queries:           {gld_cnt}")
    print(f"  • Reinforced Synapses:      {syn_cnt}")
    print("================================================================")

if __name__ == "__main__":
    clean_and_rebuild_graph()
