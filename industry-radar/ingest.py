import calendar
import concurrent.futures
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import cloudscraper
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as date_parser


logger = logging.getLogger(__name__)

ALLOWED_FEED_CONTENT_TYPES = ("rss", "atom", "xml")
MAX_FUTURE_SKEW = timedelta(minutes=5)
RSS_FIXTURE_SCHEMA_VERSION = 1


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _entry_datetime(entry):
    for field in ("published_parsed", "updated_parsed"):
        parsed = _field(entry, field)
        if parsed:
            try:
                return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                pass

    for field in ("published", "updated"):
        raw = _field(entry, field)
        if not raw:
            continue
        try:
            return _as_utc(date_parser.parse(raw))
        except (TypeError, ValueError, OverflowError) as exc:
            logger.warning("Date parse error for %r: %s", raw, exc)
    return None


def _source_title(parsed_feed, fallback):
    feed_meta = _field(parsed_feed, "feed", {})
    return _field(feed_meta, "title", fallback) or fallback


def _clean_html(value, separator=" "):
    if not value:
        return ""
    return BeautifulSoup(str(value), "html.parser").get_text(
        separator=separator, strip=True
    )


def _validate_content_type(response):
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("Content-Type", "")).lower()
    if content_type and not any(token in content_type for token in ALLOWED_FEED_CONTENT_TYPES):
        raise ValueError(f"Unexpected feed content type: {content_type}")
    return content_type


