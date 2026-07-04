import os
import json
import time

CACHE_FILE = "article_cache.json"
CACHE_TTL_DAYS = 30
SECONDS_IN_A_DAY = 86400

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
                
            # 修改 P1.8: 清理超过 30 天的缓存
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
