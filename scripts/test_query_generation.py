import os
import json
import requests
import time
from neo4j import GraphDatabase

OLLAMA_API_URL = "http://127.0.0.1:11434/api/chat"
MODEL_NAME = "gemma4:12b"

PROBLEM_STATEMENT = (
    "In the checkout journey of the users who dropped between checkout initiated "
    "and personal info, how many of them did a successful login."
)

def query_ollama_chat(messages: list, model: str = MODEL_NAME) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 2000
        }
    }
    response = requests.post(OLLAMA_API_URL, json=payload, timeout=180)
    response.raise_for_status()
    data = response.json()
    msg = data.get("message", {})
    return {
        "content": msg.get("content", ""),
        "thinking": msg.get("thinking", ""),
        "eval_count": data.get("eval_count", 0),
        "eval_duration": data.get("eval_duration", 0)
    }

def fetch_graph_context():
    driver = GraphDatabase.driver("neo4j://127.0.0.1:7687", auth=("neo4j", "password"))
    context = {}
    with driver.session(database="neo4j") as session:
        # 1. Fetch table columns and sample values
        cols = session.run("""
        MATCH (t:Table {name: 'silver_layer.t_link_journey_checkout_com'})-[:HAS_COLUMN]->(c:Column)
        RETURN c.name as name, c.dtype as dtype, c.sample_values as sample_values
        ORDER BY c.name
        """).data()
        
        # 2. Fetch metrics and associated SQL context
        metrics = session.run("""
        MATCH (m:Metric)
        OPTIONAL MATCH (m)-[:USES_COLUMN]->(col:Column)
        RETURN m.name as metric_name, m.stage as stage, collect(col.name) as columns_used
        LIMIT 15
        """).data()
        
        context["table"] = "silver_layer.t_link_journey_checkout_com"
        context["columns"] = cols
        context["metrics"] = metrics
    driver.close()
    return context

def run_evaluation():
    os.makedirs("benchmark_results", exist_ok=True)
    print("=======================================================")
    print(" EVALUATION BENCHMARK: OBJECTIVE 1 & OBJECTIVE 3")
    print(f" Model: {MODEL_NAME}")
    print(f" Question: {PROBLEM_STATEMENT}")
    print("=======================================================\n")
    
    # -------------------------------------------------------------
    # TEST 1: ZERO-SHOT GEMMA (Objective 1 Test)
    # -------------------------------------------------------------
    print(">>> Running Test 1: Zero-Shot Gemma (No Knowledge Graph Context)...")
    prompt_zero_shot = f"""
You are an expert Data Analyst and SQL Engineer.
Please write a production-ready Athena SQL query to answer the following business problem:

Business Question:
"{PROBLEM_STATEMENT}"

Return ONLY valid SQL in a ```sql code block, followed by a brief explanation of your logic and assumptions.
"""
    t0 = time.time()
    res_zero_shot = query_ollama_chat([{"role": "user", "content": prompt_zero_shot}])
    t1 = time.time()
    print(f"Zero-shot response received in {t1 - t0:.2f}s.\n")
    
    # -------------------------------------------------------------
    # TEST 2: GRAPH-AUGMENTED GEMMA (Objective 3 Test)
    # -------------------------------------------------------------
    print(">>> Retrieving Knowledge Graph Context from Neo4j...")
    graph_ctx = fetch_graph_context()
    
    # Format context for prompt
    cols_summary = []
    for c in graph_ctx["columns"]:
        try:
            samples = json.loads(c["sample_values"]) if isinstance(c["sample_values"], str) else c["sample_values"]
        except Exception:
            samples = []
        samples_preview = f" (Sample values: {samples[:5]})" if samples else ""
        cols_summary.append(f"  - `{c['name']}` ({c['dtype']}){samples_preview}")
    cols_text = "\n".join(cols_summary[:28])  # Top relevant columns
    
    prompt_graph_augmented = f"""
You are an expert Data Analyst and SQL Engineer with direct access to our Neo4j Knowledge Graph.

Database: Athena (`de_central_analytics_read`)
Physical Base Table: `{graph_ctx['table']}`

Key Table Columns and Value Samples from Knowledge Graph:
{cols_text}

Relevant Domain & Funnel Business Rules from Graph:
- Checkout Initiation / Account Step: `page_name LIKE '%checkout/account%'` or `page_name = 'basket'` with `action = 'PageView'`
- Personal Info Step: `page_name LIKE '%checkout/personalinfo%'`
- User Login Status: `user_login_type = 'loggedin'` (vs `'guest'`) OR `action = 'loginSuccess'`
- Session Identifier: `session_id` (or `guest_id`)
- Drop-off Definition: Sessions/Users that triggered the initiation step (`checkout/account`) but did NOT reach the Personal Info step (`checkout/personalinfo`).
- Successful Login among dropped users: Users in that dropped cohort whose session recorded a successful login (`user_login_type = 'loggedin'` or `action = 'loginSuccess'`).

Business Question:
"{PROBLEM_STATEMENT}"

Write a precise, production-ready Athena SQL query using the exact table `{graph_ctx['table']}` and exact column names & values documented above to calculate the count of dropped users who successfully logged in.
Return ONLY valid SQL in a ```sql code block, followed by an explanation of the CTEs and logic.
"""
    print(">>> Running Test 2: Graph-Augmented Gemma (With Neo4j Knowledge Graph Context)...")
    t2 = time.time()
    res_graph_augmented = query_ollama_chat([{"role": "user", "content": prompt_graph_augmented}])
    t3 = time.time()
    print(f"Graph-augmented response received in {t3 - t2:.2f}s.\n")
    
    # -------------------------------------------------------------
    # SAVE BENCHMARK RESULTS
    # -------------------------------------------------------------
    results = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": MODEL_NAME,
        "business_question": PROBLEM_STATEMENT,
        "test_1_zero_shot": {
            "prompt": prompt_zero_shot,
            "response": res_zero_shot["content"],
            "thinking": res_zero_shot["thinking"],
            "latency_seconds": round(t1 - t0, 2)
        },
        "test_2_graph_augmented": {
            "prompt": prompt_graph_augmented,
            "response": res_graph_augmented["content"],
            "thinking": res_graph_augmented["thinking"],
            "latency_seconds": round(t3 - t2, 2)
        }
    }
    
    results_json_path = "benchmark_results/objective_1_and_3_gemma_test.json"
    with open(results_json_path, "w") as f:
        json.dump(results, f, indent=2)
        
    print(f"Saved Benchmark Results to: {results_json_path}")
    return results

if __name__ == "__main__":
    run_evaluation()
