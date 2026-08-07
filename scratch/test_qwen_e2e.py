import time
from core.query_generator import QueryGenerator

def test_qwen_gen():
    qg = QueryGenerator()
    question = """In the checkout journey of the users who dropped between checkout initiated and personal info, how many of them did a successful login?
data of last 2 weeks only"""
    
    print("🚀 Invoking QueryGenerator.generate_sql with Local Ollama qwen2.5-coder:14b...", flush=True)
    t0 = time.time()
    res = qg.generate_sql(
        question=question,
        provider="Local Ollama",
        model_name="qwen2.5-coder:14b",
        table_filter="Auto-Detect All Tables",
        database_dialect="AWS Athena / Presto"
    )
    t1 = time.time()
    
    print(f"\n⏱️ Generation completed in {t1 - t0:.2f}s", flush=True)
    print(f"📋 Verification Status: {res.get('verification_status')}", flush=True)
    print(f"🔢 Iterations: {res.get('verification_iterations')}", flush=True)
    print(f"🛡️ Validation Valid?: {res.get('validation', {}).get('is_valid')}", flush=True)
    print(f"⚠️ Validation Errors: {res.get('validation', {}).get('errors')}", flush=True)
    print(f"💡 Validation Suggestions: {res.get('validation', {}).get('suggestions')}", flush=True)
    print("\n--- GENERATED SQL ---", flush=True)
    print(res.get("sql"), flush=True)
    print("\n--- EXPLANATION ---", flush=True)
    print(res.get("explanation"), flush=True)

if __name__ == "__main__":
    test_qwen_gen()
