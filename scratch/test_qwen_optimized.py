import time
import requests
from core.query_generator import QueryGenerator
from core.llm_gateway import LLMGateway

def test_qwen_optimized():
    qg = QueryGenerator()
    question = """In the checkout journey of the users who dropped between checkout initiated and personal info, how many of them did a successful login?
data of last 2 weeks only"""
    
    # Retrieve graph context
    ctx = qg.retrieve_graph_context(question)
    q_words = [w.lower() for w in question.split() if len(w) > 2]
    
    # Test with top 60 prioritized columns per table
    tables_text_list = []
    for t in ctx["tables"]:
        tname = t["name"]
        matching_cols = [c for c in ctx["columns"] if c.get("table_name") == tname]
        
        def col_score(c):
            cname = c["name"].lower()
            matches = sum(1 for kw in q_words if kw in cname)
            return (matches, c.get("weight", 0.5), c.get("successes", 0))
            
        sorted_cols = sorted(matching_cols, key=col_score, reverse=True)
        # Limit to top 75 for local ollama
        selected_cols = sorted_cols[:75] if len(sorted_cols) > 75 else sorted_cols
        
        col_lines = []
        for c in selected_cols:
            s_val = f" | Samples: {c.get('sample_values')[:3]}" if c.get('sample_values') else ""
            col_lines.append(f"    - `{c['name']}` ({c.get('dtype', 'VARCHAR')}){s_val}")
        tables_text_list.append(f"Table: `{tname}`\n  Columns ({len(selected_cols)} of {len(matching_cols)} shown):\n" + "\n".join(col_lines))
        
    schema_str = "\n\n".join(tables_text_list)
    print(f"Optimized schema chars: {len(schema_str)} (~{len(schema_str)//4} tokens)", flush=True)
    
    prompt = f"""You are a Principal Data Analyst and SQL Architect specializing in AWS Athena / Presto.
Use the verified physical schema below to write production SQL:

{schema_str}

Question: {question}
Return production-ready SQL in ```sql codeblock."""

    print("Calling Ollama Qwen 14B with optimized schema...", flush=True)
    t0 = time.time()
    res = LLMGateway.generate(
        prompt=prompt,
        provider="Local Ollama",
        model="qwen2.5-coder:14b",
        temperature=0.0
    )
    t1 = time.time()
    print(f"\n⚡ Qwen 14B Generation completed in {t1 - t0:.2f}s!", flush=True)
    print("\n--- GENERATED SQL ---")
    print(res.get("text"))

if __name__ == "__main__":
    test_qwen_optimized()
