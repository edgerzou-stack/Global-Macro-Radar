import hashlib
import io
import json
import re
import threading
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import List
from urllib.parse import urljoin

import pypdf
import requests
from bs4 import BeautifulSoup

from free_financials import (
    CumulativeObservation,
    FinancialDataUnavailableError,
    FinancialSourceError,
)


HKEX_BASE_URL = "https://www1.hkexnews.hk"
HKEX_SEARCH_URL = f"{HKEX_BASE_URL}/search/titlesearch.xhtml"
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 10
MAX_PDF_SCAN_PAGES = 60
MAX_EXTRACTED_TEXT_CHARS = 1_000_000
# Eight recent result announcements cover at least five discrete quarters or
# three half-year periods, including the YoY comparison required by the screen.
MAX_RESULT_DOCUMENTS = 8
_ACTIVE_STOCK_IDS = None
_ACTIVE_STOCK_IDS_LOCK = threading.Lock()

RESULT_TITLE = re.compile(
    r"(?:ANNUAL|INTERIM|HALF[- ]YEAR|FIRST QUARTER(?:LY)?|"
    r"THIRD QUARTER(?:LY)?|QUARTERLY|YEARLY)\s+RESULTS?",
    re.IGNORECASE,
)
DATE_IN_ROW = re.compile(r"\b(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}))?\b")
NUMBER_TOKEN = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")
FINANCIAL_TABLE_MARKER = re.compile(
    r"(?:CONSOLIDATED\s+(?:INCOME\s+STATEMENT|STATEMENT\s+OF\s+PROFIT)|"
    r"REVENUE.{0,1200}PROFIT\s+(?:FOR|ATTRIBUTABLE|BEFORE))",
    re.IGNORECASE | re.DOTALL,
)


def _repair_extracted_text(text: str) -> str:
    """Repair a small set of deterministic missing-``t`` PDF glyph mappings."""
    repairs = (
        (r"\bmon\s+hs\b", "months"),
        (r"\bTo\s+al\b", "Total"),
        (r"\bRepor\s+ed\b", "Reported"),
        (r"\bNe\s+profi\b", "Net profit"),
        (r"\bProfi\b", "Profit"),
        (r"\ba\s+ribu\s+able\b", "attributable"),
        (r"\bequi\s+y\b", "equity"),
        (r"\bof\s+he\s+Company\b", "of the Company"),
        (r"\bo\s+(?=(?:equity\s+)?shareholders\b)", "to "),
    )
    repaired = text
    for pattern, replacement in repairs:
        repaired = re.sub(pattern, replacement, repaired, flags=re.IGNORECASE)
    repaired = re.sub(
        r"\b(\d{1,2})(?:st|nd|rd|th|s)\s+([A-Za-z]+\s+\d{4})\b",
        r"\1 \2",
        repaired,
        flags=re.IGNORECASE,
    )
    return repaired


def _parse_english_date(value: str) -> date:
    cleaned = re.sub(r"\s+", " ", value.strip()).title()
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    raise FinancialSourceError(f"Unsupported HKEX period date: {value!r}")


