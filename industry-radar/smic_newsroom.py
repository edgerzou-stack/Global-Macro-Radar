"""Bounded issuer-bound SMIC capture, with index/detail identity checks."""
from datetime import datetime, timedelta, timezone
import re
import time
from urllib.parse import urljoin
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup

INDEX_URL = "https://www.smics.com/en/site/news"
DETAIL_URL = re.compile(r"https://www\.smics\.com/en/site/news_read/\d+\Z")


def fetch_smic_newsroom(entry, *, now, hours_back, session, timeout):
    started = time.perf_counter()
    if entry.get("url") != INDEX_URL:
        raise ValueError("SMIC discovery URL is not audited")
    health = dict(url=INDEX_URL, status="healthy", fresh=False, fresh_entries=0,
                  total_entries=0, quarantined_entries=0, newest_published_at=None,
                  error="", latency_ms=0.0, attempts=1, content_type="text/html")
    articles = []

    def fetch(url):
        response = session.get(url, timeout=timeout, allow_redirects=False)
        if response.status_code != 200 or response.url != url:
            raise ValueError("SMIC response status/URL mismatch")
        if len(response.text) > 2_000_000:
            raise ValueError("SMIC response exceeds capture bound")
        return BeautifulSoup(response.text, "html.parser")

    def text(node):
        return " ".join(node.get_text(" ", strip=True).split()) if node else ""

    try:
        soup = fetch(INDEX_URL)
        seen = set()
        candidates = []
        for row in soup.select("tr"):
            anchor = row.select_one("td.title p.t a")
            date_text = text(row.select_one("td.date p"))
            if not anchor or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
                continue
            url = urljoin(INDEX_URL, anchor.get("href", ""))
            if not DETAIL_URL.fullmatch(url) or url in seen:
                continue
            seen.add(url)
            published = datetime.strptime(date_text, "%Y-%m-%d").replace(
                tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc)
            health["total_entries"] += 1
            if published > now:
                health["quarantined_entries"] += 1
                continue
            if not health["newest_published_at"] or published.isoformat() > health["newest_published_at"]:
                health["newest_published_at"] = published.isoformat()
            if published >= now - timedelta(hours=hours_back):
                candidates.append((published, url, text(anchor), date_text))
        if not seen:
            raise ValueError("SMIC index has no auditable article inventory")
        candidates.sort(reverse=True)
        health["quarantined_entries"] += max(0, len(candidates) - 20)
        for published, url, title, date_text in candidates[:20]:
            try:
                detail = fetch(url)
                if text(detail.select_one(".new_read .sec_title p")) != title:
                    raise ValueError("SMIC index/detail title mismatch")
                if text(detail.select_one(".new_read .container > .date p")) != date_text:
                    raise ValueError("SMIC index/detail date mismatch")
                body = text(detail.select_one(".new_read .container > .content"))
                if len(body) < 30:
                    raise ValueError("SMIC article body missing")
                # Do not repair contradictory issuer datelines for trading.
                dates = re.findall(r"Shanghai,?\s+China\s*[-–—]\s*(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+(20\d{2})\b", body[:500], re.IGNORECASE)
                if dates and any(year != date_text[:4] for year in dates):
                    raise ValueError("SMIC body dateline conflicts with page year")
                articles.append(dict(title=title, link=url, content=body[:30000],
                                     summary=body[:1000], source="SMIC Newsroom",
                                     feed_url=INDEX_URL, published_at=published.isoformat(),
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
    health["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return articles, health
