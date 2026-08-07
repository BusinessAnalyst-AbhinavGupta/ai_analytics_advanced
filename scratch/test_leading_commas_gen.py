import os
from core.query_generator import QueryGenerator

q_gen = QueryGenerator()
question = "In the checkout journey of the users who dropped between checkout initiated and personal info, how many of them did a successful login?\ndata of last 2 weeks only"

print("Running QueryGenerator with OpenRouter DeepSeek V4 Flash...")
res = q_gen.generate_sql(
    question=question,
    provider="OpenRouter API",
    model_name="deepseek/deepseek-v4-flash-0731",
    api_key=os.getenv("OPENROUTER_API_KEY", ""),
    database_dialect="Athena (Presto SQL)",
    custom_instructions="data of last 2 weeks only"
)

print("\n--- GENERATED SQL ---")
print(res["sql"])
print("\n--- EXPLANATION ---")
print(res["explanation"])
print("\nVerification status:", res.get("verification_status"))
print("Validation errors:", res.get("validation_errors"))
print("Validation warnings:", res.get("validation_warnings"))
