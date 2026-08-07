"""
Extracts all tabs and cards from Metabase Dashboard 1072:
'OneShop : One Checkout Dashboard (DE/EU)'
Tabs:
- 1236: ONE CHECKOUT
- 1317: Errors Analysis
- 1581: Overall Natco
- 1902: B2B - Fixed Acquistion
"""

import subprocess
import json
import base64
import time
import os

OUTPUT_PATH = "extracted_data/metabase_1072_full_dashboard.json"

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
    print(" 🚀 EXTRACTING ALL TABS & CARDS FROM METABASE DASHBOARD 1072")
    print("=" * 75)

    os.makedirs("extracted_data", exist_ok=True)

    # 1. Fetch dashboard overview
    trigger_dash_js = """
    window.__dash1072_full = null;
    window.__dash1072_err = null;
    fetch('/api/dashboard/1072')
        .then(r => r.json())
        .then(d => { window.__dash1072_full = d; })
        .catch(e => { window.__dash1072_err = e.toString(); });
    """
    run_metabase_js(trigger_dash_js)
    
    # Poll for response
    dash_info = None
    for _ in range(10):
        time.sleep(1)
        res_str = run_metabase_js("""
        (() => {
            const d = window.__dash1072_full;
            if (!d) return JSON.stringify({status: 'waiting', err: window.__dash1072_err});
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
        print("[ERROR] Could not fetch dashboard 1072:", dash_info)
        return

    tabs_map = {t["id"]: t["name"] for t in dash_info.get("tabs", [])}
    raw_dashcards = dash_info.get("raw_dashcards", [])
    print(f"\nDashboard: '{dash_info.get('name')}' (ID: {dash_info.get('id')})")
    print(f"Total Tabs: {len(tabs_map)} | Total Dashcards: {len(raw_dashcards)}")
    for tid, tname in tabs_map.items():
        count = sum(1 for dc in raw_dashcards if dc.get("dashboard_tab_id") == tid)
        print(f"  • Tab {tid} ('{tname}'): {count} cards")

    # 2. Extract unique card IDs that have real questions
    unique_card_ids = []
    card_tab_map = {}
    for dc in raw_dashcards:
        cid = dc.get("card_id")
        if cid and cid not in unique_card_ids:
            unique_card_ids.append(cid)
            card_tab_map[cid] = {
                "tab_id": dc.get("dashboard_tab_id"),
                "tab_name": tabs_map.get(dc.get("dashboard_tab_id"), "General"),
                "dashcard_viz": dc.get("visualization_settings", {})
            }

    print(f"\nUnique Question Card IDs to fetch: {len(unique_card_ids)}")

    # 3. Fetch full card details in batches of 10
    batch_size = 10
    all_extracted_cards = []

    for i in range(0, len(unique_card_ids), batch_size):
        batch = unique_card_ids[i:i + batch_size]
        print(f"Fetching cards batch {i + 1} to {min(i + batch_size, len(unique_card_ids))} of {len(unique_card_ids)}...")
        
        batch_ids_json = json.dumps(batch)
        trigger_batch_js = f"""
        window.__batch1072_cards_res = null;
        window.__batch1072_cards_err = null;
        Promise.all({batch_ids_json}.map(cid => 
            fetch('/api/card/' + cid)
                .then(r => r.json())
                .then(cardData => ({{ cid: cid, data: cardData }}))
                .catch(err => ({{ cid: cid, error: err.toString() }}))
        )).then(results => {{
            window.__batch1072_cards_res = results;
        }}).catch(e => {{
            window.__batch1072_cards_err = e.toString();
        }});
        """
        run_metabase_js(trigger_batch_js)
        
        # Poll for batch completion
        batch_res = None
        for _ in range(15):
            time.sleep(1)
            b_str = run_metabase_js("""
            (() => {
                if (window.__batch1072_cards_res) return JSON.stringify({status: 'done', results: window.__batch1072_cards_res});
                if (window.__batch1072_cards_err) return JSON.stringify({status: 'error', error: window.__batch1072_cards_err});
                return JSON.stringify({status: 'waiting'});
            })()
            """)
            b_parsed = json.loads(b_str)
            if b_parsed.get("status") == "done":
                batch_res = b_parsed.get("results", [])
                break

        if not batch_res:
            print(f"  [WARN] Batch {i} timed out or failed.")
            continue

        for item in batch_res:
            cid = item.get("cid")
            cdata = item.get("data")
            if not cdata or "dataset_query" not in cdata:
                continue

            query_obj = cdata.get("dataset_query", {})
            sql_text = ""
            template_tags = {}

            # Format 1: MBQL v1 native
            if "native" in query_obj and isinstance(query_obj["native"], dict):
                sql_text = query_obj["native"].get("query", "")
                template_tags = query_obj["native"].get("template-tags", {})

            # Format 2: MBQL v2 stages
            if not sql_text and "stages" in query_obj and isinstance(query_obj["stages"], list):
                for stg in query_obj["stages"]:
                    if isinstance(stg, dict):
                        if "native" in stg:
                            if isinstance(stg["native"], str):
                                sql_text = stg["native"]
                            elif isinstance(stg["native"], dict):
                                sql_text = stg["native"].get("query", "")
                        if "template-tags" in stg and isinstance(stg["template-tags"], dict):
                            template_tags = stg["template-tags"]
                        if sql_text:
                            break

            has_sql = bool(sql_text.strip())

            extracted_card = {
                "card_id": cid,
                "name": cdata.get("name"),
                "description": cdata.get("description"),
                "display": cdata.get("display"),
                "tab_id": card_tab_map.get(cid, {}).get("tab_id"),
                "tab_name": card_tab_map.get(cid, {}).get("tab_name"),
                "database_id": cdata.get("database_id"),
                "has_sql": has_sql,
                "sql": sql_text.strip(),
                "template_tags": template_tags,
                "query_type": query_obj.get("type", "native"),
                "visualization_settings": cdata.get("visualization_settings", {}),
                "result_metadata": cdata.get("result_metadata", [])
            }
            all_extracted_cards.append(extracted_card)

    print(f"\n✅ Total Cards Successfully Extracted: {len(all_extracted_cards)}")
    sql_cards_cnt = sum(1 for c in all_extracted_cards if c.get("has_sql"))
    print(f"   • SQL-containing cards: {sql_cards_cnt}")
    print(f"   • Non-SQL cards: {len(all_extracted_cards) - sql_cards_cnt}")

    full_payload = {
        "dashboard_id": dash_info.get("id"),
        "dashboard_name": dash_info.get("name"),
        "description": dash_info.get("description"),
        "tabs": dash_info.get("tabs"),
        "parameters": dash_info.get("parameters"),
        "cards_extracted_count": len(all_extracted_cards),
        "sql_cards_count": sql_cards_cnt,
        "cards": all_extracted_cards
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(full_payload, f, indent=2, ensure_ascii=False)

    print(f"\n📁 Saved extracted dataset to: {OUTPUT_PATH}")

if __name__ == "__main__":
    main()
