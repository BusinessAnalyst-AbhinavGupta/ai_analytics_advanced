import os
import json
import uuid
import time
from datetime import datetime, timezone

from core.pipeline import IngestionPipeline
from core.db import create_run, update_run
from neo4j import GraphDatabase

NEO4J_URI = os.getenv("NEO4J_URI", "neo4j://127.0.0.1:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")
NEO4J_DB = os.getenv("NEO4J_DATABASE", "neo4j")

def infer_category(name: str, display_type: str) -> str:
    name_lower = name.lower()
    if "time spent" in name_lower or "duration" in name_lower:
        return "Performance & Duration"
    elif "error" in name_lower or "fail" in name_lower:
        return "Errors & Quality"
    elif "funnel" in name_lower or display_type == "funnel":
        return "Funnel & Dropoff"
    elif "conversion" in name_lower or "cr" in name_lower:
        return "Conversion & KPIs"
    elif "basket" in name_lower or "oci" in name_lower:
        return "Traffic & Funnel"
    return "Checkout Analytics"

def run_batch_ingestion(json_path: str = "extracted_data/metabase_tab_1236_cards_sql.json"):
    print("=" * 70)
    print(" METABASE TAB 1236 BATCH INGESTION TO NEO4J KNOWLEDGE GRAPH")
    print("=" * 70)
    
    if not os.path.exists(json_path):
        print(f"[ERROR] Extracted questions file not found at: {json_path}")
        return

    with open(json_path, "r") as f:
        data = json.load(f)

    cards = data.get("extracted_cards", [])
    sql_cards = [c for c in cards if c.get("has_sql") and c.get("sql")]
    
    print(f"Loaded {len(sql_cards)} questions with raw SQL to ingest.")
    print(f"Target Neo4j: {NEO4J_URI} (DB: {NEO4J_DB})")
    print("-" * 70)

    pipeline = IngestionPipeline()
    results_summary = []
    success_count = 0
    fail_count = 0

    for idx, card in enumerate(sql_cards, 1):
        card_id = card.get("card_id")
        card_name = card.get("name", "Untitled")
        display_type = card.get("display_type", "unknown")
        sql = card.get("sql", "").strip()
        
        category = infer_category(card_name, display_type)
        service_line = "Fixed & Mobile"
        natco = "DE"
        journey_stage = "Checkout"
        tags = f"{display_type.capitalize()}, Metabase, Card-{card_id}"
        owner = "OneMind Analytics"
        description = f"{card_name} ({display_type}) - Metabase Question #{card_id}"

        metadata = {
            "journey_stage": journey_stage,
            "journey_stage_or_page": journey_stage,
            "service_line": service_line,
            "category": category,
            "natco": natco,
            "tags": tags,
            "owner": owner,
            "description": description,
            "metabase_card_id": card_id,
            "display_type": display_type
        }

        run_id = str(uuid.uuid4())
        print(f"\n[{idx}/{len(sql_cards)}] Ingesting Card #{card_id}: \"{card_name}\" ({display_type})")
        
        # Record in SQLite runs DB
        create_run(
            run_id=run_id,
            sql_query=sql,
            journey_stage_or_page=journey_stage,
            service_line=service_line,
            category=category,
            natco=natco,
            tags=tags,
            status="RUNNING"
        )

        start_time = time.time()
        try:
            res = pipeline.ingest(
                raw_sql=sql,
                uri=NEO4J_URI,
                auth=(NEO4J_USER, NEO4J_PASSWORD),
                database=NEO4J_DB,
                metadata=metadata,
                run_id=run_id
            )
            elapsed = time.time() - start_time
            print(f"  --> [SUCCESS] Ingested in {elapsed:.2f}s | Canonical ID: {res['canonical_id']}")
            
            update_run(
                run_id=run_id,
                status="SUCCESS",
                checkpoint_path=res["checkpoint_path"],
                canonical_id=res["canonical_id"]
            )
            success_count += 1
            results_summary.append({
                "card_id": card_id,
                "name": card_name,
                "status": "SUCCESS",
                "elapsed": round(elapsed, 2),
                "error": None
            })
        except Exception as e:
            elapsed = time.time() - start_time
            print(f"  --> [FAILED] {e} (after {elapsed:.2f}s)")
            update_run(
                run_id=run_id,
                status="FAILED",
                error_message=str(e)
            )
            fail_count += 1
            results_summary.append({
                "card_id": card_id,
                "name": card_name,
                "status": "FAILED",
                "elapsed": round(elapsed, 2),
                "error": str(e)
            })

    print("\n" + "=" * 70)
    print(f" BATCH INGESTION COMPLETE")
    print(f" Total Processed: {len(sql_cards)} | Successful: {success_count} | Failed: {fail_count}")
    print("=" * 70)

    # Inspect Neo4j Graph counts
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session(database=NEO4J_DB) as session:
        metric_cnt = session.run("MATCH (m:Metric) RETURN count(m) AS cnt").single()["cnt"]
        rel_cnt = session.run("MATCH ()-[r]->() RETURN count(r) AS cnt").single()["cnt"]
        stage_cnt = session.run("MATCH (s:JourneyStage) RETURN count(s) AS cnt").single()["cnt"]
        cat_cnt = session.run("MATCH (c:Category) RETURN count(c) AS cnt").single()["cnt"]
        tag_cnt = session.run("MATCH (t:Tag) RETURN count(t) AS cnt").single()["cnt"]
        print(f"\n[NEO4J KNOWLEDGE GRAPH CURRENT TOTALS]")
        print(f"  • Total :Metric Nodes: {metric_cnt}")
        print(f"  • Total :JourneyStage Nodes: {stage_cnt}")
        print(f"  • Total :Category Nodes: {cat_cnt}")
        print(f"  • Total :Tag Nodes: {tag_cnt}")
        print(f"  • Total Relationships Connected: {rel_cnt}")
    driver.close()

if __name__ == "__main__":
    run_batch_ingestion()
