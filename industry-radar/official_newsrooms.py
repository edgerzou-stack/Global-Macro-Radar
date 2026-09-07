"""Strict sitemap adapters for audited issuer-owned pharmaceutical newsrooms.

This is deliberately not a generic HTML adapter.  Each supported source has a
fixed sitemap URL, canonical host and article path contract.  Sitemap entries
only discover candidates; an article is emitted after its issuer-owned page
passes canonical, headline and publication-date verification.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as datetime_time, timedelta, timezone
import json
import time
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import cloudscraper
from dateutil import parser as date_parser


ADAPTER_NAME = "official_newsroom"
MAX_FUTURE_SKEW = timedelta(minutes=5)
MAX_SITEMAP_CANDIDATES = 64

MODERNA_SITEMAP_URL = "https://news.modernatx.com/sitemap.xml"
MODERNA_HOMEPAGE_URL = "https://news.modernatx.com/"
MODERNA_IR_URL = "https://investors.modernatx.com/"
MODERNA_CORPORATE_NEWSROOM_URL = (
    "https://www.modernatx.com/newsroom/news-and-media"
)
MODERNA_EMBEDDED_FEED_URL = (
    "https://feeds.issuerdirect.com/data/custom_pr_feed.json"
)
MODERNA_CANONICAL_HOST = "news.modernatx.com"
# Merck's former item sitemap rewrites every historical ``lastmod`` during a
# bulk WordPress rebuild.  That made thousands of decade-old releases appear
# fresh and tripped the bounded-candidate guard.  The official news index has
# issuer-published dates and stable canonical links, so discovery uses it.
MERCK_SITEMAP_URL = "https://www.merck.com/media/news/"
MERCK_CANONICAL_HOST = "www.merck.com"

SUPPORTED_SOURCE_IDS = frozenset({"moderna_newsroom", "merck_newsroom"})


class OfficialNewsroomError(RuntimeError):
    pass


def _response_text(response):
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    elif int(getattr(response, "status_code", 200)) >= 400:
        raise OfficialNewsroomError(
            f"HTTP {getattr(response, 'status_code', 'unknown')}"
        )
    return str(getattr(response, "text", "") or "")


def _require_exact_final_url(response, expected_url, *, label):
    final_url = str(getattr(response, "url", "") or "").strip()
    if final_url != expected_url:
        raise OfficialNewsroomError(
            f"{label} redirected to an untrusted URL: {final_url!r}"
        )


def _require_article_final_url(response, expected_url, *, source_id):
    final_url = str(getattr(response, "url", "") or "").strip()
    try:
        trusted_final = _strict_article_url(final_url, source_id=source_id)
    except OfficialNewsroomError as error:
        raise OfficialNewsroomError(
            f"{source_id} article redirected to an untrusted URL: "
            f"{final_url!r}"
        ) from error
    if trusted_final.rstrip("/") != expected_url.rstrip("/"):
        raise OfficialNewsroomError(
            f"{source_id} article redirect changed canonical path"
        )


def _clean_text(value):
    return " ".join(BeautifulSoup(str(value or ""), "html.parser").stripped_strings)


def _normalized_title(value):
    return " ".join(_clean_text(value).split()).casefold()


def _canonical_url(soup):
    node = soup.find("link", rel=lambda value: value and "canonical" in value)
    return str(node.get("href") or "").strip() if node else ""


def _strict_article_url(value, *, source_id):
    parsed = urlsplit(str(value or "").strip())
    try:
        port = parsed.port
    except ValueError as error:
        raise OfficialNewsroomError(
            f"untrusted article URL: {value!r}"
        ) from error
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise OfficialNewsroomError(f"untrusted article URL: {value!r}")
    if source_id == "moderna_newsroom":
        # Moderna's current newsroom publishes new releases at one root-level
        # slug.  Reject its blog, statement and legacy archive paths.
        path_ok = parsed.path not in {"", "/"} and parsed.path.strip("/").count("/") == 0
        expected_host = MODERNA_CANONICAL_HOST
    elif source_id == "merck_newsroom":
        path_ok = parsed.path.startswith("/news/") and parsed.path != "/news/"
        expected_host = MERCK_CANONICAL_HOST
    else:
        raise OfficialNewsroomError(f"unsupported source_id: {source_id!r}")
    if host != expected_host or not path_ok:
        raise OfficialNewsroomError(f"untrusted canonical article URL: {value!r}")
    return parsed.geturl()


def _merck_index_candidates(text, *, cutoff_date):
    soup = BeautifulSoup(text, "html.parser")
    candidates = []
    seen = set()
    rows = soup.select(".d8-results-container .d8-result-item")
    for row in rows:
        link = row.select_one(".d8-result-item-headline a[href]")
        date_node = row.select_one(".d8-result-item-date")
        if link is None or date_node is None:
            continue
        try:
            url = _strict_article_url(link.get("href"), source_id="merck_newsroom")
            published_date = date_parser.parse(
                _clean_text(date_node.get_text(" ", strip=True)), fuzzy=False
            ).date()
        except (OfficialNewsroomError, TypeError, ValueError, OverflowError):
            continue
        if url in seen:
            continue
        seen.add(url)
        if published_date >= cutoff_date:
            candidates.append(
                (
                    url,
                    datetime.combine(
                        published_date,
                        datetime_time.min,
                        tzinfo=ZoneInfo("America/New_York"),
                    ),
                )
            )
    if not rows:
        raise OfficialNewsroomError("Merck news index lacks audited result rows")
    if len(candidates) > MAX_SITEMAP_CANDIDATES:
        raise OfficialNewsroomError(
            f"news index candidate limit exceeded: {len(candidates)} > "
            f"{MAX_SITEMAP_CANDIDATES}"
        )
    return candidates, len(seen)


def _json_ld_objects(soup):
    for node in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(node.string or node.get_text() or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        pending = [payload]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                yield item
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)


def _sitemap_candidates(text, *, source_id, cutoff_date):
    soup = BeautifulSoup(text, "xml")
    candidates = []
    total_entries = 0
    for node in soup.find_all("url"):
        loc = node.find("loc")
        lastmod = node.find("lastmod")
        if not loc or not lastmod:
            continue
        # Moderna publishes a whole-site sitemap (including its homepage and
        # non-release sections).  Those are discovery noise, not adapter
        # failures; only URLs satisfying the issuer-specific article contract
        # may advance to page verification.
        try:
            url = _strict_article_url(loc.get_text(strip=True), source_id=source_id)
        except OfficialNewsroomError:
            continue
        total_entries += 1
        try:
            modified = date_parser.isoparse(lastmod.get_text(strip=True))
        except (TypeError, ValueError, OverflowError):
            continue
        if modified.date() >= cutoff_date:
            candidates.append((url, modified))
    if len(candidates) > MAX_SITEMAP_CANDIDATES:
        raise OfficialNewsroomError(
            f"sitemap candidate limit exceeded: {len(candidates)} > "
            f"{MAX_SITEMAP_CANDIDATES}"
        )
    return candidates, total_entries


def _moderna_official_page_has_audited_feed(text, *, page_url):
    """Prove that an audited Moderna page embeds the exact MRNA feed.

    Issuer Direct is discovery-only.  Its records never become evidence on
    their own; every candidate must still resolve to, and be verified on, the
    issuer-owned Moderna newsroom.
    """
    parsed_page = urlsplit(str(page_url or "").strip())
    if (
        parsed_page.scheme != "https"
        or parsed_page.query
        or parsed_page.fragment
        or parsed_page.path not in {"", "/"}
        or (parsed_page.hostname or "").lower()
        not in {"news.modernatx.com", "investors.modernatx.com"}
    ):
        raise OfficialNewsroomError(
            f"untrusted Moderna discovery page URL: {page_url!r}"
        )
    soup = BeautifulSoup(text, "html.parser")
    for iframe in soup.find_all("iframe", src=True):
        parsed = urlsplit(str(iframe.get("src") or "").strip())
        try:
            port = parsed.port
        except ValueError:
            continue
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (
            parsed.scheme == "https"
            and (parsed.hostname or "").lower() == "feeds.issuerdirect.com"
            and parsed.username is None
            and parsed.password is None
            and port is None
            and parsed.path == "/news.html"
            and not parsed.fragment
            and query.get("symbol") == ["MRNA"]
        ):
            return True
    raise OfficialNewsroomError(
        "Moderna official page lacks the audited issuer-bound MRNA feed"
    )


def _moderna_corporate_page_binds_newsroom(text, *, page_url):
    """Verify Moderna's corporate page explicitly links its newsroom root."""
    parsed_page = urlsplit(str(page_url or "").strip())
    if (
        parsed_page.scheme != "https"
        or parsed_page.username is not None
        or parsed_page.password is not None
        or parsed_page.port is not None
        or parsed_page.query
        or parsed_page.fragment
        or (parsed_page.hostname or "").lower() != "www.modernatx.com"
        or parsed_page.path != "/newsroom/news-and-media"
    ):
        raise OfficialNewsroomError(
            f"untrusted Moderna corporate newsroom URL: {page_url!r}"
        )

    soup = BeautifulSoup(text, "html.parser")
    for link in soup.find_all("a", href=True):
        raw_href = str(link.get("href") or "").strip()
        parsed_link = urlsplit(raw_href)
        try:
            link_port = parsed_link.port
        except ValueError:
            continue
        if (
            parsed_link.scheme != "https"
            or parsed_link.username is not None
            or parsed_link.password is not None
            or link_port is not None
            or parsed_link.query
            or parsed_link.fragment
            or (parsed_link.hostname or "").lower()
            != MODERNA_CANONICAL_HOST
            or parsed_link.path not in {"", "/"}
        ):
            continue
        semantic_text = _normalized_title(
            " ".join(
                [
                    link.get_text(" ", strip=True),
                    str(link.get("title") or ""),
                    str(link.get("aria-label") or ""),
                ]
            )
        )
        if "press release" in semantic_text or "newsroom" in semantic_text:
            return True
    raise OfficialNewsroomError(
        "Moderna corporate page lacks an exact semantic newsroom link"
    )


