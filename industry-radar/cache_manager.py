import os
import json
import time

CACHE_FILE = "article_cache.json"
CACHE_TTL_DAYS = 30
SECONDS_IN_A_DAY = 86400

MAX_CACHE_ENTRIES = 1000

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
            
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving cache: {e}", flush=True)
