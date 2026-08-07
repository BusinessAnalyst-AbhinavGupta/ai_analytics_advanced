import subprocess
import base64
import time

def execute_chrome_js(js_code_str):
    b64 = base64.b64encode(js_code_str.encode("utf-8")).decode("ascii")
    runner = f'tell application "Google Chrome" to execute front window\'s active tab javascript "try {{ eval(atob(\'{b64}\')); }} catch (e) {{ window.__metabase_err = e.toString(); return e.toString(); }}"'
    proc = subprocess.run(["osascript", "-e", runner], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return proc.stdout.strip()

def press_keys(script):
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if proc.returncode != 0:
        print("AppleScript Error:", proc.stderr)

sql_query = "SELECT * FROM eshop_data.es_events_arr_v2 LIMIT 100000;"

applescript_keystrokes = f"""
tell application "Google Chrome" to activate
delay 1
tell application "System Events"
    keystroke "a" using command down
    delay 0.5
    key code 51
    delay 0.5
    keystroke "{sql_query}"
    delay 1
    key code 36 using command down
end tell
"""

print("Running Query via Keystrokes...")
press_keys(applescript_keystrokes)

print("Starting polling for Download button via JS...")

js_poll_download = """
window.__mb_download_status = "Waiting for button...";
(function() {
    let attempts = 0;
    const interval = setInterval(() => {
        attempts++;
        try {
            // Find download button (Metabase usually has an SVG with a specific path or an element with tooltip 'Download')
            const downloadBtn = Array.from(document.querySelectorAll('button, a, div')).find(el => {
                const label = el.getAttribute('aria-label') || '';
                const title = el.getAttribute('title') || '';
                const text = el.textContent || '';
                return label.toLowerCase().includes('download') || 
                       title.toLowerCase().includes('download') || 
                       (el.querySelector('svg') && text.toLowerCase().includes('download'));
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
            } else if (attempts > 60) {
                clearInterval(interval);
                window.__mb_download_status = "Timeout waiting for Download button after 2 mins.";
            }
        } catch(e) {
            clearInterval(interval);
            window.__mb_download_status = "JS Error: " + e.toString();
        }
    }, 2000);
})();
"""

execute_chrome_js(js_poll_download)

status = "Waiting for button..."
for _ in range(60):
    time.sleep(2)
    status = execute_chrome_js("window.__mb_download_status")
    print(f"Status: {status}")
    if status and status != "Waiting for button..." and "missing value" not in status:
        break

print("Final Status:", status)
