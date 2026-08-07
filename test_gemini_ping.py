"""Quick ping test for Google Gemini 2.5 Flash via the Google AI API."""

import google.generativeai as genai
import os
import time

API_KEY = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")

if not API_KEY:
    print("❌ No API key found. Set GOOGLE_API_KEY or GEMINI_API_KEY env var.")
    exit(1)

genai.configure(api_key=API_KEY)

model_name = "gemini-2.5-flash"
print(f"🔍 Pinging model: {model_name}")

try:
    start = time.time()
    model = genai.GenerativeModel(model_name)
    response = model.generate_content("Say 'pong' and nothing else.")
    elapsed = time.time() - start

    print(f"✅ Success! Response: {response.text.strip()}")
    print(f"⏱️  Round-trip time: {elapsed:.2f}s")
except Exception as e:
    print(f"❌ Failed: {type(e).__name__}: {e}")
