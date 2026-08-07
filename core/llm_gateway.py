import os
import json
import re
import time
import requests
from typing import Dict, Any, List, Optional

class LLMGateway:
    """
    Unified, multi-provider LLM gateway supporting:
    1. OpenRouter API (Primary preference - DeepSeek V4 Flash)
    2. Local Ollama API
    3. OpenAI API
    4. Google Gemini API (Dropdown selectable only)
    """

    PROVIDERS = ["OpenRouter API", "Local Ollama", "OpenAI API", "Google Gemini API"]
    
    DEFAULT_MODELS = {
        "OpenRouter API": [
            "deepseek/deepseek-v4-flash-0731",
            "deepseek/deepseek-chat",
            "deepseek/deepseek-r1",
            "anthropic/claude-3.5-sonnet",
            "openai/gpt-4o",
            "meta-llama/llama-3.3-70b-instruct",
            "qwen/qwen-2.5-coder-32b-instruct",
            "google/gemini-2.5-flash"
        ],
        "Local Ollama": ["qwen2.5-coder:14b", "qwen2.5-coder:7b", "qwen2.5:0.5b", "gemma4:12b", "gemma4:latest", "deepseek-r1:14b"],
        "OpenAI API": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o3-mini"],
        "Google Gemini API": ["gemini-3.5-flash", "gemini-flash-latest", "gemini-3.1-flash-lite", "gemini-pro-latest", "gemini-3-pro-preview"]
    }
    
    _MODELS_CACHE = {}

    @staticmethod
    def get_available_models(provider: str, api_key: str = "", ollama_url: str = "http://127.0.0.1:11434") -> List[str]:
        """Dynamically fetches models with a 120s memory cache to keep Streamlit UI super responsive."""
        cache_key = f"{provider}::{api_key[:10] if api_key else ''}::{ollama_url}"
        now = time.time()
        if cache_key in LLMGateway._MODELS_CACHE:
            cached_time, cached_res = LLMGateway._MODELS_CACHE[cache_key]
            if now - cached_time < 120:
                return cached_res
        res_models = LLMGateway.DEFAULT_MODELS.get(provider, ["default"])
        
        if provider == "OpenRouter API":
            primary_models = LLMGateway.DEFAULT_MODELS.get("OpenRouter API", ["deepseek/deepseek-v4-flash-0731"])
            res_models = list(primary_models)
            if api_key:
                try:
                    res = requests.get("https://openrouter.ai/api/v1/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=4)
                    if res.status_code == 200:
                        models = [m["id"] for m in res.json().get("data", [])]
                        if models:
                            top_picks = [m for m in primary_models if m in models]
                            deepseek_models = [m for m in models if "deepseek" in m.lower() and m not in top_picks]
                            other_models = [m for m in models if m not in top_picks and m not in deepseek_models]
                            res_models = top_picks + deepseek_models[:15] + other_models[:30]
                            if "deepseek/deepseek-v4-flash-0731" not in res_models:
                                res_models.insert(0, "deepseek/deepseek-v4-flash-0731")
                except Exception:
                    pass

        elif provider == "Google Gemini API" and api_key:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
                res = requests.get(url, timeout=4)
                if res.status_code == 200:
                    models = res.json().get("models", [])
                    extracted = []
                    for m in models:
                        if "generateContent" in m.get("supportedGenerationMethods", []):
                            name = m["name"].replace("models/", "")
                            extracted.append(name)
                    if extracted:
                        fast_order = ["gemini-3.5-flash", "gemini-flash-latest", "gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-pro-latest"]
                        top_models = [m for m in fast_order if m in extracted]
                        remaining_flash = [m for m in extracted if "flash" in m and m not in top_models]
                        other_models = [m for m in extracted if m not in top_models and m not in remaining_flash]
                        res_models = top_models + remaining_flash + other_models
            except Exception:
                pass

        elif provider == "Local Ollama":
            try:
                res = requests.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=2)
                if res.status_code == 200:
                    names = [m["name"] for m in res.json().get("models", [])]
                    if names:
                        sorted_names = sorted(names, key=lambda x: (
                            0 if "qwen2.5-coder:14b" in x else (
                                1 if "qwen2.5-coder:7b" in x else (
                                    2 if "qwen" in x else 3
                                )
                            )
                        ))
                        res_models = sorted_names
            except Exception:
                pass

        elif provider == "OpenAI API" and api_key:
            try:
                res = requests.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=4)
                if res.status_code == 200:
                    models = [m["id"] for m in res.json().get("data", []) if "gpt" in m["id"] or "o1" in m["id"] or "o3" in m["id"]]
                    if models:
                        res_models = models
            except Exception:
                pass

        LLMGateway._MODELS_CACHE[cache_key] = (now, res_models)
        return res_models

    @staticmethod
    def get_ollama_model_max_context(model: str, ollama_url: str = "http://127.0.0.1:11434") -> int:
        """Queries Ollama for the model's native context length and accounts for architecture-supported RoPE scaling."""
        m_lower = model.lower()
        if "qwen2.5" in m_lower or "qwen" in m_lower or "deepseek" in m_lower:
            # Qwen 2.5 and DeepSeek architectures officially support up to 128k context via RoPE/YaRN
            return 131072
        elif "gemma4" in m_lower:
            # Gemma 4 natively supports 256k context
            return 262144
        elif "llama-3" in m_lower or "llama3" in m_lower:
            # Llama 3.1 / 3.2 / 3.3 supports 128k context
            return 131072

        try:
            res = requests.post(f"{ollama_url.rstrip('/')}/api/show", json={"model": model}, timeout=2)
            if res.status_code == 200:
                data = res.json()
                model_info = data.get("model_info", {})
                for k, v in model_info.items():
                    if "context_length" in k and isinstance(v, (int, float)):
                        return int(v)
        except Exception:
            pass
        return 65536

    @staticmethod
    def generate(
        prompt: str = "",
        system_prompt: str = "",
        messages: Optional[List[Dict[str, str]]] = None,
        provider: str = "OpenRouter API",
        model: str = "deepseek/deepseek-v4-flash-0731",
        api_key: str = "",
        temperature: float = 0.0,
        context_window: int = 262144, # Default 256k tokens
        json_mode: bool = False,
        ollama_url: str = "http://127.0.0.1:11434",
        timeout: int = 240
    ) -> Dict[str, Any]:
        """
        Executes generation across the selected provider with automatic retry on transient errors.
        Supports multi-turn persistent session threads via the `messages` list.
        Auto-adjusts context window to model ceiling if model native context is lower.
        Returns a dict: {"text": str, "raw_response": Any, "provider": str, "model": str, "messages": List[Dict[str, str]]}
        """
        # Prepare working messages list
        chat_messages: List[Dict[str, str]] = []
        if messages and len(messages) > 0:
            chat_messages = [dict(m) for m in messages]
            # If prompt was also provided separately, append as user message
            if prompt and (not chat_messages or chat_messages[-1].get("content") != prompt):
                chat_messages.append({"role": "user", "content": prompt})
        else:
            if system_prompt:
                chat_messages.append({"role": "system", "content": system_prompt})
            if prompt:
                chat_messages.append({"role": "user", "content": prompt})

        # 1. OpenRouter API (Primary Preference)
        if "openrouter" in provider.lower():
            if not api_key:
                api_key = os.getenv("OPENROUTER_API_KEY", "")
            if not api_key:
                # Auto-read from ~/.zshrc, ~/.zprofile, ~/.bash_profile if env is not inherited
                for rc_path in [os.path.expanduser("~/.zshrc"), os.path.expanduser("~/.zprofile"), os.path.expanduser("~/.bash_profile")]:
                    if os.path.exists(rc_path):
                        try:
                            with open(rc_path, "r") as f:
                                for line in f:
                                    if "OPENROUTER_API_KEY" in line and "=" in line:
                                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                                        if val:
                                            api_key = val
                                            os.environ["OPENROUTER_API_KEY"] = val
                                            break
                        except Exception:
                            pass
                    if api_key:
                        break
            if not api_key:
                raise ValueError("OpenRouter API Key is required. Please set the OPENROUTER_API_KEY environment variable or enter it in the UI.")
            
            url = "https://openrouter.ai/api/v1/chat/completions"
            
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost:8501",
                "X-Title": "AI Analytics Assistant"
            }
            payload = {
                "model": model,
                "messages": chat_messages,
                "temperature": temperature
            }
            if "deepseek" in model.lower() or "reasoning" in model.lower():
                payload["reasoning"] = {"enabled": True}

            if json_mode:
                payload["response_format"] = {"type": "json_object"}

            res = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if res.status_code != 200:
                raise RuntimeError(f"OpenRouter API error ({res.status_code}): {res.text}")
            
            data = res.json()
            choices = data.get("choices", [])
            if not choices:
                raise RuntimeError(f"OpenRouter returned empty choices: {json.dumps(data)}")
            
            msg_obj = choices[0].get("message", {})
            text = msg_obj.get("content", "") or ""
            reasoning_details = msg_obj.get("reasoning_details") or msg_obj.get("reasoning", "")
            if not text and reasoning_details:
                text = str(reasoning_details)

            # Append assistant turn to messages
            updated_messages = list(chat_messages) + [{"role": "assistant", "content": text.strip()}]

            return {
                "text": text.strip(),
                "raw_response": data,
                "reasoning_details": reasoning_details,
                "provider": provider,
                "model": model,
                "messages": updated_messages
            }

        # 2. Google Gemini API (Dropdown selectable only)
        elif "gemini" in provider.lower():
            if not api_key:
                api_key = os.getenv("GEMINI_API_KEY", "")
            if not api_key:
                raise ValueError("Google Gemini API Key is required. Please set it in the UI or GEMINI_API_KEY environment variable.")
            
            clean_model = model.replace("models/", "")
            
            # Format Gemini contents from chat_messages
            gemini_contents = []
            gemini_system = system_prompt
            for m in chat_messages:
                r = m.get("role", "user")
                c = m.get("content", "")
                if r == "system":
                    if not gemini_system:
                        gemini_system = c
                elif r == "assistant":
                    gemini_contents.append({"role": "model", "parts": [{"text": c}]})
                else:
                    gemini_contents.append({"role": "user", "parts": [{"text": c}]})

            if not gemini_contents:
                gemini_contents = [{"role": "user", "parts": [{"text": prompt}]}]

            payload = {
                "contents": gemini_contents,
                "generationConfig": {"temperature": temperature, "maxOutputTokens": 8192}
            }
            if gemini_system:
                payload["systemInstruction"] = {"parts": [{"text": gemini_system}]}
            if json_mode:
                payload["generationConfig"]["responseMimeType"] = "application/json"

            fallback_models = [clean_model]
            if clean_model == "gemini-3.5-flash":
                fallback_models.append("gemini-3.1-flash-lite")
            elif clean_model == "gemini-3.1-flash-lite":
                fallback_models.append("gemini-3.5-flash")

            res = None
            last_err_text = ""
            active_model = clean_model
            
            for m_candidate in fallback_models:
                active_model = m_candidate
                candidate_url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_candidate}:generateContent?key={api_key}"
                
                for attempt in range(4):
                    try:
                        res = requests.post(candidate_url, json=payload, timeout=timeout)
                        if res.status_code == 200:
                            break
                        elif res.status_code in [429, 503]:
                            last_err_text = f"{res.status_code}: {res.text}"
                            time.sleep(1.5 * (attempt + 1))
                            continue
                        else:
                            last_err_text = f"{res.status_code}: {res.text}"
                            break
                    except Exception as exc:
                        last_err_text = str(exc)
                        time.sleep(1.0)
                        
                if res is not None and res.status_code == 200:
                    break

            if res is None or res.status_code != 200:
                raise RuntimeError(f"Gemini API generation failed for {active_model}: {last_err_text}")

            data = res.json()
            try:
                text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            except (KeyError, IndexError):
                text = ""
            updated_messages = list(chat_messages) + [{"role": "assistant", "content": text}]
            return {"text": text, "raw_response": data, "provider": provider, "model": active_model, "messages": updated_messages}

        # 3. OpenAI API
        elif "openai" in provider.lower():
            if not api_key:
                api_key = os.getenv("OPENAI_API_KEY", "")
            if not api_key:
                raise ValueError("OpenAI API Key is required. Please provide it in the UI.")
            
            url = "https://api.openai.com/v1/chat/completions"
            
            headers = {"Authorization": f"Bearer {api_key}"}
            payload = {
                "model": model,
                "messages": chat_messages,
                "temperature": temperature
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}

            res = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if res.status_code != 200:
                raise RuntimeError(f"OpenAI API error ({res.status_code}): {res.text}")
            
            data = res.json()
            text = data["choices"][0]["message"]["content"].strip()
            updated_messages = list(chat_messages) + [{"role": "assistant", "content": text}]
            return {"text": text, "raw_response": data, "provider": provider, "model": model, "messages": updated_messages}

        # 4. Local Ollama API (Injects auto-adjusted num_ctx)
        else:
            # Auto-adjust context window: if model limit is lower, clamp to max supported length
            model_max_ctx = LLMGateway.get_ollama_model_max_context(model, ollama_url)
            effective_num_ctx = min(context_window, model_max_ctx) if model_max_ctx else context_window

            url = f"{ollama_url.rstrip('/')}/api/chat"

            payload = {
                "model": model,
                "messages": chat_messages,
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "num_ctx": effective_num_ctx,
                    "num_predict": 4096
                }
            }
            if json_mode:
                payload["format"] = "json"

            try:
                res = requests.post(url, json=payload, timeout=timeout)
                res.raise_for_status()
                data = res.json()
                msg = data.get("message", {})
                text = msg.get("content", "").strip()
                if not text and msg.get("thinking"):
                    text = msg.get("thinking", "").strip()
                updated_messages = list(chat_messages) + [{"role": "assistant", "content": text}]
                return {"text": text, "raw_response": data, "provider": "Local Ollama", "model": model, "num_ctx": effective_num_ctx, "messages": updated_messages}
            except Exception as e:
                # Fallback to generate endpoint
                url_gen = f"{ollama_url.rstrip('/')}/api/generate"
                full_prompt = "\n\n".join([f"{m.get('role', 'user').upper()}: {m.get('content', '')}" for m in chat_messages])
                res = requests.post(url_gen, json={
                    "model": model,
                    "prompt": full_prompt,
                    "stream": False,
                    "options": {
                        "temperature": temperature, 
                        "num_ctx": effective_num_ctx,
                        "num_predict": 4096
                    }
                }, timeout=timeout)
                res.raise_for_status()
                data = res.json()
                text = data.get("response", "").strip()
                updated_messages = list(chat_messages) + [{"role": "assistant", "content": text}]
                return {"text": text, "raw_response": data, "provider": "Local Ollama", "model": model, "num_ctx": effective_num_ctx, "messages": updated_messages}
