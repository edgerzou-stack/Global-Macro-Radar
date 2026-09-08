import os
import json
import time
import hashlib
import tempfile
import fcntl
from functools import lru_cache

from url_identity import canonicalize_article_url

CACHE_FILE = os.path.abspath(
    os.environ.get(
        "RADAR_CACHE_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "article_cache.json"),
    )
)
CACHE_TTL_DAYS = 30
SECONDS_IN_A_DAY = 86400

# Roughly one month of the current 300-article/day candidate volume.  The
# previous 1,000-entry cap retained only about three runs and defeated the
# content-hash cache on otherwise unchanged articles.
MAX_CACHE_ENTRIES = int(os.environ.get("RADAR_MAX_CACHE_ENTRIES", "10000"))
CACHE_SCHEMA_VERSION = 3
SEMANTIC_CACHE_SCHEMA_VERSION = 1
DEEP_DIVE_MISS_SCHEMA_VERSION = 1
DEEP_DIVE_POLICY_VERSION = "verified-independent-primary-v1"
DEEP_DIVE_MISS_TTL_SECONDS = 24 * 60 * 60
MANUAL_REVIEW_REVOCATIONS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "manual_review_cache_revocations.json",
)


@lru_cache(maxsize=8)
def _load_manual_review_revocations(path):
    # This is deployment policy, not mutable runtime cache. A missing policy
    # must not silently restore scores whose review evidence was invalidated.
    try:
        with open(path, encoding="utf-8") as handle:
            policy = json.load(handle)
        if policy.get("schema_version") != 1 or not isinstance(policy.get("incidents"), list):
            raise ValueError("invalid schema")
        revoked = set()
        for incident in policy["incidents"]:
            run_id = incident["source_run_id"]
            if not isinstance(run_id, str) or not run_id or not incident["reason"]:
                raise ValueError("invalid incident provenance")
            for entry in incident["entries"]:
                key = entry["semantic_input_sha256"]
                fingerprint = entry["score_data_sha256"]
                if any(not isinstance(v, str) or len(v) != 64 or
                       any(c not in "0123456789abcdef" for c in v)
                       for v in (key, fingerprint)):
                    raise ValueError("invalid score identity")
                revoked.add((run_id, key, fingerprint))
        return frozenset(revoked)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise RuntimeError("Manual review cache revocation policy unavailable or invalid") from error