def extract_period(text: str, announcement_title: str = ""):
    patterns = (
        ("FY", 12, r"(?:FOR THE\s+)?YEAR ENDED\s+(\d{1,2}\s+[A-Z]+\s+\d{4})"),
        ("H1", 6, r"(?:FOR THE\s+)?SIX MONTHS ENDED\s+(\d{1,2}\s+[A-Z]+\s+\d{4})"),
        ("9M", 9, r"(?:FOR THE\s+)?NINE MONTHS ENDED\s+(\d{1,2}\s+[A-Z]+\s+\d{4})"),
        ("Q", 3, r"(?:FOR THE\s+)?THREE MONTHS ENDED\s+(\d{1,2}\s+[A-Z]+\s+\d{4})"),
    )
    title = announcement_title.upper()
    preferred_code = None
    if re.search(r"\b(?:ANNUAL|YEARLY)\s+RESULT", title):
        preferred_code = "FY"
    elif re.search(r"\b(?:INTERIM|HALF[- ]YEAR)\s+RESULT", title):
        preferred_code = "H1"
    elif re.search(r"\bFIRST\s+QUARTER", title):
        preferred_code = "Q1"
    elif re.search(r"\bTHIRD\s+QUARTER", title):
        preferred_code = "Q3"

    candidates = []
    for period_code, months, pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            resolved_code = period_code
            if period_code == "Q":
                resolved_code = preferred_code if preferred_code in {"Q1", "Q3"} else "Q?"
            candidates.append(
                (resolved_code, months, _parse_english_date(match.group(1)), match.start())
            )

    if candidates:
        matching_title = [item for item in candidates if item[0] == preferred_code]
        pool = matching_title or candidates
        # Prefer the latest period mentioned in the announcement.  This avoids
        # selecting an older annual comparative embedded in an interim report.
        period_code, months, period_end, _position = max(
            pool, key=lambda item: (item[2], -item[3])
        )
        return period_code, months, period_end

    range_match = re.search(
        r"(?:AS\s+)?FROM\s+(\d{1,2}\s+[A-Z]+\s+\d{4})\s+TO\s+"
        r"(\d{1,2}\s+[A-Z]+\s+\d{4})",
        text,
        re.IGNORECASE,
    )
    if range_match:
        start = _parse_english_date(range_match.group(1))
        period_end = _parse_english_date(range_match.group(2))
        duration_days = (period_end - start).days + 1
        if 150 <= duration_days <= 220:
            return "H1", 6, period_end
        if 330 <= duration_days <= 400:
            return "FY", 12, period_end
        if 240 <= duration_days <= 310:
            return "9M", 9, period_end
        if 70 <= duration_days <= 120:
            return "Q?", 3, period_end
    return None, 0, None


def _parse_number(value: str) -> float:
    cleaned = value.replace(",", "").strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    number = float(cleaned)
    return -number if negative else number


def _normalise_currency(token: str) -> str:
    token = token.upper().replace(" ", "")
    if token.startswith("HKMILLIONDOLLAR"):
        return "HKD"
    return {
        "HK$": "HKD",
        "RMB": "CNY",
        "RENMINBI": "CNY",
        "US$": "USD",
    }.get(token, token)


def _unit_multiplier(token: str) -> float:
    token = (token or "").lower()
    if token.startswith("bil"):
        return 1_000_000_000.0
    if token.startswith("mil"):
        return 1_000_000.0
    if token.startswith("thou") or "000" in token:
        return 1_000.0
    return 1.0


