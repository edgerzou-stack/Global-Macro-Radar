import time
from functools import wraps
import threading

def ttl_cache(ttl_seconds):
    cache = {}
    cache_lock = threading.Lock()
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Create a cache key from args and kwargs
            key = str(args) + str(sorted(kwargs.items()))
            now = time.time()
            with cache_lock:
                if key in cache:
                    result, timestamp = cache[key]
                    if now - timestamp < ttl_seconds:
                        return result
            
            result = func(*args, **kwargs)
            
            with cache_lock:
                cache[key] = (result, now)
            return result
        return wrapper
    return decorator
