from core.query_generator import QueryGenerator

def check_tokens():
    qg = QueryGenerator()
    question = """In the checkout journey of the users who dropped between checkout initiated and personal info, how many of them did a successful login?
data of last 2 weeks only"""
    
    ctx = qg.retrieve_graph_context(question)
    
    tables_text_list = []
    for t in ctx["tables"]:
        tname = t["name"]
        matching_cols = [c for c in ctx["columns"] if c.get("table_name") == tname]
        col_strs = []
        for c in matching_cols:
            s_val = f" | Samples: {c['sample_values']}" if c.get('sample_values') else ""
            col_strs.append(f"    - `{c['name']}` ({c.get('dtype', 'VARCHAR')}){s_val}")
        tables_text_list.append(f"  • Table: `{tname}` (Database: {t.get('database')})\n    Columns ({len(matching_cols)} total):\n" + "\n".join(col_strs))
    
    schema_str = "\n\n".join(tables_text_list)
    
    total_char_len = len(schema_str)
    approx_tokens = total_char_len // 4
    
    print(f"Total schema characters: {total_char_len}")
    print(f"Approximate tokens: {approx_tokens}")

if __name__ == "__main__":
    check_tokens()
