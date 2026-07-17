import argparse
import json
import os
import shutil
import tempfile
import time
from io import StringIO

import akshare as ak
import pandas as pd
import requests


MIN_COUNTS = {"A": 400, "US": 300, "HK": 20}
FALLBACK_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_COUNT_CHANGE_RATIO = 0.20
HTTP_TIMEOUT = (5, 15)
UNIVERSE_FIXTURE_SCHEMA_VERSION = 1


class UniverseRefreshError(RuntimeError):
    """Raised when neither a validated refresh nor a fresh fallback is available."""


def _get_html(url):
    headers = {"User-Agent": "GlobalMacroRadar/1.0 universe-health"}
    response = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.text


def _normalized(values):
    return sorted({str(value).strip() for value in values if str(value).strip()})


def get_hsi_tickers():
    url = "https://en.wikipedia.org/wiki/Hang_Seng_Index"
    try:
        tables = pd.read_html(StringIO(_get_html(url)))
        for table in tables:
            if "Ticker" not in table.columns:
                continue
            cleaned = []
            for raw in table["Ticker"].astype(str):
                number = "".join(filter(str.isdigit, raw))
                if number:
                    cleaned.append(number.zfill(4) + ".HK")
            return _normalized(cleaned)
    except Exception as error:
        print(f"Failed HSI: {error}")
    return []


def get_us_tickers():
    sp500, nasdaq = [], []
    try:
        tables = pd.read_html(
            StringIO(
                _get_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
            )
        )
        sp500 = tables[0]["Symbol"].tolist()
    except Exception as error:
        print(f"Failed S&P 500: {error}")

    try:
        tables = pd.read_html(
            StringIO(_get_html("https://en.wikipedia.org/wiki/Nasdaq-100"))
        )
        for table in tables:
            if "Ticker" in table.columns:
                nasdaq = table["Ticker"].tolist()
                break
    except Exception as error:
        print(f"Failed NDX 100: {error}")
    return _normalized(sp500 + nasdaq)


def get_a_tickers():
    try:
        csi300 = ak.index_stock_cons(symbol="000300")["品种代码"].tolist()
        csi500 = ak.index_stock_cons(symbol="000905")["品种代码"].tolist()
        return _normalized(csi300 + csi500)
    except Exception as error:
        print(f"Failed A-share components: {error}")
        return []


def _validated_payload(payload):
    if not isinstance(payload, dict):
        raise UniverseRefreshError("universe payload must be an object")
    result = {}
    for market, minimum in MIN_COUNTS.items():
        values = payload.get(market)
        if not isinstance(values, list):
            raise UniverseRefreshError(f"universe {market} must be a list")
        result[market] = _normalized(values)
        if len(result[market]) < minimum:
            raise UniverseRefreshError(
                f"universe {market} is below minimum size: {len(result[market])} < {minimum}"
            )
    return result


def load_universe_fixture(path):
    fixture_path = os.path.abspath(os.fspath(path))
    try:
        with open(fixture_path, "r", encoding="utf-8") as handle:
            fixture = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise UniverseRefreshError(
            f"cannot load universe fixture {fixture_path}: {error}"
        ) from error
    if not isinstance(fixture, dict):
        raise UniverseRefreshError("universe fixture must be an object")
    if fixture.get("schema_version") != UNIVERSE_FIXTURE_SCHEMA_VERSION:
        raise UniverseRefreshError("unsupported universe fixture schema_version")
    return _validated_payload(fixture.get("data"))


def _load_fallback(path):
    if not os.path.exists(path):
        return None
    age = time.time() - os.path.getmtime(path)
    if age > FALLBACK_MAX_AGE_SECONDS:
        raise UniverseRefreshError(
            f"universe fallback is stale: age_seconds={int(age)}"
        )
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return _validated_payload(json.load(handle))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise UniverseRefreshError(f"invalid universe fallback: {error}") from error


def _choose_market(market, refreshed, fallback):
    minimum = MIN_COUNTS[market]
    candidate = _normalized(refreshed)
    if len(candidate) < minimum:
        if fallback is None:
            raise UniverseRefreshError(
                f"{market} refresh returned {len(candidate)} symbols and no fallback exists"
            )
        print(f"WARNING: {market} refresh too small; using validated fallback")
        return fallback[market]

    if fallback:
        previous = len(fallback[market])
        change_ratio = abs(len(candidate) - previous) / previous
        if change_ratio > MAX_COUNT_CHANGE_RATIO:
            raise UniverseRefreshError(
                f"{market} universe size changed by {change_ratio:.1%}; manual review required"
            )
    return candidate


def _atomic_write_json(path, payload):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=".universes.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def main(project_dir=None, fixture_path=None):
    project_dir = project_dir or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    out_path = os.path.join(project_dir, "universes.json")
    backup_path = os.path.join(project_dir, "universes_backup.json")
    fallback = _load_fallback(out_path)

    fixture_path = fixture_path or os.environ.get("UNIVERSE_FIXTURE")
    if fixture_path:
        print(f"Loading deterministic universe fixture: {fixture_path}")
        refreshed = load_universe_fixture(fixture_path)
    else:
        print("Fetching A-share universes...")
        refreshed = {
            "A": get_a_tickers(),
            "US": get_us_tickers(),
            "HK": get_hsi_tickers(),
        }
    result = {
        market: _choose_market(market, refreshed[market], fallback)
        for market in ("A", "US", "HK")
    }
    _validated_payload(result)

    if fallback is not None:
        shutil.copy2(out_path, backup_path)
    _atomic_write_json(out_path, result)
    print(
        "Universes saved to "
        f"{out_path}: A={len(result['A'])}, US={len(result['US'])}, HK={len(result['HK'])}"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Refresh validated market universes")
    parser.add_argument("--project-dir")
    parser.add_argument(
        "--fixture",
        help="versioned JSON fixture; bypasses every live universe provider",
    )
    cli_args = parser.parse_args()
    main(project_dir=cli_args.project_dir, fixture_path=cli_args.fixture)
