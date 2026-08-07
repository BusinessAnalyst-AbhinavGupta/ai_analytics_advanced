import os
import json
import uuid
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from core.parser import analyze_sql_deep_reasoning
from core.neo4j_adapter import Neo4jAdapter
from core.db import create_run, update_run
from schema.models import DeepSqlReasoning
from neo4j import GraphDatabase

NEO4J_URI = os.getenv("NEO4J_URI", "neo4j://127.0.0.1:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")
NEO4J_DB = os.getenv("NEO4J_DATABASE", "neo4j")

def process_single_card(card: dict, dashboard_meta: dict, adapter: Neo4jAdapter) -> dict:
    card_id = card.get("card_id")
    card_name = card.get("card_name") or card.get("name", "Untitled")
    tab_name = card.get("tab_name", "General")
    display_type = card.get("display_type") or card.get("display", "table")
    sql = card.get("sql", "").strip()
    dash_id = dashboard_meta.get("dashboard_id", "Unknown")
    dash_name = dashboard_meta.get("dashboard_name", "OneShop Dashboard")

    metadata = {
        "dashboard_id": dash_id,
        "dashboard_name": dash_name,
        "tab_name": tab_name,
        "journey_stage": tab_name,
        "card_id": card_id,
        "card_name": card_name,
        "description": card.get("description", ""),
        "display": display_type,
        "visualization_settings": card.get("visualization_settings", {}),
        "template_tags": card.get("template_tags", {}),
        "llm_provider": "OpenRouter API",
        "llm_model": "deepseek/deepseek-v4-flash-0731",
        "llm_api_key": os.getenv("OPENROUTER_API_KEY", "")
    }

    run_id = str(uuid.uuid4())
    create_run(
        run_id=run_id,
        sql_query=sql,
        journey_stage_or_page=tab_name,
        service_line="Fixed & Mobile",
        category="Acquisition Journey",
        natco="DE",
        tags=f"{tab_name}, Metabase-{dash_id}, Card-{card_id}",
        status="RUNNING"
    )

    t0 = time.time()
    try:
        # 1. Deep SQL Reasoning via Gemini 3.1 Flash Lite
        reasoning_dict = analyze_sql_deep_reasoning(sql, metadata=metadata)
        deep_model = DeepSqlReasoning.model_validate(reasoning_dict)

        # 2. Ingest into Neo4j
        adapter.ingest_deep_sql_reasoning(
            reasoning=deep_model,
            uri=NEO4J_URI,
            auth=(NEO4J_USER, NEO4J_PASSWORD),
            database=NEO4J_DB,
            metadata=metadata
        )

        elapsed = time.time() - t0
        update_run(
            run_id=run_id,
            status="SUCCESS",
            canonical_id=str(card_id)
        )
        return {
            "card_id": card_id,
            "name": card_name,
            "tab": tab_name,
            "intent": deep_model.intent_name,
            "cols_count": len(deep_model.column_usages),
            "idioms_count": len(deep_model.sql_idioms),
            "rules_count": len(deep_model.learned_rules),
            "status": "SUCCESS",
            "elapsed": round(elapsed, 2)
        }
    except Exception as e:
        elapsed = time.time() - t0
        update_run(
            run_id=run_id,
            status="FAILED",
            error_message=str(e)
        )
        return {
            "card_id": card_id,
            "name": card_name,
            "tab": tab_name,
            "status": "FAILED",
            "error": str(e),
            "elapsed": round(elapsed, 2)
        }

def run_distillation(json_path: str = "extracted_data/metabase_1452_full_dashboard.json", max_workers: int = 10):
    print("=" * 80, flush=True)
    print(" DISTILLING & INGESTING METABASE DASHBOARD 1452 DEEP SQL INTELLIGENCE", flush=True)
    print("=" * 80, flush=True)

    if not os.path.exists(json_path):
        print(f"[ERROR] Extracted dashboard file not found at: {json_path}", flush=True)
        return

    with open(json_path, "r") as f:
        dash_data = json.load(f)

    cards = [c for c in dash_data.get("cards", []) if c.get("has_sql") and c.get("sql")]
    print(f"Dashboard: {dash_data.get('dashboard_name')} (ID: {dash_data.get('dashboard_id')})", flush=True)
    print(f"Total SQL Cards to Distill: {len(cards)}", flush=True)
    print(f"Concurrency: {max_workers} parallel workers with Gemini 3.1 Flash Lite", flush=True)
    print("-" * 80, flush=True)

    adapter = Neo4jAdapter()
    results = []
    success_count = 0
    fail_count = 0

    t_start = time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_card = {
            executor.submit(process_single_card, card, dash_data, adapter): card
            for card in cards
        }

        for idx, future in enumerate(as_completed(future_to_card), 1):
            res = future.result()
            results.append(res)
            if res["status"] == "SUCCESS":
                success_count += 1
                print(f"[{idx}/{len(cards)}] [SUCCESS {res['elapsed']}s] Card #{res['card_id']} ({res['tab']}): \"{res['intent']}\" | {res['cols_count']} cols, {res['idioms_count']} idioms, {res['rules_count']} rules", flush=True)
            else:
                fail_count += 1
                print(f"[{idx}/{len(cards)}] [FAILED {res['elapsed']}s] Card #{res['card_id']} ({res['tab']}): \"{res['name']}\" -> {res.get('error')}", flush=True)

    total_time = time.time() - t_start
    print("\n" + "=" * 80, flush=True)
    print(" DISTILLATION & INGESTION COMPLETE", flush=True)
    print(f" Total Processed: {len(cards)} | Success: {success_count} | Failed: {fail_count} | Time: {total_time:.2f}s", flush=True)
    print("=" * 80, flush=True)

    # Neo4j Graph Verification
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session(database=NEO4J_DB) as session:
        intents = session.run("MATCH (i:BusinessIntent) RETURN count(i) AS cnt").single()["cnt"]
        stages = session.run("MATCH (s:JourneyStage) RETURN count(s) AS cnt").single()["cnt"]
        columns = session.run("MATCH (c:Column) RETURN count(c) AS cnt").single()["cnt"]
        synapses = session.run("MATCH ()-[r:REQUIRES_COLUMN]->() RETURN count(r) AS cnt").single()["cnt"]
        reinforced = session.run("MATCH ()-[r:SYNAPSE_REINFORCED]->() RETURN count(r) AS cnt").single()["cnt"]
        idioms = session.run("MATCH (i:SqlIdiom) RETURN count(i) AS cnt").single()["cnt"]
        rules = session.run("MATCH (r:LearnedRule) RETURN count(r) AS cnt").single()["cnt"]
        golden = session.run("MATCH (g:VerifiedGoldenQuery) RETURN count(g) AS cnt").single()["cnt"]
        total_rels = session.run("MATCH ()-[r]->() RETURN count(r) AS cnt").single()["cnt"]

        print("\n[NEO4J KNOWLEDGE GRAPH BRAIN TOTALS]")
        print(f"  • :BusinessIntent Nodes:       {intents}")
        print(f"  • :JourneyStage Nodes:         {stages}")
        print(f"  • :Column Nodes:               {columns}")
        print(f"  • :SqlIdiom Nodes:             {idioms}")
        print(f"  • :LearnedRule Nodes:          {rules}")
        print(f"  • :VerifiedGoldenQuery Nodes:  {golden}")
        print(f"  • [:REQUIRES_COLUMN] Synapses: {synapses}")
        print(f"  • [:SYNAPSE_REINFORCED] Links: {reinforced}")
        print(f"  • Total Relationships in DB:   {total_rels}")

    driver.close()

if __name__ == "__main__":
    run_distillation()
