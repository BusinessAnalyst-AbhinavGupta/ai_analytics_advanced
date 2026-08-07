import time
import os
import sys
from core.query_generator import QueryGenerator

def test_full_gen():
    print("1. Initializing QueryGenerator...", flush=True)
    qg = QueryGenerator()
    question = """In the checkout journey of the users who dropped between checkout initiated and personal info, how many of them did a successful login?
data of last 2 weeks only"""
    
    print("2. Retrieving graph context...", flush=True)
    ctx = qg.retrieve_graph_context(question)
    print(f"   Retrieved {len(ctx.get('tables', []))} tables, {len(ctx.get('columns', []))} columns, {len(ctx.get('golden_queries', []))} golden queries", flush=True)
    
    print("3. Invoking QueryGenerator.generate_sql with Gemini 3.5 Flash...", flush=True)
    t0 = time.time()
    res = qg.generate_sql(
        question=question,
        provider="Google Gemini API",
        model_name="gemini-3.5-flash",
        api_key=os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6KfuVn3yd1-VA08eNi5KDtVQ2LbaKWr6jZvrvWD2q1rQQ"),
        table_filter="Auto-Detect All Tables",
        database_dialect="AWS Athena / Presto"
    )
    t1 = time.time()
    
    print(f"\n⏱️ Generation completed in {t1 - t0:.2f}s", flush=True)
    print(f"📋 Verification Status: {res.get('verification_status')}", flush=True)
    print(f"🔢 Iterations: {res.get('verification_iterations')}", flush=True)
    print(f"🛡️ Validation Valid?: {res.get('validation', {}).get('is_valid')}", flush=True)
    print(f"⚠️ Validation Errors: {res.get('validation', {}).get('errors')}", flush=True)
    print("\n--- GENERATED SQL ---", flush=True)
    print(res.get("sql"), flush=True)
    print("\n--- EXPLANATION ---", flush=True)
    print(res.get("explanation"), flush=True)

if __name__ == "__main__":
    test_full_gen()
