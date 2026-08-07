import time
import os
from core.query_generator import QueryGenerator

def test_full_gen():
    qg = QueryGenerator()
    question = """In the checkout journey of the users who dropped between checkout initiated and personal info, how many of them did a successful login?
data of last 2 weeks only"""
    
    print("🚀 Invoking QueryGenerator.generate_sql with Gemini 3.5 Flash...")
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
    
    print(f"\n⏱️ Generation completed in {t1 - t0:.2f}s")
    print(f"📋 Verification Status: {res.get('verification_status')}")
    print(f"🔢 Iterations: {res.get('verification_iterations')}")
    print(f"🛡️ Validation Valid?: {res.get('validation', {}).get('is_valid')}")
    print(f"⚠️ Validation Errors: {res.get('validation', {}).get('errors')}")
    print(f"💡 Validation Suggestions: {res.get('validation', {}).get('suggestions')}")
    print("\n--- GENERATED SQL ---")
    print(res.get("sql"))
    print("\n--- EXPLANATION ---")
    print(res.get("explanation"))

if __name__ == "__main__":
    test_full_gen()
