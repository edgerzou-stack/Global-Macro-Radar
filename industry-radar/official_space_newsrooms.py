"""Strict adapter for issuer-owned commercial-space updates.

SpaceX's public ``/updates/`` route is a client-rendered application and does
not expose an RSS feed or sitemap.  The application itself binds to the exact
``content.spacex.com`` API below.  This adapter accepts only that audited API,
derives only the public SpaceX update canonical, and emits a T1 candidate only
after the record has an exact date, stable identifier and explicit SpaceX
entity evidence.  Any contract drift fails closed.

NASA, FAA and third-party space reporting are intentionally out of scope: they
remain authorities for their own records and cannot inherit SpaceX's issuer
identity through this adapter.
"""

from __future__ import annotations

from datetime import datetime, time as datetime_time, timedelta, timezone
import json
import re
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import cloudscraper


ADAPTER_NAME = "official_space_newsroom"
SPACEX_SOURCE_ID = "spacex_updates"
SPACEX_UPDATES_API_URL = (
    "https://content.spacex.com/api/spacex-website/updates"
)
SPACEX_PUBLIC_HOST = "www.spacex.com"
SPACEX_CONTENT_HOST = "content.spacex.com"
SPACEX_CANONICAL_PREFIX = "https://www.spacex.com/updates/"
MAX_RECORDS = 128
MAX_FUTURE_SKEW = timedelta(minutes=5)
MAX_CONTENT_CHARS = 30000
_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?")
_DOCUMENT_ID = re.compile(r"[a-z0-9]{8,64}")
_ENTITY_PATTERN = re.compile(
    r"\b(?:space\s*x|spacex|starship|starlink|falcon(?:\s+(?:9|heavy))?|"
    r"dragon|raptor|super\s+heavy)\b",
    re.IGNORECASE,
)


class OfficialSpaceNewsroomError(RuntimeError):
    pass


def _clean_text(value):
    return " ".join(
        BeautifulSoup(str(value or ""), "html.parser").stripped_strings
    )


def _response_json(response):
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    elif int(getattr(response, "status_code", 200)) >= 400:
        raise OfficialSpaceNewsroomError(
            f"HTTP {getattr(response, 'status_code', 'unknown')}"
        )
    try:
        if hasattr(response, "json"):
            payload = response.json()
        else:
            payload = json.loads(str(getattr(response, "text", "") or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OfficialSpaceNewsroomError(
            "SpaceX updates endpoint did not return JSON"
        ) from error
    if not isinstance(payload, list):
        raise OfficialSpaceNewsroomError("SpaceX updates payload is not a list")
    if not payload or len(payload) > MAX_RECORDS:
        raise OfficialSpaceNewsroomError(
            f"SpaceX updates record count outside audited bounds: {len(payload)}"
        )
    return payload


def _strict_endpoint(value):
    parsed = urlsplit(str(value or "").strip())
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != SPACEX_CONTENT_HOST
        or parsed.path != "/api/spacex-website/updates"
        or parsed.query
        or parsed.fragment
    ):
        raise OfficialSpaceNewsroomError(
            f"SpaceX official endpoint does not match audited URL: {value!r}"
        )
    return parsed.geturl()


def _canonical_for_slug(value):
    slug = str(value or "").strip()
    if not _SLUG.fullmatch(slug):
        raise OfficialSpaceNewsroomError("invalid SpaceX updateId")
    canonical = f"{SPACEX_CANONICAL_PREFIX}{slug}/"
    parsed = urlsplit(canonical)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != SPACEX_PUBLIC_HOST
        or parsed.path != f"/updates/{slug}/"
        or parsed.query
        or parsed.fragment
    ):
        raise OfficialSpaceNewsroomError("invalid SpaceX public canonical")
    return canonical


def _content_text(blocks):
    if not isinstance(blocks, list) or not blocks:
        raise OfficialSpaceNewsroomError("SpaceX update lacks contentBlocks")
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for field in ("heading", "paragraph"):
            value = _clean_text(block.get(field))
            if value:
                parts.append(value)
        list_items = block.get("listItems")
        if isinstance(list_items, list):
            for item in list_items:
                if isinstance(item, dict):
                    item = item.get("text") or item.get("content")
                value = _clean_text(item)
                if value:
                    parts.append(value)
    content = " ".join(parts)
    if not content:
        raise OfficialSpaceNewsroomError("SpaceX update lacks textual content")
    return content[:MAX_CONTENT_CHARS]


