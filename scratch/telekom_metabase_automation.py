import subprocess
import base64
import time

def execute_chrome_js(js_code_str):
    b64 = base64.b64encode(js_code_str.encode("utf-8")).decode("ascii")
    runner = f'tell application "Google Chrome" to execute front window\'s active tab javascript "try {{ eval(atob(\'{b64}\')); }} catch (e) {{ return \'ERROR: \' + e.toString(); }}"'
    proc = subprocess.run(["osascript", "-e", runner], capture_output=True, text=True)
    if proc.returncode != 0:
        return "OSASCRIPT_ERROR: " + proc.stderr
    return proc.stdout.strip()

def run_applescript(script):
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if proc.returncode != 0:
        print("AppleScript Error:", proc.stderr)
    return proc.stdout.strip()

print("⏳ Waiting 10 seconds so you can click into the Metabase SQL Editor...")
for i in range(10, 0, -1):
    print(f"Starting in {i} seconds...")
    time.sleep(1)

print("🚀 Activating Chrome and typing query...")
sql_query = "SELECT * FROM eshop_data.es_events_arr_v2 LIMIT 100000;"

keystroke_script = f"""
tell application "Google Chrome" to activate
delay 0.5
tell application "System Events"
    keystroke "a" using command down
    delay 0.2
    key code 51
    delay 0.2
    keystroke "{sql_query}"
    delay 1
    key code 36 using command down
end tell
"""
run_applescript(keystroke_script)

print("▶️ Query running! Polling for Download button (this might take a few minutes)...")

js_poll_download = """
window.__mb_download_status = "Waiting for button...";
(function() {
    let attempts = 0;
    const interval = setInterval(() => {
        attempts++;
        try {
            const downloadBtn = Array.from(document.querySelectorAll('button, a, div')).find(el => {
                const label = (el.getAttribute('aria-label') || '').toLowerCase();
                const title = (el.getAttribute('title') || '').toLowerCase();
                const text = (el.textContent || '').toLowerCase();
                return label.includes('download') || 
                       title.includes('download') || 
                       (el.querySelector('svg') && text.includes('download'));
            });
            
            if (downloadBtn) {
                clearInterval(interval);
                downloadBtn.click();
                
                setTimeout(() => {
                    const csvBtn = Array.from(document.querySelectorAll('*')).find(el => {
                        const txt = el.textContent || '';
                        return txt.trim() === 'CSV' || txt.trim() === 'csv';
                    });
                    if (csvBtn) {
                        csvBtn.click();
                        window.__mb_download_status = "Successfully clicked CSV download!";
                    } else {
                        window.__mb_download_status = "Download menu opened but CSV option not found.";
                    }
                }, 1000);
            } else if (attempts > 300) { // 10 minutes timeout
                clearInterval(interval);
                window.__mb_download_status = "Timeout waiting for Download button after 10 mins.";
            }
        } catch(e) {
            clearInterval(interval);
            window.__mb_download_status = "JS Error: " + e.toString();
        }
    }, 2000);
    return "Polling started...";
})();
"""

execute_chrome_js(js_poll_download)

status = "Waiting for button..."
for _ in range(300):
    time.sleep(2)
    status = execute_chrome_js("window.__mb_download_status")
    print(f"Status: {status}")
    if status and status != "Waiting for button..." and "missing value" not in status:
        break

print("✅ Final Status:", status)