def _fixture_timestamp(value, field_name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty ISO timestamp")
    try:
        parsed = date_parser.isoparse(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be a valid ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def load_rss_fixture(path):
    """Load a deterministic, already-ingested RSS snapshot without networking."""
    fixture_path = os.path.abspath(os.fspath(path))
    try:
        with open(fixture_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load RSS fixture {fixture_path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("RSS fixture must be a JSON object")
    if payload.get("schema_version") != RSS_FIXTURE_SCHEMA_VERSION:
        raise ValueError("Unsupported RSS fixture schema_version")
    raw_articles = payload.get("articles")
    raw_health = payload.get("health")
    if not isinstance(raw_articles, list) or not isinstance(raw_health, list):
        raise ValueError("RSS fixture articles and health must be lists")

    articles = []
    for index, raw in enumerate(raw_articles):
        if not isinstance(raw, dict):
            raise ValueError(f"RSS fixture article {index} must be an object")
        article = dict(raw)
        for field in ("title", "link", "source"):
            if not isinstance(article.get(field), str) or not article[field].strip():
                raise ValueError(
                    f"RSS fixture article {index} has invalid {field}"
                )
        for field in ("summary", "content"):
            value = article.get(field, "")
            if not isinstance(value, str):
                raise ValueError(
                    f"RSS fixture article {index} has invalid {field}"
                )
            article[field] = value
        article["published_at"] = _fixture_timestamp(
            article.get("published_at"), f"articles[{index}].published_at"
        )
        articles.append(article)

    health = []
    seen_urls = set()
    for index, raw in enumerate(raw_health):
        if not isinstance(raw, dict):
            raise ValueError(f"RSS fixture health {index} must be an object")
        item = dict(raw)
        url = item.get("url")
        if not isinstance(url, str) or not url.strip() or url in seen_urls:
            raise ValueError(f"RSS fixture health {index} has invalid/duplicate url")
        seen_urls.add(url)
        if item.get("status") not in {"healthy", "degraded", "failed"}:
            raise ValueError(f"RSS fixture health {index} has invalid status")
        for field in ("fresh_entries", "total_entries", "quarantined_entries"):
            value = item.get(field, 0)
            if type(value) is not int or value < 0:
                raise ValueError(f"RSS fixture health {index} has invalid {field}")
            item[field] = value
        if item["fresh_entries"] > item["total_entries"]:
            raise ValueError(
                f"RSS fixture health {index} fresh_entries exceeds total_entries"
            )
        expected_fresh = item["fresh_entries"] > 0
        if "fresh" in item and item["fresh"] is not expected_fresh:
            raise ValueError(f"RSS fixture health {index} has inconsistent fresh")
        item["fresh"] = expected_fresh
        if item["status"] == "failed" and expected_fresh:
            raise ValueError(f"RSS fixture health {index} failed but has fresh entries")
        newest = item.get("newest_published_at")
        if newest is not None:
            item["newest_published_at"] = _fixture_timestamp(
                newest, f"health[{index}].newest_published_at"
            )
        health.append(item)

    fresh_total = sum(item["fresh_entries"] for item in health)
    if fresh_total != len(articles):
        raise ValueError(
            "RSS fixture article count does not match health fresh_entries total"
        )
    articles.sort(
        key=lambda item: (item.get("published_at", ""), item.get("link", "")),
        reverse=True,
    )
    return articles, health


def fetch_rss_feeds(
    feeds,
    hours_back=168,
    *,
    now=None,
    return_health=False,
    request_timeout=15,
    max_workers=10,
):
    """Fetch recent RSS articles and optionally return per-source health state.

    Timestamps are normalized to UTC. Entries without a trustworthy timestamp are
    quarantined rather than being presented as newly published.
    """
    now_utc = _as_utc(now or datetime.now(timezone.utc))
    time_limit = now_utc - timedelta(hours=hours_back)

    def fetch_single_feed(feed_url):
        started = time.perf_counter()
        local_articles = []
        health = {
            "url": feed_url,
            "status": "failed",
            "fresh": False,
            "fresh_entries": 0,
            "total_entries": 0,
            "quarantined_entries": 0,
            "newest_published_at": None,
            "bozo": False,
            "content_type": "",
            "error": "",
            "latency_ms": 0.0,
        }
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            }
            scraper = cloudscraper.create_scraper()
            response = scraper.get(feed_url, headers=headers, timeout=request_timeout)
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            elif getattr(response, "status_code", 200) >= 400:
                raise RuntimeError(f"HTTP {response.status_code}")
            health["content_type"] = _validate_content_type(response)

            parsed_feed = feedparser.parse(response.content)
            entries = list(_field(parsed_feed, "entries", []) or [])
            health["total_entries"] = len(entries)
            health["bozo"] = bool(_field(parsed_feed, "bozo", False))
            source_name = _source_title(parsed_feed, feed_url)
            newest = None

            if not entries:
                health["error"] = "Feed contains no entries"
                print(f"  ✗ {feed_url}: no entries", flush=True)
                health["latency_ms"] = round(
                    (time.perf_counter() - started) * 1000.0, 3
                )
                return local_articles, health

            for entry in entries:
                pub_date = _entry_datetime(entry)
                if pub_date is None or pub_date > now_utc + MAX_FUTURE_SKEW:
                    health["quarantined_entries"] += 1
                    continue

                newest = pub_date if newest is None else max(newest, pub_date)
                if pub_date < time_limit:
                    continue

                content_items = _field(entry, "content", []) or []
                content = " ".join(
                    str(_field(item, "value", "")) for item in content_items
                )
                raw_title = _field(entry, "title", "")
                raw_summary = _field(entry, "summary", "")
                local_articles.append(
                    {
                        "title": _clean_html(raw_title, separator="") or "No Title",
                        "link": _field(entry, "link", "") or "",
                        "summary": _clean_html(raw_summary),
                        "content": _clean_html(content),
                        "source": source_name,
                        "published_at": pub_date.isoformat(),
                    }
                )

            health["fresh_entries"] = len(local_articles)
            health["fresh"] = bool(local_articles)
            health["newest_published_at"] = newest.isoformat() if newest else None
            degraded_reasons = []
            if health["bozo"]:
                degraded_reasons.append(
                    f"bozo feed: {_field(parsed_feed, 'bozo_exception', 'parse error')}"
                )
            if health["quarantined_entries"]:
                degraded_reasons.append(
                    f"{health['quarantined_entries']} entries quarantined"
                )
            if not local_articles:
                degraded_reasons.append("no fresh entries")

            health["status"] = "degraded" if degraded_reasons else "healthy"
            health["error"] = "; ".join(degraded_reasons)
            marker = "⚠" if health["status"] == "degraded" else "✓"
            print(
                f"  {marker} {source_name}: {len(local_articles)} fresh articles "
                f"({health['status']})",
                flush=True,
            )
        except Exception as exc:
            health["status"] = "failed"
            health["error"] = str(exc)
            print(f"  ✗ {feed_url}: {exc}", flush=True)
        health["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        return local_articles, health

    print(f"Fetching {len(feeds)} RSS feeds in parallel...")
    articles = []
    health_by_index = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_single_feed, url): index
            for index, url in enumerate(feeds)
        }
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            local_articles, health = future.result()
            articles.extend(local_articles)
            health_by_index[index] = health

    articles.sort(
        key=lambda item: (item.get("published_at", ""), item.get("link", "")),
        reverse=True,
    )
    health_results = [health_by_index[index] for index in range(len(feeds))]
    print(f"Total: {len(articles)} articles fetched.")
    if return_health:
        return articles, health_results
    return articles


if __name__ == "__main__":
    feeds = ["https://techcrunch.com/feed/"]
    print(fetch_rss_feeds(feeds, hours_back=168, return_health=True))
