import os
import requests
import json

def _load_env(path):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass
_load_env(os.environ.get("RADAR_ENV", "/Users/zouzhengting/Workplace/Global-Macro-Radar-Core/industry-radar_archived/.env"))

def call_llm(prompt, require_json=False):
    # Try Gemini first
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    openai_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    deepseek_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

    # 1. Try Gemini
    if gemini_key:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={gemini_key}"
            headers = {"Content-Type": "application/json"}
            
            gen_config = {"temperature": 0.0}
            if require_json:
                gen_config["responseMimeType"] = "application/json"
                
            data = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": gen_config
            }
            resp = requests.post(url, headers=headers, json=data, timeout=60)
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text) if require_json else text
        except Exception as e:
            err_msg = str(e)
            import re
            err_msg = re.sub(r"key=([^& ]+)", "key=***HIDDEN***", err_msg)
            print(f"Gemini fallback: {err_msg}")

    # Helper function to fix proxy double-encoding (mojibake)
    def fix_encoding(s):
        try:
            return s.encode('latin-1').decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            return s

    # 2. Try OpenAI
    if openai_key:
        try:
            url = f"{openai_url}/chat/completions"
            headers = {"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"}
            data = {
                "model": "gpt-5.4-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0
            }
            if require_json:
                data["response_format"] = {"type": "json_object"}
                
            resp = requests.post(url, headers=headers, json=data, timeout=60)
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            text = fix_encoding(text)
            return json.loads(text) if require_json else text
        except Exception as e:
            err_msg = str(e)
            print(f"OpenAI fallback: {err_msg}")

    # 3. Try DeepSeek
    if deepseek_key:
        try:
            url = f"{deepseek_url}/chat/completions"
            headers = {"Authorization": f"Bearer {deepseek_key}", "Content-Type": "application/json"}
            data = {
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0
            }
            if require_json:
                data["response_format"] = {"type": "json_object"}
                
            resp = requests.post(url, headers=headers, json=data, timeout=60)
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            text = fix_encoding(text)
            return json.loads(text) if require_json else text
        except Exception as e:
            print(f"DeepSeek fallback: {e}")

    return {} if require_json else "LLM Error: All fallback APIs failed."