def _extract_metric(text: str, labels):
    """Return a metric and its currency without mistaking dates/notes for values.

    HKEX PDFs are not structurally consistent.  Prefer an explicitly denominated
    amount in the same sentence as the row/metric label, then fall back to a
    same-line table value.  We deliberately do not take the first arbitrary
    number after a label: that was how report years such as ``2023`` became
    revenue values.
    """
    flattened = re.sub(r"[\t\xa0 ]+", " ", text)
    # PDF text extraction occasionally splits a comma-group (``11,7 42`` or
    # ``5, 227``). Repair only comma-delimited groups, never arbitrary dates.
    flattened = re.sub(r"(?<=,)\s+(?=\d)", "", flattened)
    flattened = re.sub(r"(?<=,\d)\s+(?=\d{2,3}\b)", "", flattened)
    currency_amount = re.compile(
        r"(?P<currency>HK\s*\$|HKD|RMB|CNY|RENMINBI|US\s*\$|USD)\s*"
        r"(?P<amount>\(?-?\d[\d,]*(?:\.\d+)?\)?)\s*"
        r"(?P<unit>billions?|millions?|thousands?|['’]000)?",
        re.IGNORECASE,
    )
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    document_currency, document_multiplier = _currency_and_multiplier(text)
    all_metric_candidates = []
    for label_index, label in enumerate(labels):
        metric_candidates = []
        for label_match in re.finditer(label, flattened, re.IGNORECASE):
            # Do not cross a sentence boundary: otherwise a heading containing
            # "revenue" can capture an unrelated currency amount later on.
            tail = flattened[label_match.end() : label_match.end() + 220]
            tail = re.split(r";|(?<!\d)\.(?!\d)", tail, maxsplit=1)[0]
            amount_match = currency_amount.search(tail)
            if amount_match is not None:
                value = _parse_number(amount_match.group("amount"))
                if re.search(r"\bloss\b", tail[: amount_match.start()], re.IGNORECASE):
                    value = -abs(value)
                metric_candidates.append(
                    (
                        2,
                        -label_match.start(),
                        value * _unit_multiplier(amount_match.group("unit")),
                        _normalise_currency(amount_match.group("currency")),
                    )
                )

        for index, line in enumerate(lines):
            label_match = re.search(label, line, re.IGNORECASE)
            if label_match is None:
                continue
            suffix = line[label_match.end() :]
            # Values sometimes start on the next extracted line, but prose
            # sentences must never be allowed to consume numbers from several
            # later lines (for example an issued-share count in a footnote).
            suffix_without_notes = re.sub(r"^(?:\s*\(\d+\))+\s*", "", suffix)
            suffix = suffix_without_notes
            if (
                not NUMBER_TOKEN.search(suffix_without_notes)
                and len(line) <= 100
                and not re.search(r"[.;:]", suffix_without_notes)
                and index + 1 < len(lines)
            ):
                suffix = f"{suffix} {lines[index + 1]}"
            tokens = NUMBER_TOKEN.findall(suffix)
            row_values = []
            for token in tokens:
                value = _parse_number(token)
                # Reject note numbers and report years before considering a row
                # value.  Actual small values remain available as a last resort.
                if 1900 <= abs(value) <= 2100:
                    continue
                row_values.append(value)
            if not row_values:
                continue
            material = [value for value in row_values if abs(value) > 100]
            value = material[0] if material else row_values[0]
            context = " ".join(lines[max(0, index - 8) : index + 4])
            default_currency, default_multiplier = _currency_and_multiplier(context)
            if default_multiplier == 1.0 and document_multiplier != 1.0:
                default_multiplier = document_multiplier
            if default_currency == "HKD" and document_currency != "HKD":
                default_currency = document_currency
            # A row with current and comparative values is stronger evidence
            # than a rounded narrative sentence such as "HK$5.7 billion".
            table_score = 3 if len(material) >= 2 else 1
            metric_candidates.append(
                (
                    table_score,
                    -index,
                    value * default_multiplier,
                    default_currency,
                )
            )
        all_metric_candidates.extend(
            (score, -label_index, position, value, currency)
            for score, position, value, currency in metric_candidates
        )
    if all_metric_candidates:
        _score, _label_priority, _position, value, currency = max(
            all_metric_candidates
        )
        return value, currency
    raise FinancialSourceError(f"Missing HKEX metric: {labels[0]}")


PERIOD_HEADER = re.compile(
    r"(?:YEAR|SIX MONTHS|NINE MONTHS|THREE MONTHS)\s+ENDED",
    re.IGNORECASE,
)


def _period_section(text: str, period_code: str) -> str:
    desired = {
        "FY": r"YEAR\s+ENDED",
        "H1": r"SIX MONTHS\s+ENDED",
        "9M": r"NINE MONTHS\s+ENDED",
        "Q1": r"THREE MONTHS\s+ENDED",
        "Q3": r"THREE MONTHS\s+ENDED",
        "Q?": r"THREE MONTHS\s+ENDED",
    }[period_code]
    for match in re.finditer(desired, text, re.IGNORECASE):
        following = text[match.start() : match.start() + 2500]
        competing = PERIOD_HEADER.search(following, match.end() - match.start())
        if competing is not None:
            following = following[: competing.start()]
        if re.search(r"\bRevenues?\b|\bTurnover\b", following, re.IGNORECASE):
            return following
    raise FinancialSourceError(f"Could not locate {period_code} result table")


