import os
import json
import time
import hashlib
import tempfile
from urllib.parse import urlsplit, urlunsplit

CACHE_FILE = os.path.abspath(
    os.environ.get(
        "RADAR_CACHE_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "article_cache.json"),
    )
)
CACHE_TTL_DAYS = 30
SECONDS_IN_A_DAY = 86400

MAX_CACHE_ENTRIES = 1000
CACHE_SCHEMA_VERSION = 2


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_url(url):
    if not url:
        return ""
    parts = urlsplit(str(url).strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def build_cache_key(article, config, prompt_version, provider, model):
    """Build a content- and execution-versioned cache key.

    URL alone is insufficient because publishers can update an article in place,
    and scoring semantics change when the rubric, model, or configuration changes.
    """
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "url": _canonical_url(article.get("link") or article.get("url")),
        "content": {
            "title": article.get("title", ""),
            "summary": article.get("summary", ""),
            "content": article.get("content", ""),
        },
        "prompt_version": str(prompt_version),
        "provider": str(provider),
        "model": str(model),
        "config": config,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def make_cache_entry(cache_key, score_data, **extra):
    entry = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "cache_key": cache_key,
        "timestamp": time.time(),
        "score_data": score_data,
    }
    entry.update(extra)
    return entry


def get_cached_score(entry, expected_cache_key):
    if not isinstance(entry, dict):
        return None
    if entry.get("schema_version") != CACHE_SCHEMA_VERSION:
        return None
    if entry.get("cache_key") != expected_cache_key:
        return None
    score_data = entry.get("score_data")
    return score_data if isinstance(score_data, dict) else None

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
                
            current_time = time.time()
            keys_to_delete = []
            for key, value in cache_data.items():
                if isinstance(value, dict) and "timestamp" in value:
                    if current_time - value["timestamp"] > CACHE_TTL_DAYS * SECONDS_IN_A_DAY:
                        keys_to_delete.append(key)
                        
            if keys_to_delete:
                for key in keys_to_delete:
                    del cache_data[key]
                print(f"Pruned {len(keys_to_delete)} expired cache entries.", flush=True)
                
            # Limit to MAX_CACHE_ENTRIES
            if len(cache_data) > MAX_CACHE_ENTRIES:
                sorted_entries = sorted(
                    cache_data.items(), 
                    key=lambda item: item[1].get("timestamp", 0) if isinstance(item[1], dict) else 0,
                    reverse=True
                )
                keys_to_delete = [item[0] for item in sorted_entries[MAX_CACHE_ENTRIES:]]
                for key in keys_to_delete:
                    del cache_data[key]
                print(f"Pruned {len(keys_to_delete)} cache entries to maintain max size.", flush=True)
                
            return cache_data
        except Exception as e:
            print(f"Error loading cache: {e}. Starting with empty cache.", flush=True)
            return {}
    return {}

def save_cache(cache_data):
    # Ensure newly added entries have a timestamp
    current_time = time.time()
    for key, value in cache_data.items():
        if isinstance(value, dict) and "timestamp" not in value:
            value["timestamp"] = current_time
            
    temp_path = None
    try:
        target_path = os.path.abspath(CACHE_FILE)
        target_dir = os.path.dirname(target_path)
        os.makedirs(target_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target_dir,
            prefix=f".{os.path.basename(target_path)}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            temp_path = f.name
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, target_path)
        temp_path = None
    except Exception as e:
        print(f"Error saving cache: {e}", flush=True)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
