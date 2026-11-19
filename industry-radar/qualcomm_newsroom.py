"""Bounded issuer-bound Qualcomm capture from the audited public sitemap.

The Qualcomm newsroom is a client-rendered React application without a feed;
the issuer-published sitemap with per-URL ``lastmod`` values is the only
stable server-rendered inventory. Detail pages server-render only the
headline, so captured review material is title-grade and is never padded
with fabricated body text. A detail headline must agree with the sitemap
slug on at least three normalized tokens before an article is accepted.
"""
from datetime import date, datetime, timedelta, timezone
import re
import time

SITEMAP_URL = "https://www.qualcomm.com/sitemap.xml"
DETAIL_URL = re.compile(r"https://www\.qualcomm\.com/news/releases/\d{4}/\d{2}/[a-z0-9-]+\Z")
TITLE_TAG = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE)
SITEMAP_ENTRY = re.compile(
    r"<url>\s*<loc>([^<]+)</loc>\s*<lastmod>(\d{4}-\d{2}-\d{2})", re.IGNORECASE
)
STOP_TOKENS = {"and", "the", "of", "for", "a", "an", "to", "in", "on", "with", "new"}
MAX_DETAIL_ARTICLES = 20


def _tokens(value):
    return {
        token
        for token in re.split(r"[^a-z0-9]+", value.lower())
        if len(token) > 2 and token not in STOP_TOKENS
    }


def fetch_qualcomm_newsroom(entry, *, now, hours_back, session, timeout):
    started = time.perf_counter()
    if entry.get("url") != SITEMAP_URL:
        raise ValueError("Qualcomm discovery URL is not audited")
    health = dict(url=SITEMAP_URL, status="healthy", fresh=False, fresh_entries=0,
                  total_entries=0, quarantined_entries=0, newest_published_at=None,
                  error="", latency_ms=0.0, attempts=1, content_type="application/xml")
    articles = []

    try:
        response = session.get(SITEMAP_URL, timeout=timeout, allow_redirects=False)
        if response.status_code != 200 or getattr(response, "url", SITEMAP_URL) != SITEMAP_URL:
            raise ValueError("Qualcomm sitemap status/URL mismatch")
        text = response.text
        if len(text) > 8_000_000:
            raise ValueError("Qualcomm sitemap exceeds capture bound")

        candidates = []
        for loc, lastmod in SITEMAP_ENTRY.findall(text):
            if not DETAIL_URL.fullmatch(loc):
                continue
            health["total_entries"] += 1
            try:
                published = date.fromisoformat(lastmod)
            except ValueError:
                health["quarantined_entries"] += 1
                continue
            published_at = datetime(published.year, published.month, published.day,
                                    tzinfo=timezone.utc)
            if published_at > now:
                health["quarantined_entries"] += 1
                continue
            if not health["newest_published_at"] or published_at.isoformat() > health["newest_published_at"]:
                health["newest_published_at"] = published_at.isoformat()
            if published_at >= now - timedelta(hours=hours_back):
                candidates.append((published_at, loc))
        if not health["total_entries"]:
            raise ValueError("Qualcomm sitemap has no auditable press-release inventory")
        candidates.sort(reverse=True)
        health["quarantined_entries"] += max(0, len(candidates) - MAX_DETAIL_ARTICLES)
        for published_at, loc in candidates[:MAX_DETAIL_ARTICLES]:
            try:
                detail = session.get(loc, timeout=timeout, allow_redirects=False)
                if detail.status_code != 200 or getattr(detail, "url", loc) != loc:
                    raise ValueError("Qualcomm detail status/URL mismatch")
                if len(detail.text) > 2_000_000:
                    raise ValueError("Qualcomm detail exceeds capture bound")
                match = TITLE_TAG.search(detail.text)
                if not match:
                    raise ValueError("Qualcomm headline missing")
                title = " ".join(match.group(1).split())
                suffix = title.rsplit("|", 1)
                if len(suffix) == 2 and suffix[1].strip().lower() == "qualcomm":
                    title = suffix[0].strip()
                if len(title) < 15:
                    raise ValueError("Qualcomm headline too short")
                slug = loc.rsplit("/", 1)[1]
                overlap = _tokens(slug) & _tokens(title)
                if len(overlap) < 3:
                    raise ValueError("Qualcomm headline does not bind sitemap slug")
                articles.append(dict(title=title, link=loc, content="",
                                     summary=title, source="Qualcomm Newsroom",
                                     feed_url=SITEMAP_URL,
                                     published_at=published_at.isoformat(),
                                     reference_urls=[]))
            except Exception as exc:
                health["quarantined_entries"] += 1
                health["error"] = str(exc)
        health["fresh_entries"] = len(articles)
        health["fresh"] = bool(articles)
        if health["quarantined_entries"]:
            health["status"] = "degraded"
    except Exception as exc:
        health["status"] = "failed"
        health["error"] = str(exc)
    health["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return articles, health
