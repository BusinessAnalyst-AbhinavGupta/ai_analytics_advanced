import asyncio
import sys
from playwright.async_api import async_playwright

async def run_metabase():
    print("🚀 Launching Chrome instance...", flush=True)
    async with async_playwright() as p:
        # headless=False so the user can interact/login
        browser = await p.chromium.launch(headless=False)
        
        # Give permission for downloads automatically
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        
        print("🔗 Navigating to https://metabase.om.yo-digital.com/ ...", flush=True)
        await page.goto("https://metabase.om.yo-digital.com/")
        
        print("⏳ Please log in if prompted. Waiting for the Metabase homepage to load (looking for 'New' button)...", flush=True)
        # Wait up to 5 minutes for the user to pass SSO/VPN
        new_button = page.locator('text="New", button:has-text("New")').first
        await new_button.wait_for(state="visible", timeout=300000)
        
        print("🖱️ Clicking 'New'...", flush=True)
        await new_button.click()
        
        print("🖱️ Clicking 'SQL query' or 'Native query'...", flush=True)
        # It could be named "SQL query" or "Native query" depending on Metabase version
        sql_option = page.locator('text="SQL query", text="Native query"').first
        await sql_option.wait_for(state="visible", timeout=10000)
        await sql_option.click()
        
        print("⏳ Waiting for SQL editor to load...", flush=True)
        # Often prompts to select a Database if multiple exist.
        print("👉 IMPORTANT: If it prompts to select a Database, please select it manually now!", flush=True)
        
        # Wait for the code editor to become visible
        editor = page.locator('.ace_editor, .CodeMirror, .monaco-editor, textarea').first
        await editor.wait_for(state="visible", timeout=60000)
        
        # Give it a second to settle, then click and type
        await page.wait_for_timeout(2000)
        print("⌨️ Typing query...", flush=True)
        await editor.click()
        
        # Select all and delete anything already there
        await page.keyboard.press("Meta+A")
        await page.keyboard.press("Backspace")
        
        sql_query = "SELECT * FROM eshop_data.es_events_arr_v2 LIMIT 100000;"
        await page.keyboard.type(sql_query, delay=10)
        
        print("▶️ Running query (pressing Cmd+Enter)...", flush=True)
        await page.keyboard.press("Meta+Enter")
        
        print("⏳ Waiting for query to finish and Download button to appear (can take a few minutes)...", flush=True)
        # Metabase typically has an icon-download or aria-label="Download" on the button
        download_btn = page.locator('button[aria-label="Download"], .icon-download, path[d*="download"]').first
        
        # Wait up to 10 minutes for the 100k rows to finish
        await download_btn.wait_for(state="visible", timeout=600000)
        
        print("🖱️ Query finished! Clicking Download...", flush=True)
        # Sometimes it needs a slight delay before it's clickable
        await page.wait_for_timeout(1000)
        await download_btn.click()
        
        print("🖱️ Selecting 'CSV' format...", flush=True)
        csv_option = page.locator('text="CSV", text=".csv"').first
        await csv_option.wait_for(state="visible", timeout=10000)
        
        print("📥 Waiting for file to download...", flush=True)
        async with page.expect_download(timeout=120000) as download_info:
            await csv_option.click()
        
        download = await download_info.value
        filename = download.suggested_filename
        save_path = f"/Users/abhinav.gupta/Documents/AI analytics/scratch/{filename}"
        await download.save_as(save_path)
        
        print(f"✅ Success! File downloaded to: {save_path}", flush=True)
        
        print("Closing browser in 5 seconds...")
        await page.wait_for_timeout(5000)
        await browser.close()

if __name__ == "__main__":
    try:
        asyncio.run(run_metabase())
    except Exception as e:
        print(f"❌ Automation failed: {e}")
        sys.exit(1)
