import os
import time
from dataclasses import replace
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import List

import requests
from dotenv import dotenv_values

from free_financials import (
    CumulativeObservation,
    FinancialDataUnavailableError,
    FinancialSourceError,
)


ALLOWED_FORMS = {
    "10-K",
    "10-Q",
    "10-K/A",
    "10-Q/A",
    "20-F",
    "20-F/A",
    "40-F",
    "40-F/A",
}
REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
)
INCOME_TAGS = ("NetIncomeLoss", "ProfitLoss")
RECENT_FISCAL_YEARS = 6
PROJECT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def _configured_user_agent() -> str:
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if user_agent:
        return user_agent
    try:
        return str(dotenv_values(PROJECT_ENV_FILE).get("SEC_USER_AGENT") or "").strip()
    except OSError:
        return ""


def _fact_key(fact: dict):
    return (
        fact.get("accn"),
        fact.get("fy"),
        fact.get("fp"),
        fact.get("start"),
        fact.get("end"),
        fact.get("form"),
    )


def _preferred_facts(us_gaap: dict, tags, as_of_date: date) -> dict:
    """Merge tag migrations while retaining the configured tag priority per filing."""
    selected = {}
    for tag in tags:
        units = us_gaap.get(tag, {}).get("units", {})
        for fact in units.get("USD", []):
            try:
                filed = date.fromisoformat(str(fact["filed"]))
            except (KeyError, TypeError, ValueError):
                continue
            if filed > as_of_date or fact.get("form") not in ALLOWED_FORMS:
                continue
            key = _fact_key(fact)
            if not all(key) or fact.get("val") is None:
                continue
            selected.setdefault(key, (fact, filed, tag))
    return selected


def _canonicalize_actual_periods(observations):
    """Deduplicate SEC comparatives and align them to the actual fiscal cycle.

    Company Facts repeats prior-period comparators in later filings and assigns
    them the *new filing's* ``fy``. Grouping directly on that field can replace
    the current quarter with last year's comparator. Values/provenance use the
    latest filing available as of the effective date, while the fiscal-year
    label comes from the earliest filing of that actual period.
    """
    grouped = {}
    for observation in observations:
        grouped.setdefault(
            (observation.period_end, observation.duration_days), []
        ).append(observation)

    canonical = []
    for versions in grouped.values():
        earliest = min(versions, key=lambda item: (item.filed_date, item.source_document))
        latest = max(versions, key=lambda item: (item.filed_date, item.source_document))
        canonical.append(replace(latest, fiscal_year=earliest.fiscal_year))

    annuals = sorted(
        (item for item in canonical if 330 <= item.duration_days <= 400),
        key=lambda item: item.period_end,
    )
    if not annuals:
        return sorted(canonical, key=lambda item: (item.period_end, item.duration_days))

    aligned = []
    for observation in canonical:
        if 330 <= observation.duration_days <= 400:
            aligned.append(observation)
            continue
        future = next(
            (
                annual
                for annual in annuals
                if observation.period_end <= annual.period_end
                and (annual.period_end - observation.period_end).days <= 370
            ),
            None,
        )
        if future is not None:
            aligned.append(replace(observation, fiscal_year=future.fiscal_year))
            continue
        prior = [annual for annual in annuals if annual.period_end < observation.period_end]
        if prior:
            aligned.append(replace(observation, fiscal_year=prior[-1].fiscal_year + 1))
        else:
            aligned.append(observation)
    return sorted(aligned, key=lambda item: (item.period_end, item.duration_days))


def parse_sec_companyfacts(
    payload: dict, as_of_date: date
) -> List[CumulativeObservation]:
    us_gaap = payload.get("facts", {}).get("us-gaap", {})
    revenue = _preferred_facts(us_gaap, REVENUE_TAGS, as_of_date)
    net_income = _preferred_facts(us_gaap, INCOME_TAGS, as_of_date)

    observations = []
    for key in sorted(set(revenue) & set(net_income), key=str):
        revenue_fact, revenue_filed, _revenue_tag = revenue[key]
        income_fact, income_filed, _income_tag = net_income[key]
        accession, fiscal_year, period_code, start, end, _form = key
        try:
            start_date = date.fromisoformat(str(start))
            end_date = date.fromisoformat(str(end))
            fiscal_year = int(fiscal_year)
            revenue_value = float(revenue_fact["val"])
            income_value = float(income_fact["val"])
        except (TypeError, ValueError, KeyError):
            continue
        duration_days = (end_date - start_date).days + 1
        if duration_days <= 0:
            continue
        observations.append(
            CumulativeObservation(
                fiscal_year=fiscal_year,
                period_code=str(period_code),
                duration_months=round(duration_days / (365.25 / 12.0)),
                duration_days=duration_days,
                period_end=end_date,
                filed_date=max(revenue_filed, income_filed),
                revenue=revenue_value,
                net_income=income_value,
                currency="USD",
                source="sec_edgar",
                source_document=f"sec-edgar://accession/{accession}",
            )
        )
    return _canonicalize_actual_periods(observations)


def _request_json(session, url, headers, timeout):
    last_error = None
    for attempt in range(3):
        try:
            response = session.get(url, headers=headers, timeout=timeout)
            if response.status_code == 429:
                delay = min(float(response.headers.get("Retry-After", "1")), 10.0)
                time.sleep(max(delay, 0.0))
                continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise FinancialSourceError(f"SEC request failed for {url}: {last_error}")


@lru_cache(maxsize=4)
def _company_ticker_map(user_agent: str) -> dict:
    session = requests.Session()
    payload = _request_json(
        session,
        "https://www.sec.gov/files/company_tickers.json",
        {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        15,
    )
    mapping = {}
    for item in payload.values():
        ticker = str(item.get("ticker", "")).upper()
        cik = item.get("cik_str")
        if ticker and cik is not None:
            mapping[ticker] = str(cik).zfill(10)
    if not mapping:
        raise FinancialSourceError("SEC ticker mapping is empty")
    return mapping


def load_sec_financials(
    ticker: str, as_of_date: date
) -> List[CumulativeObservation]:
    user_agent = _configured_user_agent()
    if not user_agent or "contact@example.com" in user_agent.lower():
        raise FinancialSourceError(
            "SEC_USER_AGENT must identify the application and a real contact address"
        )

    ticker_sec = ticker.upper().replace(".", "-")
    cik = _company_ticker_map(user_agent).get(ticker_sec)
    if cik is None:
        raise FinancialDataUnavailableError(
            f"SEC ticker mapping has no entry for {ticker_sec}"
        )

    session = requests.Session()
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
    payload = _request_json(
        session,
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
        headers,
        20,
    )
    observations = parse_sec_companyfacts(payload, as_of_date)
    if not observations:
        raise FinancialDataUnavailableError(
            f"SEC returned no matched revenue/net-income observations for {ticker_sec}"
        )
    fiscal_years = sorted({item.fiscal_year for item in observations})
    retained_years = set(fiscal_years[-RECENT_FISCAL_YEARS:])
    return [item for item in observations if item.fiscal_year in retained_years]
