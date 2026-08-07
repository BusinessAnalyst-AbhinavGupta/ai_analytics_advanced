"""
Unified Clean Refresh & Multi-Dashboard Distillation Pipeline:
1. Clears legacy/duplicate nodes from Neo4j
2. Sets up strict uniqueness constraints (JourneyStage, Table, Column, SqlIdiom, GoldenQuery)
3. Ingests base physical schemas (eshop_data.es_events_v2 & silver_layer.t_link_journey_checkout_com)
4. Distills & ingests Metabase Dashboard 1452 ('Section - Wise Dashboard OneShop DE' - 87 cards)
5. Distills & ingests Metabase Dashboard 1072 ('OneShop : One Checkout Dashboard DE/EU' - 61 cards)
"""

import os
import sys
import json
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from neo4j import GraphDatabase
from core.table_ingestion import TableSchemaIngestion
from scripts.distill_and_ingest_dashboard_1452 import run_distillation

NEO4J_URI = os.getenv("NEO4J_URI", "neo4j://127.0.0.1:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")
NEO4J_DB = os.getenv("NEO4J_DATABASE", "neo4j")

DASH_1452_PATH = "extracted_data/metabase_1452_full_dashboard.json"
DASH_1072_PATH = "extracted_data/metabase_1072_full_dashboard.json"

def main():
    t_start = time.time()
    print("=" * 80)
    print(" 🚀 STARTING COMPLETE NEO4J CLEAN REFRESH & MULTI-DASHBOARD DISTILLATION")
    print("=" * 80)

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    # 1. Clear Graph
    print("\n🧹 Step 1: Wiping legacy and duplicate graph nodes...")
    with driver.session(database=NEO4J_DB) as s:
        s.run("MATCH (n) DETACH DELETE n")
        print("   ✅ Graph wiped successfully.")

        # 2. Setup Constraints
        print("\n🔒 Step 2: Establishing strict uniqueness constraints & indexes...")
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

    p1 = "data/schema_samples/silver_layer_t_link_journey_checkout_com_schema.json"
    if os.path.exists(p1):
        with open(p1) as f:
            s1 = json.load(f)
        res1 = ingestion.ingest_schema(s1)
        print(f"   • {res1['table_name']}: {res1['columns_ingested']} columns ingested.")

    p2 = "data/schema_samples/eshop_data_es_events_v2_schema.json"
    if os.path.exists(p2):
        with open(p2) as f:
            s2 = json.load(f)
        res2 = ingestion.ingest_schema(s2)
        print(f"   • {res2['table_name']}: {res2['columns_ingested']} columns ingested.")

    # 4. Ingest Metabase Dashboard 1452
    print("\n" + "=" * 80)
    print("🧠 Step 4: Distilling & Ingesting Dashboard 1452 (Section-Wise OneShop DE)...")
    print("=" * 80)
    run_distillation(DASH_1452_PATH, max_workers=8)

    # 5. Ingest Metabase Dashboard 1072
    print("\n" + "=" * 80)
    print("🧠 Step 5: Distilling & Ingesting Dashboard 1072 (OneShop : One Checkout DE/EU)...")
    print("=" * 80)
    run_distillation(DASH_1072_PATH, max_workers=8)

    # 6. Ingest Metabase Dashboard 1717 (Checkout Conversion, Purchase Success, Tariff Level Deep Dive)
    dash_1717_path = "extracted_data/metabase_1717_full_dashboard.json"
    if os.path.exists(dash_1717_path):
        print("\n" + "=" * 80)
        print("🧠 Step 6: Distilling & Ingesting Dashboard 1717 (Basket Conversion Dashboard)...")
        print("=" * 80)
        run_distillation(dash_1717_path, max_workers=8)

    # 6. Final Summary Statistics
    total_time = time.time() - t_start
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

    print("\n" + "=" * 80)
    print("✨ ALL-IN-ONE MULTI-DASHBOARD INGESTION COMPLETE!")
    print(f"⏱️ Total Pipeline Execution Time: {total_time:.2f}s")
    print("=" * 80)
    print(f"  • Total Graph Nodes:        {total_nodes}")
    print(f"  • Total Relationships:      {total_rels}")
    print(f"  • Physical Tables:          {tbl_cnt}")
    print(f"  • Profiled Columns:         {col_cnt}")
    print(f"  • Distinct Journey Stages:  {stg_cnt}")
    print(f"  • Business Intents:         {int_cnt}")
    print(f"  • SQL Idioms:               {idm_cnt}")
    print(f"  • Learned Domain Rules:     {rul_cnt}")
    print(f"  • Golden Query Templates:   {gld_cnt}")
    print(f"  • Reinforced Synapses:      {syn_cnt}")
    print("=" * 80)

if __name__ == "__main__":
    main()