def _moderna_discovery_binding(session, *, timeout):
    """Find a first-party page that binds Moderna to the MRNA feed.

    The newsroom and investor-relations pages are independently hosted
    first-party surfaces.  Failure of either one must not prevent checking the
    other, because their shared embedded feed remains discovery-only.
    """
    failures = []
    binding_pages = (
        (MODERNA_HOMEPAGE_URL, _moderna_official_page_has_audited_feed),
        (MODERNA_IR_URL, _moderna_official_page_has_audited_feed),
        (
            MODERNA_CORPORATE_NEWSROOM_URL,
            _moderna_corporate_page_binds_newsroom,
        ),
    )
    for page_url, validator in binding_pages:
        try:
            response = session.get(page_url, timeout=timeout)
            _require_exact_final_url(
                response,
                page_url,
                label="Moderna official discovery page",
            )
            page = _response_text(response)
            validator(
                page,
                page_url=page_url,
            )
            return page_url
        except Exception as error:
            failures.append(f"{page_url}: {error}")
    raise OfficialNewsroomError(
        "Moderna official discovery pages unavailable or unbound: "
        + "; ".join(failures)
    )


def _moderna_embedded_candidates(text, *, cutoff_date):
    """Read the issuer-bound feed as candidates, never as T1 evidence."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OfficialNewsroomError("Moderna embedded feed is not JSON") from error
    if not isinstance(payload, list):
        raise OfficialNewsroomError("Moderna embedded feed is not a list")

    candidates = []
    seen = set()
    total_entries = 0
    for item in payload:
        if not isinstance(item, dict):
            continue
        if item.get("topic") != "MRNA" or item.get("source") != "Custom":
            continue
        try:
            url = _strict_article_url(
                item.get("permalink"), source_id="moderna_newsroom"
            )
        except OfficialNewsroomError:
            # Third-party platform URLs and aggregator-only records must never
            # inherit Moderna's T1 identity.
            continue
        title = _clean_text(item.get("headline"))
        raw_timestamp = item.get("datetime")
        if not title or isinstance(raw_timestamp, bool) or not isinstance(
            raw_timestamp, (int, float)
        ):
            continue
        try:
            published = datetime.fromtimestamp(raw_timestamp, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            continue
        if url in seen:
            continue
        seen.add(url)
        total_entries += 1
        if published.date() >= cutoff_date:
            candidates.append((url, published, title))
    if len(candidates) > MAX_SITEMAP_CANDIDATES:
        raise OfficialNewsroomError(
            f"embedded feed candidate limit exceeded: {len(candidates)} > "
            f"{MAX_SITEMAP_CANDIDATES}"
        )
    return candidates, total_entries


def _merck_article(text, expected_url):
    soup = BeautifulSoup(text, "html.parser")
    canonical = _strict_article_url(
        _canonical_url(soup), source_id="merck_newsroom"
    )
    if canonical.rstrip("/") != expected_url.rstrip("/"):
        raise OfficialNewsroomError("Merck canonical URL does not match sitemap")
    records = []
    for item in _json_ld_objects(soup):
        types = item.get("@type")
        if isinstance(types, str):
            types = [types]
        if not isinstance(types, list) or "NewsArticle" not in types:
            continue
        try:
            raw_record_url = item.get("url") or item.get("mainEntityOfPage")
            if isinstance(raw_record_url, dict):
                raw_record_url = raw_record_url.get("@id") or raw_record_url.get("url")
            record_url = _strict_article_url(
                raw_record_url,
                source_id="merck_newsroom",
            )
        except OfficialNewsroomError:
            continue
        if record_url.rstrip("/") == canonical.rstrip("/"):
            records.append(item)
    if len(records) != 1:
        raise OfficialNewsroomError(
            "Merck page lacks one canonical NewsArticle record"
        )
    record = records[0]
    published = date_parser.isoparse(str(record.get("datePublished") or ""))
    if published.tzinfo is None:
        raise OfficialNewsroomError("Merck publication time lacks timezone")
    title = _clean_text(record.get("headline"))
    if not title:
        raise OfficialNewsroomError("Merck NewsArticle lacks headline")
    description = ""
    for item in _json_ld_objects(soup):
        if item.get("@type") == "WebPage" and item.get("description"):
            description = _clean_text(item["description"])
            break
    if not description:
        description_node = soup.find("meta", attrs={"name": "description"})
        description = _clean_text(
            description_node.get("content") if description_node else ""
        )
    main = soup.select_one(
        "main#main, article, [itemprop='articleBody'], .article-body, .news-detail"
    )
    content = _clean_text(main.get_text(" ", strip=True) if main else description)
    return {
        "title": title,
        "link": canonical,
        "summary": description,
        "content": content[:30000],
        "source": "Merck News Releases",
        "feed_url": MERCK_SITEMAP_URL,
        "published_at": published.astimezone(timezone.utc).isoformat(),
        "reference_urls": [],
    }


def _moderna_article(text, expected_url, *, expected_title=None):
    soup = BeautifulSoup(text, "html.parser")
    canonical = _strict_article_url(
        _canonical_url(soup), source_id="moderna_newsroom"
    )
    if canonical.rstrip("/") != expected_url.rstrip("/"):
        raise OfficialNewsroomError("Moderna canonical URL does not match sitemap")
    headline_node = soup.find("h1")
    title = _clean_text(
        headline_node.get_text(" ", strip=True) if headline_node else ""
    )
    if not title:
        raise OfficialNewsroomError("Moderna official page lacks headline")
    if expected_title and _normalized_title(title) != _normalized_title(
        expected_title
    ):
        raise OfficialNewsroomError(
            "Moderna official headline does not match issuer-bound discovery"
        )

    body_parts = []
    if headline_node:
        for paragraph in headline_node.find_all_next("p", limit=120):
            value = _clean_text(paragraph.get_text(" ", strip=True))
            if value:
                body_parts.append(value)
    published_date = None
    for value in body_parts[:8]:
        for date_format in ("%B %d, %Y", "%b %d, %Y"):
            try:
                published_date = datetime.strptime(value, date_format).date()
                break
            except ValueError:
                continue
        if published_date is not None:
            break
    if published_date is None:
        raise OfficialNewsroomError("Moderna page lacks an auditable publication date")
    published = datetime.combine(
        published_date,
        datetime_time.min,
        tzinfo=ZoneInfo("America/New_York"),
    ).astimezone(timezone.utc)

    description_node = soup.find("meta", attrs={"name": "description"})
    description = _clean_text(
        description_node.get("content") if description_node else ""
    )
    content = " ".join(body_parts)[:30000]
    return {
        "title": title,
        "link": canonical,
        "summary": description or " ".join(body_parts[1:4]),
        "content": content,
        "source": "Moderna Press Releases",
        "feed_url": MODERNA_SITEMAP_URL,
        "published_at": published.isoformat(),
        "reference_urls": [],
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
        "content_type": "application/xml",
    }


def _fetch_one(entry, *, now, hours_back, session, timeout, max_workers):
    source_id = str(entry.get("id") or "")
    if source_id == "smic_newsroom":
        from smic_newsroom import fetch_smic_newsroom
        return fetch_smic_newsroom(entry, now=now, hours_back=hours_back,
                                   session=session, timeout=timeout)
    sitemap_url = str(entry.get("url") or "")
    expected_url = {
        "moderna_newsroom": MODERNA_SITEMAP_URL,
        "merck_newsroom": MERCK_SITEMAP_URL,
    }.get(source_id)
    if source_id not in SUPPORTED_SOURCE_IDS or sitemap_url != expected_url:
        raise OfficialNewsroomError(
            f"official newsroom URL does not match audited source {source_id!r}"
        )

    started = time.perf_counter()
    health = _health(sitemap_url)
    try:
        response = session.get(sitemap_url, timeout=timeout)
        _require_exact_final_url(
            response,
            sitemap_url,
            label=f"{source_id} audited discovery endpoint",
        )
        discovery_binding_url = ""
        if (
            source_id == "moderna_newsroom"
            and int(getattr(response, "status_code", 200)) == 403
        ):
            # Duda occasionally requires a first-party home-page cookie before
            # serving the sitemap.  Check both audited first-party surfaces,
            # but retry the sitemap even when those pages are temporarily
            # unavailable; a homepage 403 must not abort this recovery path.
            try:
                discovery_binding_url = _moderna_discovery_binding(
                    session,
                    timeout=timeout,
                )
            except OfficialNewsroomError:
                discovery_binding_url = ""
            response = session.get(sitemap_url, timeout=timeout)
            _require_exact_final_url(
                response,
                sitemap_url,
                label=f"{source_id} audited discovery endpoint",
            )
            health["attempts"] = 2
        cutoff_date = (now - timedelta(hours=hours_back)).date()
        if (
            source_id == "moderna_newsroom"
            and int(getattr(response, "status_code", 200)) == 403
        ):
            # The fallback is authorized by an exact issuer-owned iframe
            # binding.  Issuer Direct supplies candidates only; detail pages
            # remain subject to strict Moderna canonical/title/date checks.
            if not discovery_binding_url:
                discovery_binding_url = _moderna_discovery_binding(
                    session,
                    timeout=timeout,
                )
            discovery = _response_text(
                session.get(MODERNA_EMBEDDED_FEED_URL, timeout=timeout)
            )
            candidates, total_entries = _moderna_embedded_candidates(
                discovery, cutoff_date=cutoff_date
            )
            health["content_type"] = "application/json"
            health["discovery_fallback"] = "issuer_bound_embedded_feed"
            health["discovery_binding_url"] = discovery_binding_url
        else:
            discovery = _response_text(response)
        if source_id == "merck_newsroom":
            candidates, total_entries = _merck_index_candidates(
                discovery, cutoff_date=cutoff_date
            )
            health["content_type"] = "text/html"
        elif not health.get("discovery_fallback"):
            candidates, total_entries = _sitemap_candidates(
                discovery,
                source_id=source_id,
                cutoff_date=cutoff_date,
            )
        # Availability is based on the audited article inventory, while page
        # downloads remain bounded to candidates whose sitemap lastmod is in
        # the requested window.
        health["total_entries"] = total_entries
        article_parser = (
            _moderna_article if source_id == "moderna_newsroom" else _merck_article
        )
        parsed = []
        quarantined = []

        def fetch_detail(candidate):
            url = candidate[0]
            expected_title = candidate[2] if len(candidate) > 2 else None
            detail_response = session.get(url, timeout=timeout)
            _require_article_final_url(
                detail_response,
                url,
                source_id=source_id,
            )
            detail = _response_text(detail_response)
            if source_id == "moderna_newsroom":
                article = article_parser(
                    detail, url, expected_title=expected_title
                )
                if expected_title:
                    issuer_timezone = ZoneInfo("America/New_York")
                    discovered_date = candidate[1].astimezone(
                        issuer_timezone
                    ).date()
                    article_date = date_parser.isoparse(
                        article["published_at"]
                    ).astimezone(issuer_timezone).date()
                    if article_date != discovered_date:
                        raise OfficialNewsroomError(
                            "Moderna official publication date does not match "
                            "issuer-bound discovery"
                        )
                return article
            return article_parser(detail, url)

        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 8))) as executor:
            futures = {
                executor.submit(fetch_detail, candidate): candidate[0]
                for candidate in candidates
            }
            for future in as_completed(futures):
                try:
                    parsed.append(future.result())
                except Exception as error:
                    quarantined.append(f"{futures[future]}: {error}")

        cutoff = now - timedelta(hours=hours_back)
        recent = []
        newest = None
        for article in parsed:
            published = date_parser.isoparse(article["published_at"])
            newest = published if newest is None else max(newest, published)
            if published > now + MAX_FUTURE_SKEW:
                quarantined.append(f"{article['link']}: future publication time")
            elif published >= cutoff:
                recent.append(article)
        health["fresh_entries"] = len(recent)
        health["fresh"] = bool(recent)
        health["quarantined_entries"] = len(quarantined)
        health["newest_published_at"] = newest.isoformat() if newest else None
        reasons = []
        if quarantined:
            reasons.append(f"{len(quarantined)} entries quarantined")
        if not recent:
            reasons.append("no fresh entries")
        if reasons:
            health["status"] = "degraded"
            health["error"] = "; ".join(reasons)
        return recent, health
    except Exception as error:
        health["status"] = "failed"
        health["error"] = str(error)
        return [], health
    finally:
        health["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)


def fetch_official_newsrooms(
    entries,
    *,
    hours_back,
    now,
    session=None,
    request_timeout=15,
    max_workers=6,
):
    """Fetch exact audited official newsroom entries in registry order."""
    now = now.astimezone(timezone.utc)
    session = session or cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "mobile": False}
    )
    if hasattr(session, "headers"):
        session.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://investors.modernatx.com/",
            }
        )
    articles = []
    health = []
    for entry in entries:
        local_articles, local_health = _fetch_one(
            entry,
            now=now,
            hours_back=hours_back,
            session=session,
            timeout=request_timeout,
            max_workers=max_workers,
        )
        articles.extend(local_articles)
        health.append(local_health)
    articles.sort(
        key=lambda item: (item.get("published_at", ""), item.get("link", "")),
        reverse=True,
    )
    return articles, health