def _parse_record(record):
    if not isinstance(record, dict):
        raise OfficialSpaceNewsroomError("SpaceX update is not an object")
    record_id = record.get("id")
    if isinstance(record_id, bool) or not isinstance(record_id, int) or record_id < 1:
        raise OfficialSpaceNewsroomError("SpaceX update lacks stable numeric id")
    document_id = str(record.get("documentId") or "").strip()
    if not _DOCUMENT_ID.fullmatch(document_id):
        raise OfficialSpaceNewsroomError("SpaceX update lacks stable documentId")
    if record.get("link") not in (None, ""):
        # External-link tiles are not canonical issuer update records.
        raise OfficialSpaceNewsroomError("SpaceX update redirects off canonical route")

    canonical = _canonical_for_slug(record.get("updateId"))
    title = _clean_text(record.get("title"))
    if not title:
        raise OfficialSpaceNewsroomError("SpaceX update lacks title")
    try:
        published_date = datetime.strptime(
            str(record.get("date") or ""), "%Y-%m-%d"
        ).date()
    except ValueError as error:
        raise OfficialSpaceNewsroomError(
            "SpaceX update publication date is not exact YYYY-MM-DD"
        ) from error
    published = datetime.combine(
        published_date,
        datetime_time.min,
        tzinfo=ZoneInfo("America/Los_Angeles"),
    ).astimezone(timezone.utc)
    content = _content_text(record.get("contentBlocks"))
    if not _ENTITY_PATTERN.search(f"{title} {content}"):
        raise OfficialSpaceNewsroomError(
            "SpaceX update lacks explicit issuer/product entity evidence"
        )
    return {
        "title": title,
        "link": canonical,
        "summary": content[:600],
        "content": content,
        "source": "SpaceX Updates",
        "feed_url": SPACEX_UPDATES_API_URL,
        "published_at": published.isoformat(),
        "reference_urls": [],
        "canonical_verification": "official_api_update_id",
        "official_document_id": document_id,
    }


def _health(url):
    return {
        "url": url,
        "status": "healthy",
        "fresh": False,
        "fresh_entries": 0,
        "total_entries": 0,
        "quarantined_entries": 0,
        "newest_published_at": None,
        "error": "",
        "latency_ms": 0.0,
        "attempts": 1,
        "content_type": "application/json",
    }


def fetch_official_space_newsrooms(
    entries,
    *,
    hours_back,
    now,
    session=None,
    request_timeout=15,
):
    """Fetch exact audited commercial-space sources in registry order."""
    now = now.astimezone(timezone.utc)
    session = session or cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "mobile": False}
    )
    if hasattr(session, "headers"):
        session.headers.update(
            {
                "Accept": "application/json",
                "Referer": "https://www.spacex.com/updates/",
            }
        )

    articles = []
    health_rows = []
    for entry in entries:
        source_id = str(entry.get("id") or "")
        endpoint = str(entry.get("url") or "")
        started = time.perf_counter()
        health = _health(endpoint)
        try:
            if source_id != SPACEX_SOURCE_ID:
                raise OfficialSpaceNewsroomError(
                    f"unsupported official space source: {source_id!r}"
                )
            _strict_endpoint(endpoint)
            payload = _response_json(
                session.get(endpoint, timeout=request_timeout)
            )
            health["total_entries"] = len(payload)
            parsed = []
            quarantine = []
            seen_canonicals = set()
            for record in payload:
                try:
                    article = _parse_record(record)
                    if article["link"] in seen_canonicals:
                        raise OfficialSpaceNewsroomError(
                            "duplicate SpaceX public canonical"
                        )
                    seen_canonicals.add(article["link"])
                    parsed.append(article)
                except Exception as error:
                    quarantine.append(str(error))

            if not parsed:
                raise OfficialSpaceNewsroomError(
                    "SpaceX endpoint contained no verified issuer updates"
                )
            newest = max(
                datetime.fromisoformat(item["published_at"]) for item in parsed
            )
            health["newest_published_at"] = newest.isoformat()
            cutoff = now - timedelta(hours=hours_back)
            recent = []
            for article in parsed:
                published = datetime.fromisoformat(article["published_at"])
                if published > now + MAX_FUTURE_SKEW:
                    quarantine.append(f"{article['link']}: future publication time")
                elif published >= cutoff:
                    recent.append(article)
            health["fresh_entries"] = len(recent)
            health["fresh"] = bool(recent)
            health["quarantined_entries"] = len(quarantine)
            reasons = []
            if quarantine:
                reasons.append(f"{len(quarantine)} entries quarantined")
            if not recent:
                reasons.append("no fresh entries")
            if reasons:
                health["status"] = "degraded"
                health["error"] = "; ".join(reasons)
            articles.extend(recent)
        except Exception as error:
            health["status"] = "failed"
            health["error"] = str(error)
        finally:
            health["latency_ms"] = round(
                (time.perf_counter() - started) * 1000, 3
            )
        health_rows.append(health)

    articles.sort(
        key=lambda item: (item.get("published_at", ""), item.get("link", "")),
        reverse=True,
    )
    return articles, health_rows
