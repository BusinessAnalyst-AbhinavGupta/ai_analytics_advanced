import requests
import time

def test_ollama_direct():
    url = "http://127.0.0.1:11434/api/chat"
    payload = {
        "model": "qwen2.5-coder:14b",
        "messages": [
            {"role": "user", "content": "Write a 2-line Athena SQL query selecting 1 from dual."}
        ],
        "stream": False,
        "options": {
            "temperature": 0.0,
            "num_ctx": 4096
        }
    }
    print("Testing direct Ollama chat call...", flush=True)
    t0 = time.time()
    res = requests.post(url, json=payload, timeout=30)
    t1 = time.time()
    print(f"Status: {res.status_code} in {t1 - t0:.2f}s", flush=True)
    print("Response:", res.json().get("message", {}).get("content"), flush=True)

if __name__ == "__main__":
    test_ollama_direct()