def _currency_and_multiplier(text: str):
    currency = "HKD"
    statement_unit_match = re.search(
        r"ALL\s+AMOUNTS\s+EXPRESSED\s+IN\s+"
        r"(?P<unit>MILLIONS?|THOUSANDS?)\s+OF\s+"
        r"(?P<currency>RENMINBI|HONG\s+KONG\s+DOLLARS?|US\s+DOLLARS?)",
        text,
        re.IGNORECASE,
    )
    if statement_unit_match:
        currency_name = statement_unit_match.group("currency").upper()
        currency = (
            "CNY"
            if currency_name == "RENMINBI"
            else "USD"
            if currency_name.startswith("US ")
            else "HKD"
        )
        multiplier = (
            1_000_000.0
            if statement_unit_match.group("unit").upper().startswith("MILLION")
            else 1_000.0
        )
        return currency, multiplier

    explicit_unit_match = re.search(
        r"(?P<hk_million>HK\s+MILLION\s+DOLLARS?)|"
        r"(?P<currency>HK\$|HKD|RMB|CNY|RENMINBI|US\$|USD)"
        r"\s*(?:IN\s+)?(?P<unit>MILLIONS?|THOUSANDS?|['’]000)",
        text,
        re.IGNORECASE,
    )
    # Prefer an explicit accounting unit header over an earlier narrative
    # currency mention (e.g. an RMB gas margin before an HK$ result table).
    if explicit_unit_match:
        currency = _normalise_currency(
            explicit_unit_match.group("hk_million")
            or explicit_unit_match.group("currency")
        )
    else:
        currency_match = re.search(
            r"(HK\s+MILLION\s+DOLLARS?|HK\$|HKD|RMB|CNY|RENMINBI|US\$|USD)",
            text,
            re.IGNORECASE,
        )
        if currency_match:
            currency = _normalise_currency(currency_match.group(1))

    multiplier = 1.0
    if (
        explicit_unit_match and explicit_unit_match.group("hk_million")
    ) or re.search(r"\b(?:IN\s+)?MILLIONS?\b", text, re.IGNORECASE):
        multiplier = 1_000_000.0
    elif re.search(r"(?:'000|’000|\bTHOUSANDS?\b)", text, re.IGNORECASE):
        multiplier = 1_000.0
    return currency, multiplier


def _extract_contract_revenue_total(text: str):
    """Extract an exact total when a statement lists contract revenue components."""
    heading = re.search(
        r"Revenue recognised from contracts with customers", text, re.IGNORECASE
    )
    if heading is None:
        return None
    block = text[heading.start() : heading.start() + 1400]
    expense = re.search(r"\n\s*EXPENSES?\b", block, re.IGNORECASE)
    if expense is not None:
        block = block[: expense.start()]
    components = []
    for label in (r"Oil and gas sales", r"Marketing revenues?", r"Other revenue"):
        match = re.search(
            rf"{label}\s+(?:\d+\s+)?(?P<value>\(?-?\d[\d,]*(?:\.\d+)?\)?)",
            block,
            re.IGNORECASE,
        )
        if match is None:
            return None
        components.append(_parse_number(match.group("value")))
    currency, multiplier = _currency_and_multiplier(
        text[max(0, heading.start() - 500) : heading.start() + 1400]
    )
    return sum(components) * multiplier, currency


def parse_hkex_financial_text(
    text: str,
    ticker: str,
    filed_date: date,
    source_document: str,
    announcement_title: str = "",
) -> CumulativeObservation:
    del ticker  # Retained in the public API for audit/error context compatibility.
    text = _repair_extracted_text(text)
    announcement_title = _repair_extracted_text(announcement_title)
    period_code, duration_months, period_end = extract_period(
        text, announcement_title=announcement_title
    )
    if not period_code:
        raise FinancialSourceError("Could not determine period from HKEX text")

    contract_total = _extract_contract_revenue_total(text)
    if contract_total is not None:
        revenue, revenue_currency = contract_total
    else:
        revenue, revenue_currency = _extract_metric(
            text,
            (
                r"\bConsolidated\s+Revenues?\b",
                r"\bTotal\s+Revenues?\b",
                r"\bOil\s+and\s+gas\s+sales\b",
                r"\bInsurance\s+revenue\b",
                r"\bNet\s+operating\s+income\b",
                r"\bRevenues?\b",
                r"\bTurnover\b",
            ),
        )
    try:
        net_income, income_currency = _extract_metric(
            text,
            (
                r"(?:Net\s+)?Profit attributable to (?:equity |ordinary )?(?:shareholders?|holders?|owners?)(?: of (?:the )?(?:Company|parent))?",
                r"Earnings attributable to (?:equity holders|shareholders) of (?:the )?Company",
                r"\bEquity shareholders of the Company\b",
                r"\bCompany[’']s shareholders\b",
                r"\bTotal earnings\b",
                r"\bReported earnings\b",
                r"\bNet profit\b",
                r"\bProfit for the (?:period|year)\b",
                r"\bProfit after tax\b",
            ),
        )
    except FinancialSourceError as exc:
        raise FinancialSourceError("Missing net income in HKEX text") from exc
    if revenue_currency != income_currency:
        raise FinancialSourceError(
            f"HKEX metric currency mismatch: {revenue_currency}/{income_currency}"
        )
    if not all(map(lambda value: value == value and abs(value) != float("inf"), (revenue, net_income))):
        raise FinancialSourceError("HKEX financial values are not finite")

    return CumulativeObservation(
        fiscal_year=period_end.year,
        period_code=period_code,
        duration_months=duration_months,
        duration_days=round(duration_months * 365.25 / 12.0),
        period_end=period_end,
        filed_date=filed_date,
        revenue=revenue,
        net_income=net_income,
        currency=revenue_currency,
        source="hkexnews",
        source_document=source_document,
    )