def is_revoked_manual_score(entry):
    """Reject only the exact score and run provenance covered by an incident."""
    if not isinstance(entry, dict) or "manual_review_provenance" not in entry:
        return False
    revoked = _load_manual_review_revocations(MANUAL_REVIEW_REVOCATIONS_FILE)
    provenance = entry.get("manual_review_provenance") or {}
    fingerprint = hashlib.sha256(_canonical_json(entry.get("score_data")).encode("utf-8")).hexdigest()
    return (
        provenance.get("source_run_id"),
        entry.get("semantic_cache_key") or entry.get("cache_key"),
        fingerprint,
    ) in revoked


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_url(url):
    return canonicalize_article_url(url)


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
        "source_evidence": {
            "source_id": article.get("source_id"),
            "source_tier": article.get("source_tier"),
            "source_lane": article.get("source_lane"),
            "source_domains": sorted(article.get("source_domains") or []),
            "authority_for": sorted(article.get("authority_for") or []),
            "trade_eligible": article.get("trade_eligible"),
            "requires_corroboration": article.get("requires_corroboration"),
        },
        "prompt_version": str(prompt_version),
        "provider": str(provider),
        "model": str(model),
        "config": config,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def build_semantic_cache_key(article, config, prompt_version, rules_sha256):
    """Return the provider-independent identity of an actual scoring request.

    Provider and model remain cache-entry provenance. They are deliberately not
    part of this key: an already validated score for the exact same article,
    source evidence, prompt contract, and deterministic rules must not incur a
    second API charge merely because the operator changes the provider order.
    """

    payload = {
        "semantic_schema_version": SEMANTIC_CACHE_SCHEMA_VERSION,
        "url": _canonical_url(article.get("link") or article.get("url")),
        "content": {
            "title": article.get("title", ""),
            "summary": article.get("summary", ""),
            "content": article.get("content", ""),
            "published_at": article.get("published_at", ""),
        },
        "source_evidence": {
            "source": article.get("source"),
            "source_id": article.get("source_id"),
            "source_tier": article.get("source_tier"),
            "source_lane": article.get("source_lane"),
            "source_domains": sorted(article.get("source_domains") or []),
            "authority_for": sorted(article.get("authority_for") or []),
            "trade_eligible": article.get("trade_eligible"),
            "requires_corroboration": article.get("requires_corroboration"),
        },
        "prompt_version": str(prompt_version),
        "rules_sha256": str(rules_sha256),
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
    if is_revoked_manual_score(entry):
        return None
    score_data = entry.get("score_data")
    return score_data if isinstance(score_data, dict) else None


def make_deep_dive_miss(reason, *, now=None):
    return {
        "schema_version": DEEP_DIVE_MISS_SCHEMA_VERSION,
        "policy_version": DEEP_DIVE_POLICY_VERSION,
        "attempted_at": float(time.time() if now is None else now),
        "reason": str(reason),
    }


def is_fresh_deep_dive_miss(entry, *, now=None):
    if not isinstance(entry, dict):
        return False
    miss = entry.get("deep_dive_miss")
    if not isinstance(miss, dict):
        return False
    if miss.get("schema_version") != DEEP_DIVE_MISS_SCHEMA_VERSION:
        return False
    if miss.get("policy_version") != DEEP_DIVE_POLICY_VERSION:
        return False
    if not miss.get("reason"):
        return False
    try:
        age = float(time.time() if now is None else now) - float(
            miss["attempted_at"]
        )
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= age < DEEP_DIVE_MISS_TTL_SECONDS

def _prune_cache(cache_data, *, now=None):
    # Validated scores are content-addressed facts. Evicting them by age or an
    # arbitrary capacity cap would violate the no-rescore contract and create
    # avoidable API charges. Time-bounded deep-dive failures are evaluated by
    # ``is_fresh_deep_dive_miss`` and do not require deleting the score entry.
    return 0, 0


def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
                
            expired, overflow = _prune_cache(cache_data)
            if expired:
                print(f"Pruned {expired} expired cache entries.", flush=True)
            if overflow:
                print(
                    f"Pruned {overflow} cache entries to maintain max size.",
                    flush=True,
                )
                
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
    expired, overflow = _prune_cache(cache_data, now=current_time)
    if expired or overflow:
        print(
            f"Pruned cache before save: expired={expired}, overflow={overflow}.",
            flush=True,
        )
            
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


def merge_verified_cache_entries(entries):
    """Atomically merge importer-verified scores into the content cache.

    Manual-review imports run after the interactive capture has paused.  A
    locked read/modify/write prevents a concurrent legitimate cache writer from
    being lost.  An exact semantic identity is immutable: conflicting scores
    fail closed rather than using whichever import happened last.
    """

    if not isinstance(entries, dict) or any(
        not isinstance(key, str)
        or not key
        or not isinstance(value, dict)
        or value.get("cache_key") != key
        for key, value in entries.items()
    ):
        raise ValueError("verified cache entries are malformed")
    target_path = os.path.abspath(CACHE_FILE)
    target_dir = os.path.dirname(target_path)
    os.makedirs(target_dir, exist_ok=True)
    lock_path = target_path + ".lock"
    with open(lock_path, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            if os.path.exists(target_path):
                try:
                    with open(target_path, "r", encoding="utf-8") as handle:
                        cache_data = json.load(handle)
                except (OSError, UnicodeError, json.JSONDecodeError) as error:
                    raise RuntimeError(
                        f"Cannot read score cache for verified merge: {error}"
                    ) from error
                if not isinstance(cache_data, dict):
                    raise RuntimeError("Score cache root must be a JSON object")
            else:
                cache_data = {}

            for key, entry in entries.items():
                if is_revoked_manual_score(entry):
                    raise RuntimeError("Cannot import revoked manual review score " + key)
                existing = cache_data.get(key)
                if isinstance(existing, dict) and existing.get(
                    "score_data"
                ) != entry.get("score_data") and not is_revoked_manual_score(existing):
                    raise RuntimeError(
                        "Conflicting validated scores for semantic cache key " + key
                    )
                cache_data[key] = entry

            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=target_dir,
                    prefix=f".{os.path.basename(target_path)}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary = handle.name
                    json.dump(
                        cache_data,
                        handle,
                        ensure_ascii=False,
                        indent=2,
                        allow_nan=False,
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target_path)
                temporary = None
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
