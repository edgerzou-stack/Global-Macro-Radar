import os
import json
import sys

# Load env variables (ensuring keys are available)
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

radar_env_path = os.environ.get("RADAR_ENV", os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"))
_load_env(radar_env_path)

# Import the systematic LLM router from industry-radar
radar_root = os.environ.get("RADAR_ROOT", os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "industry-radar"))
sys.path.append(radar_root)

try:
    from llm_router import _call_llm_with_fallback
except ImportError as e:
    _call_llm_with_fallback = None
    print(f"Failed to import llm_router from {radar_root}: {e}")


# The quant pipeline must never infer Gemini availability merely from a key in
# the parent process. Gemini and OpenAI are explicitly disabled; DeepSeek is
# the only provider authorized to process quant hot-spot context.
QUANT_LLM_CONFIG = {
    "llm": {
        "order": ["deepseek", "openai"],
        "providers": {
            "gemini": {"enabled": False},
            "deepseek": {"enabled": True},
            "openai": {"enabled": False},
        },
    }
}

def call_llm(prompt, require_json=False):
    """
    Systematically routes all LLM calls through industry-radar's Triple-Tier Cascade Router.
    Removes duplicated, brittle temporary patches.
    """
    if _call_llm_with_fallback is None:
        return {} if require_json else "LLM Router not found."
    
    try:
        # Use the project-wide provider policy; Gemini remains disabled even if
        # the invoking agent exports a GEMINI_API_KEY.
        res = _call_llm_with_fallback(
            prompt=prompt,
            config=QUANT_LLM_CONFIG,
            system_prompt="You are a helpful assistant designed to output JSON.",
            title_context="Quant Strategy Call"
        )
        if not require_json:
            return json.dumps(res, ensure_ascii=False) if isinstance(res, (dict, list)) else res
            
        if isinstance(res, (dict, list)):
            return res
            
        # Fallback regex parser for hallucinated JSON
        if isinstance(res, str):
            try:
                import re
                # Try to extract JSON from markdown code block or curly braces
                json_match = re.search(r'```json\s*(\{.*\}|\[.*\])\s*```', res, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group(1))
                    
                json_match = re.search(r'(\{.*\})', res, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group(1))
            except Exception as e:
                print(f"Fallback Regex JSON parsing failed: {e}")
                
        return res
    except Exception as e:
        print(f"LLM Error in call_llm: {e}")
        return {} if require_json else f"LLM Error: {e}"
