import os
import json
from google import genai
from openai import OpenAI

def get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    return genai.Client(api_key=api_key)

def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    if not api_key:
        return None
    return OpenAI(api_key=api_key, base_url=base_url, timeout=120.0)

def get_deepseek_client():
    api_key = os.getenv("DEEPSEEK_API_KEY")
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    if not api_key:
        return None
    return OpenAI(api_key=api_key, base_url=base_url, timeout=120.0)

def _call_llm_with_fallback(prompt, config, system_prompt="You are a helpful assistant designed to output JSON.", title_context=""):
    """
    Executes the Triple-Tier Cascade Router:
    1. Google Gemini (gemini-2.5-flash)
    2. OpenAI (gpt-5.4-mini for scoring, gpt-5.5 for heavy lifting)
    3. DeepSeek (deepseek-v4-flash for scoring, deepseek-v4-pro for heavy lifting)
    """
    
    is_heavy = "VC Analyst" in system_prompt
    openai_model = "gpt-5.5" if is_heavy else "gpt-5.4-mini"
    deepseek_model = "deepseek-v4-pro" if is_heavy else "deepseek-v4-flash"
    
    # 1. Gemini
    gemini_client = get_gemini_client()
    if gemini_client:
        try:
            response = gemini_client.models.generate_content(
                model='gemini-2.5-flash', 
                contents=prompt,
                config=genai.types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                )
            )
            return json.loads(response.text)
        except Exception as e:
            err_str = str(e).lower()
            if "429" in err_str or "exhausted" in err_str or "quota" in err_str or "503" in err_str or "unavailable" in err_str:
                print(f"Gemini limit/overload for '{title_context}'. Falling back to OpenAI...", flush=True)
            else:
                print(f"Gemini error for '{title_context}': {e}. Halting execution!", flush=True)
                raise e

    # 2. OpenAI
    openai_client = get_openai_client()
    if openai_client:
        try:
            response = openai_client.chat.completions.create(
                model=openai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            print(f"OpenAI error/limit for '{title_context}': {e}. Falling back to DeepSeek...", flush=True)

    # 3. DeepSeek
    deepseek_client = get_deepseek_client()
    if deepseek_client:
        try:
            response = deepseek_client.chat.completions.create(
                model=deepseek_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            print(f"DeepSeek error for '{title_context}': {e}. Halting execution!", flush=True)
            raise e
            
    raise Exception(f"All LLM APIs failed or are unconfigured for '{title_context}'")
