"""Capture bounded official bodies before scoring; frozen replay never fetches."""
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
APPLE_FEED = "https://www.apple.com/newsroom/rss-feed.rss"
APPLE_ARTICLE = re.compile(
    r"https://www\.apple\.com/newsroom/20\d{2}/(?:0[1-9]|1[0-2])/[a-z0-9-]+/\Z"
)
NVIDIA_FEED = "https://nvidianews.nvidia.com/releases.xml"
NVIDIA_ARTICLE = re.compile(r"https://nvidianews\.nvidia\.com/news/[a-z0-9-]+\Z")

_HEADER_DATE = re.compile(r"[A-Z][a-z]+ \d{1,2}, 20\d\d")


def _clean_title(value):
    # Newsroom headlines typeset the non-breaking hyphen (U+2011/U+2010)
    # where feed titles carry an ASCII hyphen; normalize both sides.
    return " ".join(value.replace("‑", "-").replace("‐", "-").split()).casefold()


def _verify_fda(soup, article):
    title = soup.find("h1")
    if title is None or _clean_title(title.get_text(" ", strip=True)) != _clean_title(article["title"]):
        raise ValueError("article_title_mismatch")
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
    return page_date, body


def _verify_apple(soup, article):
    header = soup.select_one(".article-header")
    headline = header.find("h1") if header is not None else None
    if headline is None or _clean_title(headline.get_text(" ", strip=True)) != _clean_title(article["title"]):
        raise ValueError("article_title_mismatch")
    date_text = ""
    if header is not None:
        eyebrow = header.select_one(".category-eyebrow__date")
        if eyebrow is not None:
            candidate = eyebrow.get_text(" ", strip=True)
            if _HEADER_DATE.fullmatch(candidate):
                date_text = candidate
    if not date_text:
        raise ValueError("article_publication_date_missing")
    page_date = date_parser.parse(date_text, fuzzy=False).date()
    parts = [
        " ".join(node.get_text(" ", strip=True).split())
        for node in soup.select(".pagebody-copy")
    ]
    body = " ".join(" ".join(parts).split())
    return page_date, body


def _verify_nvidia(soup, article):
    headline = soup.select_one("h1.article-title")
    if headline is None or _clean_title(headline.get_text(" ", strip=True)) != _clean_title(article["title"]):
        raise ValueError("article_title_mismatch")
    body_node = soup.select_one(".article-body")
    if body_node is None:
        raise ValueError("article_body_missing")
    body = " ".join(
        node.get_text(" ", strip=True)
        for node in body_node.find_all(["p", "h2", "h3", "ul", "ol", "blockquote"])
    )
    body = " ".join(body.split())
    container = headline.find_parent("div", class_="article")
    if container is not None:
        body_node.extract()
        header_text = container.get_text(" ", strip=True)
    else:
        header_text = headline.get_text(" ", strip=True)
    match = _HEADER_DATE.search(header_text)
    if match is None:
        raise ValueError("article_publication_date_missing")
    page_date = date_parser.parse(match.group(0), fuzzy=False).date()
    return page_date, body


class _CaptureSite:
    def __init__(self, source_id, article_pattern, timezone, verify):
        self.source_id = source_id
        self.article_pattern = article_pattern
        self.timezone = timezone
        self.verify = verify


_CAPTURE_SITES = {
    FDA_FEED: _CaptureSite(
        "fda_press_releases", FDA_ARTICLE, ZoneInfo("America/New_York"), _verify_fda
    ),
    APPLE_FEED: _CaptureSite(
        "apple_newsroom", APPLE_ARTICLE, ZoneInfo("America/Los_Angeles"), _verify_apple
    ),
    NVIDIA_FEED: _CaptureSite(
        "nvidia_newsroom", NVIDIA_ARTICLE, ZoneInfo("America/Los_Angeles"), _verify_nvidia
    ),
}


def capture_official_bodies(articles, registry, *, now, session=None):
    result = [dict(article) for article in articles]
    candidates = []
    for index, article in enumerate(result):
        site = _CAPTURE_SITES.get(article.get("feed_url"))
        if site is None or registry.get(article.get("feed_url"), {}).get("id") != site.source_id:
            continue
        if not site.article_pattern.fullmatch(str(article.get("link", ""))):
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
        site = _CAPTURE_SITES[article["feed_url"]]
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
            canonical = soup.find("link", rel="canonical")
            if canonical is None or canonical.get("href") != url:
                raise ValueError("article_canonical_mismatch")
            page_date, body = site.verify(soup, article)
            if page_date != published.astimezone(site.timezone).date():
                raise ValueError("article_publication_date_mismatch")
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
