import os
import inspect
from core.llm_gateway import LLMGateway
from core.query_generator import QueryGenerator
from core.auto_healer import AutoHealer
from core.parser import parse_analytics_logic

def test_defaults():
    print("=== Testing LLMGateway Providers & Models ===")
    assert LLMGateway.PROVIDERS[0] == "OpenRouter API", f"Expected OpenRouter API first, got {LLMGateway.PROVIDERS[0]}"
    assert "Google Gemini API" in LLMGateway.PROVIDERS, "Expected Google Gemini API to be in list"
    assert LLMGateway.PROVIDERS[-1] == "Google Gemini API" or LLMGateway.PROVIDERS[0] != "Google Gemini API"
    
    models = LLMGateway.get_available_models("OpenRouter API")
    print(f"OpenRouter models: {models[:5]}")
    assert models[0] == "deepseek/deepseek-v4-flash-0731", f"Expected deepseek/deepseek-v4-flash-0731 first, got {models[0]}"

    print("\n=== Testing Function Signatures ===")
    sig_gen = inspect.signature(LLMGateway.generate)
    assert sig_gen.parameters["provider"].default == "OpenRouter API"
    assert sig_gen.parameters["model"].default == "deepseek/deepseek-v4-flash-0731"
    print("✓ LLMGateway.generate defaults to OpenRouter API and deepseek/deepseek-v4-flash-0731")

    sig_qg = inspect.signature(QueryGenerator.generate_sql)
    assert sig_qg.parameters["provider"].default == "OpenRouter API"
    assert sig_qg.parameters["model_name"].default == "deepseek/deepseek-v4-flash-0731"
    print("✓ QueryGenerator.generate_sql defaults to OpenRouter API and deepseek/deepseek-v4-flash-0731")

    sig_ah = inspect.signature(AutoHealer.diagnose_and_heal)
    assert sig_ah.parameters["provider"].default == "OpenRouter API"
    assert sig_ah.parameters["model_name"].default == "deepseek/deepseek-v4-flash-0731"
    print("✓ AutoHealer.diagnose_and_heal defaults to OpenRouter API and deepseek/deepseek-v4-flash-0731")

    print("\n✅ All provider and default assertions passed successfully!")

if __name__ == "__main__":
    test_defaults()
