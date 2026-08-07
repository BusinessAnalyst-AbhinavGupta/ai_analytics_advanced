import subprocess
import json
import base64
import time
import os

OUTPUT_PATH = "extracted_data/metabase_1717_full_dashboard.json"

def run_metabase_js(js_code: str) -> str:
    """Executes JS in background inside the open Metabase Chrome tab."""
    b64 = base64.b64encode(js_code.encode("utf-8")).decode("ascii")
    apple_script = f'''
    tell application "Google Chrome"
        repeat with w in windows
            repeat with t in tabs of w
                if URL of t contains "metabase.om.yo-digital.com" then
                    set res to (execute t javascript "eval(atob('{b64}'))")
                    return res
                end if
            end repeat
        end repeat
        return "error: no metabase tab"
    end tell
    '''
    proc = subprocess.run(["osascript", "-e", apple_script], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"AppleScript error: {proc.stderr}")
    return proc.stdout.strip()

def main():
    print("=" * 75)
    print(" 🚀 EXTRACTING DASHBOARD 1717: BASKET CONVERSION DASHBOARD")
    print("=" * 75)

    os.makedirs("extracted_data", exist_ok=True)

    # 1. Fetch dashboard 1717 overview
    trigger_dash_js = """
    window.__dash1717_full = null;
    window.__dash1717_err = null;
    fetch('/api/dashboard/1717')
        .then(r => r.json())
        .then(d => { window.__dash1717_full = d; })
        .catch(e => { window.__dash1717_err = e.toString(); });
    """
    run_metabase_js(trigger_dash_js)
    
    # Poll for response
    dash_info = None
    for _ in range(10):
        time.sleep(1)
        res_str = run_metabase_js("""
        (() => {
            const d = window.__dash1717_full;
            if (!d) return JSON.stringify({status: 'waiting', err: window.__dash1717_err});
            return JSON.stringify({
                id: d.id,
                name: d.name,
                description: d.description,
                tabs: d.tabs,
                parameters: d.parameters,
                raw_dashcards: (d.ordered_cards || d.dashcards || []).map(dc => ({
                    dashcard_id: dc.id,
                    card_id: dc.card_id || (dc.card ? dc.card.id : null),
                    dashboard_tab_id: dc.dashboard_tab_id,
                    card: dc.card,
                    visualization_settings: dc.visualization_settings
                }))
            });
        })()
        """)
        parsed = json.loads(res_str)
        if parsed.get("id"):
            dash_info = parsed
            break

    if not dash_info or "raw_dashcards" not in dash_info:
        print("[ERROR] Could not fetch dashboard 1717:", dash_info)
        return

    tabs_map = {t["id"]: t["name"] for t in dash_info.get("tabs", [])}
    raw_dashcards = dash_info.get("raw_dashcards", [])
    print(f"\nDashboard: '{dash_info.get('name')}' (ID: {dash_info.get('id')})")
    print(f"Total Tabs: {len(tabs_map)} | Total Dashcards: {len(raw_dashcards)}")
    
    # Identify target tabs matching user criteria:
    # 1. Checkout Conversion
    # 2. Purchase Success
    # 3. Tariff Level Deep Dive (or Tarrif Level Deep Dive)
    target_tab_ids = []
    
    for tid, tname in tabs_map.items():
        count = sum(1 for dc in raw_dashcards if dc.get("dashboard_tab_id") == tid)
        is_target = any(tt in tname.lower() for tt in ["checkout conversion", "purchase success", "tariff", "tarrif"])
        if is_target:
            target_tab_ids.append(tid)
            print(f"  🎯 [TARGET] Tab {tid} ('{tname}'): {count} cards")
        else:
            print(f"  ⏭️  [SKIPPED] Tab {tid} ('{tname}'): {count} cards")

    # Filter dashcards to only target tabs
    filtered_dashcards = [dc for dc in raw_dashcards if dc.get("dashboard_tab_id") in target_tab_ids]
    print(f"\nTotal Dashcards in selected tabs: {len(filtered_dashcards)}")

    # Extract unique card IDs
    unique_card_ids = []
    for dc in filtered_dashcards:
        cid = dc.get("card_id")
        if cid and cid not in unique_card_ids:
            unique_card_ids.append(cid)

    print(f"Unique Card IDs to fetch: {len(unique_card_ids)}")

    # Fetch full card details in batches
    card_details = {}
    batch_size = 15
    for i in range(0, len(unique_card_ids), batch_size):
        batch = unique_card_ids[i:i+batch_size]
        print(f"Fetching card batch {i+1} to {min(i+batch_size, len(unique_card_ids))}...")
        
        trigger_batch_js = f"""
        window.__batch_cards_1717 = null;
        Promise.all({json.dumps(batch)}.map(id => 
            fetch('/api/card/' + id).then(r => r.json()).catch(e => ({{id: id, error: e.toString()}}))
        )).then(results => {{
            window.__batch_cards_1717 = results;
        }});
        """
        run_metabase_js(trigger_batch_js)
        
        batch_res = None
        for _ in range(15):
            time.sleep(1)
            b_str = run_metabase_js("""
            (() => {
                const b = window.__batch_cards_1717;
                if (!b) return JSON.stringify({status: 'waiting'});
                return JSON.stringify({status: 'done', cards: b});
            })()
            """)
            b_parsed = json.loads(b_str)
            if b_parsed.get("status") == "done":
                batch_res = b_parsed.get("cards", [])
                break
                
        if batch_res:
            for c in batch_res:
                if isinstance(c, dict) and "id" in c:
                    card_details[c["id"]] = c
        else:
            print(f"[WARN] Batch {i} timed out")

    print(f"Successfully fetched details for {len(card_details)} cards.")

    # Assemble cards with SQL and metadata
    assembled_cards = []
    for dc in filtered_dashcards:
        cid = dc.get("card_id")
        c_full = card_details.get(cid, {}) if cid else {}
        
        dataset_query = c_full.get("dataset_query", {})
        native_dict = dataset_query.get("native", {})
        sql = native_dict.get("query", "")
        template_tags = native_dict.get("template-tags", {})
        
        # Also check MBQL v2 stages if native is in stages
        if not sql and "stages" in dataset_query:
            stages = dataset_query.get("stages", [])
            for stg in stages:
                if stg.get("native"):
                    sql = stg.get("native")
                    break

        tab_id = dc.get("dashboard_tab_id")
        tab_name = tabs_map.get(tab_id, "Unknown Tab")

        assembled_cards.append({
            "dashcard_id": dc.get("dashcard_id"),
            "card_id": cid,
            "tab_id": tab_id,
            "tab_name": tab_name,
            "card_name": c_full.get("name") or (dc.get("card") or {}).get("name", "Untitled Card"),
            "description": c_full.get("description"),
            "display_type": c_full.get("display") or (dc.get("card") or {}).get("display", "table"),
            "database_id": c_full.get("database_id"),
            "has_sql": bool(sql),
            "sql": sql,
            "template_tags": template_tags,
            "visualization_settings": dc.get("visualization_settings", {})
        })

    final_payload = {
        "dashboard_id": dash_info.get("id"),
        "dashboard_name": dash_info.get("name"),
        "description": dash_info.get("description"),
        "tabs": [t for t in dash_info.get("tabs", []) if t["id"] in target_tab_ids],
        "parameters": dash_info.get("parameters", []),
        "total_cards": len(assembled_cards),
        "sql_cards_count": sum(1 for c in assembled_cards if c.get("has_sql")),
        "cards": assembled_cards
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(final_payload, f, indent=2)

    print("\n" + "=" * 75)
    print(f" ✅ DASHBOARD 1717 EXTRACTED SUCCESSFULLY TO: {OUTPUT_PATH}")
    print(f" Total Selected Tabs: {len(final_payload['tabs'])}")
    print(f" Total Dashcards:     {final_payload['total_cards']}")
    print(f" SQL Cards with Code: {final_payload['sql_cards_count']}")
    print("=" * 75)

if __name__ == "__main__":
    main()
