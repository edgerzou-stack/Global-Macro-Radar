"""Capture bounded official FDA bodies before scoring; frozen replay never fetches."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import re
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from dateutil import parser as date_parser
import requests

FDA_FEED = "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml"
FDA_ARTICLE = re.compile(r"https://www\.fda\.gov/news-events/press-announcements/[a-z0-9-]+\Z")


def capture_official_bodies(articles, registry, *, now, session=None):
    result = [dict(article) for article in articles]
    candidates = []
    for index, article in enumerate(result):
        feed = article.get("feed_url")
        if feed != FDA_FEED or registry.get(feed, {}).get("id") != "fda_press_releases":
            continue
        if not FDA_ARTICLE.fullmatch(str(article.get("link", ""))):
            article["official_body_capture"] = {"status": "unavailable", "reason": "unaudited_article_url"}
            continue
        candidates.append(index)
    # Bound latency and resource use, without silently claiming complete capture.
    candidates.sort(key=lambda index: str(result[index].get("link", "")))
    for index in candidates[12:]:
        result[index]["official_body_capture"] = {"status": "unavailable", "reason": "capture_budget_exhausted"}

    def capture(index):
        article = result[index]
        url = article["link"]
        try:
            published = date_parser.isoparse(article["published_at"])
            if published.tzinfo is None or not now - timedelta(hours=48) <= published <= now:
                raise ValueError("publication_outside_capture_window")
            response = (session or requests).get(
                url, timeout=10, allow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
            if response.status_code != 200 or response.url != url:
                raise ValueError("response_status_or_url_mismatch")
            if len(response.text) > 2_000_000:
                raise ValueError("response_too_large")
            soup = BeautifulSoup(response.text, "html.parser")
            title = soup.find("h1")
            clean = lambda value: " ".join(value.split()).casefold()
            if title is None or clean(title.get_text(" ", strip=True)) != clean(article["title"]):
                raise ValueError("article_title_mismatch")
            canonical = soup.find("link", rel="canonical")
            if canonical is None or canonical.get("href") != url:
                raise ValueError("article_canonical_mismatch")
            date_node = soup.select_one(".field--name-field-publish-date")
            date_meta = soup.find("meta", attrs={"name": "DC.date.issued"})
            date_text = date_node.get_text(" ", strip=True) if date_node else (date_meta.get("content", "") if date_meta else "")
            main = soup.select_one("article#main-content > div.col-md-8")
            if main is not None:
                release = main.select_one(".inset-column dl.lcds-description-list--grid")
                label = release.select_one("dt.cell-1_1") if release else None
                published_node = release.select_one("dd.cell-2_1 time") if release else None
                if label and label.get_text(" ", strip=True) == "For Immediate Release:" and published_node:
                    date_text = published_node.get_text(" ", strip=True)
            # Never let dateutil invent an omitted year/month/day.
            if not re.fullmatch(r"(?:[A-Za-z]+\s+\d{1,2},?\s+20\d{2}|20\d{2}-\d{2}-\d{2}(?:T\S+)?)", date_text.strip()):
                raise ValueError("article_publication_date_missing")
            page_date = date_parser.parse(date_text, fuzzy=False).date()
            if page_date != published.astimezone(ZoneInfo("America/New_York")).date():
                raise ValueError("article_publication_date_mismatch")
            body_node = soup.select_one(".field--name-body")
            if main is not None:
                # Current FDA layout: body precedes the first horizontal rule;
                # subsequent media contacts and site boilerplate are not evidence.
                parts = []
                for node in main.find_all(recursive=False):
                    if node.name == "hr":
                        break
                    if node.name in {"p", "h2", "h3", "ul", "ol", "table", "blockquote"}:
                        parts.append(node.get_text(" ", strip=True))
                body = " ".join(" ".join(parts).split())
            elif body_node is None:
                raise ValueError("article_body_missing")
            else:
                for node in body_node.select("script, style, nav, form"):
                    node.decompose()
                body = " ".join(body_node.get_text(" ", strip=True).split())
            if not 100 <= len(body) <= 30000:
                raise ValueError("article_body_size_invalid")
            article["content"] = body
            article["official_body_capture"] = {
                "status": "captured", "url": url,
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "publication_date": page_date.isoformat(),
            }
        except Exception as exc:
            article["official_body_capture"] = {"status": "unavailable", "reason": str(exc)}

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(capture, candidates[:12]))
    return result
