import subprocess
import json
import base64
import time
import os

def run_metabase_js(js_code: str) -> str:
    """Executes JS in background inside the open Metabase tab."""
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
    print("=" * 70)
    print(" EXTRACTING ALL TABS & CARDS FROM METABASE DASHBOARD 1452")
    print("=" * 70)

    # 1. Fetch dashboard overview
    trigger_dash_js = """
    window.__dash1452_full = null;
    fetch('/api/dashboard/1452')
        .then(r => r.json())
        .then(d => { window.__dash1452_full = d; })
        .catch(e => { window.__dash1452_full_err = e.toString(); });
    """
    run_metabase_js(trigger_dash_js)
    time.sleep(2)

    get_dash_js = """
    (() => {
        const d = window.__dash1452_full;
        if (!d) return JSON.stringify({status: 'waiting'});
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
                card: dc.card
            }))
        });
    })()
    """
    dash_info = json.loads(run_metabase_js(get_dash_js))
    if "raw_dashcards" not in dash_info:
        print("[ERROR] Could not fetch dashboard 1452:", dash_info)
        return

    tabs_map = {t["id"]: t["name"] for t in dash_info.get("tabs", [])}
    raw_dashcards = dash_info.get("raw_dashcards", [])
    print(f"Dashboard: '{dash_info.get('name')}' (ID: {dash_info.get('id')})")
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
                "tab_name": tabs_map.get(dc.get("dashboard_tab_id"), "General")
            }

    print(f"\nUnique Question Card IDs to fetch: {len(unique_card_ids)}")

    # 3. Fetch full card details in batches of 10
    batch_size = 10
    all_extracted_cards = []

    for i in range(0, len(unique_card_ids), batch_size):
        batch = unique_card_ids[i:i + batch_size]
        print(f"Fetching cards batch {i + 1} to {min(i + batch_size, len(unique_card_ids))} of {len(unique_card_ids)}...")
        
        trigger_batch_js = f"""
        window.__batch_cards_res = null;
        window.__batch_cards_err = null;
        Promise.all({json.dumps(batch)}.map(cid => fetch('/api/card/' + cid).then(r => r.json())))
            .then(res => {{ window.__batch_cards_res = res; }})
            .catch(e => {{ window.__batch_cards_err = e.toString(); }});
        """
        run_metabase_js(trigger_batch_js)
        time.sleep(2)

        get_batch_js = """
        (() => {
            if (window.__batch_cards_err) return JSON.stringify({error: window.__batch_cards_err});
            if (!window.__batch_cards_res) return JSON.stringify({status: 'waiting'});
            return JSON.stringify(window.__batch_cards_res);
        })()
        """
        batch_res_raw = run_metabase_js(get_batch_js)
        batch_cards = json.loads(batch_res_raw)
        
        if isinstance(batch_cards, list):
            for c in batch_cards:
                if not isinstance(c, dict) or "id" not in c:
                    continue
                cid = c["id"]
                tab_info = card_tab_map.get(cid, {})
                
                # Extract SQL from stages or native query
                sql = None
                db_id = None
                template_tags = {}
                
                dq = c.get("dataset_query") or {}
                if "stages" in dq and isinstance(dq["stages"], list) and len(dq["stages"]) > 0:
                    stage = dq["stages"][0]
                    if isinstance(stage, dict):
                        sql = stage.get("native")
                        template_tags = stage.get("template-tags", {})
                        db_id = dq.get("database")
                elif "native" in dq and isinstance(dq["native"], dict):
                    sql = dq["native"].get("query")
                    template_tags = dq["native"].get("template-tags", {})
                    db_id = dq.get("database")

                all_extracted_cards.append({
                    "card_id": cid,
                    "name": c.get("name"),
                    "description": c.get("description") or "",
                    "display": c.get("display"),
                    "tab_id": tab_info.get("tab_id"),
                    "tab_name": tab_info.get("tab_name"),
                    "database_id": db_id,
                    "has_sql": bool(sql),
                    "sql": sql,
                    "template_tags": template_tags,
                    "visualization_settings": c.get("visualization_settings", {}),
                    "result_metadata": c.get("result_metadata", [])
                })
        else:
            print(f"[WARN] Batch returned non-list: {batch_res_raw[:200]}")

    # 4. Save extracted dataset
    os.makedirs("extracted_data", exist_ok=True)
    out_file = "extracted_data/metabase_1452_full_dashboard.json"
    full_output = {
        "dashboard_id": dash_info.get("id"),
        "dashboard_name": dash_info.get("name"),
        "dashboard_description": dash_info.get("description"),
        "tabs": dash_info.get("tabs"),
        "parameters": dash_info.get("parameters"),
        "total_extracted_cards": len(all_extracted_cards),
        "cards_with_sql_count": sum(1 for c in all_extracted_cards if c.get("has_sql")),
        "cards": all_extracted_cards
    }

    with open(out_file, "w") as f:
        json.dump(full_output, f, indent=2)

    print("\n" + "=" * 70)
    print(f" [SUCCESS] Extraction complete! Saved {len(all_extracted_cards)} cards to {out_file}")
    print(f" Cards with Native SQL: {full_output['cards_with_sql_count']}")
    print("=" * 70)

if __name__ == "__main__":
    main()