def _extract_stock_id(payload, target_code: str):
    candidates = payload.get("stockInfo", []) if isinstance(payload, dict) else payload
    if not isinstance(candidates, list):
        return None
    for item in candidates:
        if not isinstance(item, dict):
            continue
        code = str(item.get("stockCode") or item.get("c") or "").zfill(5)
        if code == target_code:
            value = item.get("stockId")
            if value is None:
                # activestock_sehk_e.json uses `i` for the title-search stockId;
                # `s` is a separate short-name identifier and returns zero results.
                value = item.get("i")
            if value is not None:
                return str(value)
    return None


def _active_stock_ids(session, headers: dict) -> dict:
    global _ACTIVE_STOCK_IDS
    if _ACTIVE_STOCK_IDS is not None:
        return _ACTIVE_STOCK_IDS
    with _ACTIVE_STOCK_IDS_LOCK:
        if _ACTIVE_STOCK_IDS is not None:
            return _ACTIVE_STOCK_IDS
        response = session.get(
            f"{HKEX_BASE_URL}/ncms/script/eds/activestock_sehk_e.json",
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        candidates = (
            payload.get("stockInfo", []) if isinstance(payload, dict) else payload
        )
        if not isinstance(candidates, list):
            raise FinancialSourceError("HKEX active-stock map has invalid shape")
        mapping = {}
        for item in candidates:
            if not isinstance(item, dict):
                continue
            code = str(item.get("stockCode") or item.get("c") or "").zfill(5)
            stock_id = item.get("stockId")
            if stock_id is None:
                stock_id = item.get("i")
            if code and stock_id is not None:
                mapping[code] = str(stock_id)
        if not mapping:
            raise FinancialSourceError("HKEX active-stock map is empty")
        _ACTIVE_STOCK_IDS = mapping
        return _ACTIVE_STOCK_IDS


def _lookup_stock_id(session, target_code: str, headers: dict) -> str:
    errors = []
    try:
        stock_id = _active_stock_ids(session, headers).get(target_code)
        if stock_id:
            return stock_id
    except (FinancialSourceError, requests.RequestException, ValueError) as exc:
        errors.append(str(exc))

    try:
        response = session.get(
            f"{HKEX_BASE_URL}/search/prefix.do",
            params={"lang": "en", "type": "A", "name": target_code, "market": "SEHK"},
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        stock_id = _extract_stock_id(response.json(), target_code)
        if stock_id:
            return stock_id
    except (requests.RequestException, ValueError) as exc:
        errors.append(str(exc))
    raise FinancialSourceError(
        f"Could not resolve HKEX stockId for {target_code}: {'; '.join(errors)}"
    )


def _parse_search_results(html: str):
    results = []
    soup = BeautifulSoup(html, "html.parser")
    for row in soup.select("tr"):
        link = next(
            (
                anchor
                for anchor in row.select("a[href]")
                if ".pdf" in anchor.get("href", "").lower()
            ),
            None,
        )
        if link is None:
            continue
        row_text = " ".join(row.stripped_strings)
        date_match = DATE_IN_ROW.search(row_text)
        if not date_match:
            continue
        released_at = datetime.strptime(
            f"{date_match.group(1)} {date_match.group(2) or '00:00'}",
            "%d/%m/%Y %H:%M",
        )
        results.append(
            {
                "title": " ".join(link.stripped_strings),
                "pdf_url": urljoin(HKEX_BASE_URL, link["href"]),
                "released_at": released_at,
            }
        )
    return sorted(results, key=lambda item: item["released_at"], reverse=True)


def _parse_search_json(payload: dict):
    raw_results = payload.get("result", [])
    if isinstance(raw_results, str):
        try:
            raw_results = json.loads(raw_results)
        except json.JSONDecodeError as exc:
            raise FinancialSourceError("HKEX title search returned invalid JSON") from exc
    if not isinstance(raw_results, list):
        raise FinancialSourceError("HKEX title search result is not a list")
    parsed = []
    for item in raw_results:
        if not isinstance(item, dict) or not item.get("FILE_LINK"):
            continue
        try:
            released_at = datetime.strptime(item["DATE_TIME"], "%d/%m/%Y %H:%M")
        except (KeyError, TypeError, ValueError):
            continue
        title = BeautifulSoup(
            f"{item.get('TITLE', '')} {item.get('LONG_TEXT', '')}", "html.parser"
        ).get_text(" ", strip=True)
        parsed.append(
            {
                "title": title,
                "pdf_url": urljoin(HKEX_BASE_URL, item["FILE_LINK"]),
                "released_at": released_at,
            }
        )
    return parsed


def _search_result_announcements(session, stock_id: str, as_of_date: date, headers):
    """Query the same JSON servlet used by HKEX's official title-search page."""
    endpoint = f"{HKEX_BASE_URL}/search/titleSearchServlet.do"
    results = {}
    window_end = as_of_date
    for _ in range(5):
        window_start = window_end - timedelta(days=365)
        params = {
            "sortDir": "0",
            "sortByOptions": "DateTime",
            "category": "0",
            "market": "SEHK",
            "stockId": stock_id,
            "documentType": "",
            "fromDate": window_start.strftime("%Y%m%d"),
            "toDate": window_end.strftime("%Y%m%d"),
            "title": "RESULTS",
            "searchType": "0",
            "t1code": "",
            "t2Gcode": "",
            "t2code": "",
            "rowRange": "100",
            "lang": "E",
        }
        response = session.get(endpoint, params=params, headers=headers, timeout=25)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise FinancialSourceError("HKEX title-search servlet returned non-JSON") from exc
        for item in _parse_search_json(payload):
            excluded = re.search(r"\b(?:PRESENTATION|WEBCAST)\b", item["title"], re.IGNORECASE)
            if (
                item["released_at"].date() <= as_of_date
                and RESULT_TITLE.search(item["title"])
                and not excluded
            ):
                results[item["pdf_url"]] = item
        if len(results) >= MAX_RESULT_DOCUMENTS:
            break
        window_end = window_start - timedelta(days=1)
    return sorted(results.values(), key=lambda item: item["released_at"], reverse=True)


def _extract_bounded_pdf_text(reader) -> str:
    """Read the front matter plus a bounded set of later financial-table pages."""
    page_count = len(reader.pages)
    head_count = min(MAX_PDF_PAGES, page_count)
    selected = set(range(head_count))
    page_text = {}

    def extract(index):
        if index not in page_text:
            page_text[index] = reader.pages[index].extract_text() or ""
        return page_text[index]

    for index in range(head_count, min(page_count, MAX_PDF_SCAN_PAGES)):
        text = extract(index)
        if not FINANCIAL_TABLE_MARKER.search(text):
            continue
        # Accounting units and wrapped table rows often spill onto adjacent
        # pages. Keep only a small neighborhood instead of the complete PDF.
        selected.update(
            page
            for page in (index - 1, index, index + 1)
            if 0 <= page < page_count
        )

    chunks = []
    total_chars = 0
    for index in sorted(selected):
        text = extract(index)
        remaining = MAX_EXTRACTED_TEXT_CHARS - total_chars
        if remaining <= 0:
            break
        chunks.append(text[:remaining])
        total_chars += min(len(text), remaining)
    return "\n".join(chunks)


def _download_pdf_text(session, url: str, headers: dict):
    response = session.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    content = response.content
    if not content.startswith(b"%PDF-"):
        raise FinancialSourceError(f"HKEX document is not a PDF: {url}")
    if len(content) > MAX_PDF_BYTES:
        raise FinancialSourceError(f"HKEX PDF exceeds {MAX_PDF_BYTES} bytes: {url}")
    digest = hashlib.sha256(content).hexdigest()
    try:
        reader = pypdf.PdfReader(io.BytesIO(content), strict=False)
        text = _extract_bounded_pdf_text(reader)
    except Exception as exc:
        raise FinancialSourceError(f"Cannot parse HKEX PDF {url}: {exc}") from exc
    if not text.strip():
        raise FinancialSourceError(f"HKEX PDF has no extractable text: {url}")
    return text, digest


def _align_fiscal_years(observations):
    annuals = sorted(
        (observation for observation in observations if observation.period_code == "FY"),
        key=lambda item: item.period_end,
    )
    if not annuals:
        return observations
    aligned = []
    for observation in observations:
        if observation.period_code == "FY":
            aligned.append(observation)
            continue
        future_annual = next(
            (
                annual
                for annual in annuals
                if observation.period_end < annual.period_end
                and (annual.period_end - observation.period_end).days <= 370
            ),
            None,
        )
        if future_annual is not None:
            updated = replace(observation, fiscal_year=future_annual.fiscal_year)
            if observation.period_code == "Q?":
                months_until = (
                    (future_annual.period_end.year - observation.period_end.year) * 12
                    + future_annual.period_end.month
                    - observation.period_end.month
                )
                quarter = 4 - months_until // 3 if months_until in {0, 3, 6, 9} else None
                if quarter not in {1, 2, 3, 4}:
                    raise FinancialSourceError("Cannot infer HKEX quarter from fiscal year end")
                updated = replace(updated, period_code=f"Q{quarter}")
            aligned.append(updated)
            continue
        prior_annuals = [annual for annual in annuals if annual.period_end < observation.period_end]
        if prior_annuals:
            prior = prior_annuals[-1]
            updated = replace(observation, fiscal_year=prior.fiscal_year + 1)
            if observation.period_code == "Q?":
                months_since = (
                    (observation.period_end.year - prior.period_end.year) * 12
                    + observation.period_end.month
                    - prior.period_end.month
                )
                quarter = months_since // 3 if months_since in {3, 6, 9, 12} else None
                if quarter not in {1, 2, 3, 4}:
                    raise FinancialSourceError("Cannot infer HKEX quarter from prior fiscal year end")
                updated = replace(updated, period_code=f"Q{quarter}")
            aligned.append(updated)
        else:
            if observation.period_code == "Q?":
                raise FinancialSourceError("Cannot infer HKEX quarter without an annual result")
            aligned.append(observation)
    return aligned


def load_hkex_financials(
    ticker: str, as_of_date: date
) -> List[CumulativeObservation]:
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Global-Macro-Radar/1.0)",
        "Referer": f"{HKEX_SEARCH_URL}?lang=en",
        "Accept-Language": "en-US,en;q=0.8",
    }
    target_code = ticker.split(".")[0].zfill(5)
    stock_id = _lookup_stock_id(session, target_code, headers)

    # Establish cookies used by the official title-search front end.
    landing = session.get(
        HKEX_SEARCH_URL, params={"lang": "en"}, headers=headers, timeout=15
    )
    landing.raise_for_status()
    candidates = _search_result_announcements(
        session, stock_id, as_of_date, headers
    )[:MAX_RESULT_DOCUMENTS]
    if not candidates:
        raise FinancialDataUnavailableError(
            f"No HKEX result announcements found for {ticker}"
        )

    observations = []
    errors = []
    for item in candidates:
        try:
            text, digest = _download_pdf_text(session, item["pdf_url"], headers)
            observation = parse_hkex_financial_text(
                text,
                ticker,
                item["released_at"].date(),
                f"{item['pdf_url']}#sha256={digest}",
                announcement_title=item["title"],
            )
            observations.append(observation)
        except (FinancialSourceError, requests.RequestException) as exc:
            errors.append(f"{item['pdf_url']}: {exc}")

    if not observations:
        raise FinancialDataUnavailableError(
            f"HKEX announcements for {ticker} could not be parsed: " + "; ".join(errors[:3])
        )

    deduplicated = {}
    for observation in observations:
        key = (observation.period_end, observation.duration_months)
        existing = deduplicated.get(key)
        if existing is None or observation.filed_date > existing.filed_date:
            deduplicated[key] = observation
    return _align_fiscal_years(list(deduplicated.values()))
