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

target_url = "https://metabase.om.yo-digital.com/question/38588-test-question"

print(f"🚀 Navigating Chrome to {target_url}...")
nav_script = f"""
tell application "Google Chrome"
    activate
    if (count every window) = 0 then
        make new window
    end if
    set URL of active tab of front window to "{target_url}"
end tell
"""
run_applescript(nav_script)

print("⏳ Waiting 8 seconds for the question page to load...")
time.sleep(8)

sql_query = "SELECT * FROM eshop_data.es_events_arr_v2 LIMIT 100000;"

# Try to set text via Ace / Monaco / DOM or Fallback to Keystrokes
js_set_and_run = f"""
(function() {{
    try {{
        // 1. Try Monaco editor
        if (window.monaco && monaco.editor && monaco.editor.getModels().length > 0) {{
            monaco.editor.getModels()[0].setValue("{sql_query}");
            return "Set via Monaco";
        }}
        
        // 2. Try Ace editor
        const aceEl = document.querySelector('.ace_editor');
        if (aceEl && window.ace && ace.edit) {{
            const editor = ace.edit(aceEl);
            editor.setValue("{sql_query}");
            editor.clearSelection();
            return "Set via Ace";
        }}

        // 3. Try CodeMirror
        const cmEl = document.querySelector('.CodeMirror');
        if (cmEl && cmEl.CodeMirror) {{
            cmEl.CodeMirror.setValue("{sql_query}");
            return "Set via CodeMirror";
        }}

        // 4. Focus editor element for keystroke fallback
        const target = document.querySelector('.ace_editor, .monaco-editor, textarea, .ace_text-input, .inputarea');
        if (target) {{
            target.focus();
            return "Focused for Keystroke";
        }}

        return "No known editor DOM found";
    }} catch(e) {{
        return "JS Error: " + e.toString();
    }}
}})();
"""

res = execute_chrome_js(js_set_and_run)
print(f"Editor detection result: {res}")

# Always execute keystrokes as a guaranteed delivery if direct DOM set didn't trigger full Metabase state
print("⌨️ Sending keystrokes to ensure text is replaced and query is executed...")
keystroke_script = f"""
tell application "Google Chrome" to activate
delay 0.5
tell application "System Events"
    keystroke "a" using command down
    delay 0.2
    key code 51
    delay 0.2
    keystroke "{sql_query}"
    delay 0.5
    key code 36 using command down
end tell
"""
run_applescript(keystroke_script)

print("▶️ Query execution triggered! Now monitoring for query completion and Download CSV button...")

js_poll_and_download = """
window.__download_state = "POLLING";
(function() {
    let checkCount = 0;
    const interval = setInterval(() => {
        checkCount++;
        try {
            // Find download button or icon
            const downloadBtn = Array.from(document.querySelectorAll('button, a, div[role="button"]')).find(el => {
                const label = (el.getAttribute('aria-label') || '').toLowerCase();
                const title = (el.getAttribute('title') || '').toLowerCase();
                const text = (el.textContent || '').toLowerCase();
                return label.includes('download') || 
                       title.includes('download') || 
                       (el.querySelector('svg') && text.includes('download')) ||
                       el.className.toString().includes('Download');
            });

            if (downloadBtn) {
                downloadBtn.click();
                setTimeout(() => {
                    const csvBtn = Array.from(document.querySelectorAll('button, a, div, span')).find(el => {
                        const txt = (el.textContent || '').trim();
                        return txt === 'CSV' || txt === '.csv' || txt === 'Download CSV';
                    });
                    if (csvBtn) {
                        csvBtn.click();
                        window.__download_state = "DOWNLOAD_CLICKED";
                        clearInterval(interval);
                    }
                }, 1000);
            }

            if (checkCount > 180) { // 6 minutes max timeout
                clearInterval(interval);
                window.__download_state = "TIMEOUT";
            }
        } catch (e) {
            window.__download_state = "ERROR: " + e.toString();
            clearInterval(interval);
        }
    }, 2000);
})();
"""
execute_chrome_js(js_poll_and_download)

for i in range(180):
    time.sleep(2)
    state = execute_chrome_js("window.__download_state")
    print(f"Download status [{i*2}s]: {state}")
    if state == "DOWNLOAD_CLICKED":
        print("🎉 Successfully clicked Download CSV!")
        break
    elif state in ("TIMEOUT", "ERROR") or "ERROR" in str(state):
        print(f"Ended with state: {state}")
        break
