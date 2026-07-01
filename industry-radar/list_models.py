import os
import requests
from dotenv import load_dotenv

load_dotenv()

def check_openai_models():
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = requests.get(f"{base_url}/models", headers=headers, timeout=10)
        resp.raise_for_status()
        models = resp.json().get('data', [])
        model_names = sorted([m['id'] for m in models])
        print(f"--- OpenAI Proxy Models ({len(model_names)}) ---")
        for name in model_names:
            if "gpt" in name or "claude" in name or "gemini" in name:
                print(name)
    except Exception as e:
        print(f"Error checking OpenAI models: {e}")

def check_deepseek_models():
    api_key = os.getenv("DEEPSEEK_API_KEY")
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = requests.get(f"{base_url}/models", headers=headers, timeout=10)
        resp.raise_for_status()
        models = resp.json().get('data', [])
        model_names = sorted([m['id'] for m in models])
        print(f"\n--- DeepSeek Models ({len(model_names)}) ---")
        for name in model_names:
            print(name)
    except Exception as e:
        print(f"Error checking DeepSeek models: {e}")

if __name__ == "__main__":
    check_openai_models()
    check_deepseek_models()
