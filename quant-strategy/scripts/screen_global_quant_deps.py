import json
import os
import re
import hashlib
from datetime import datetime
import yfinance as yf
from screen_a_share import fetch_quote_snapshot_cached
from core.strategy_registry import ACTIVE_STRATEGIES

STRATEGIES = ACTIVE_STRATEGIES

HOT_SPOT_SCHEMA_VERSION = 1
HOT_SPOT_STRATEGIES = (
    "hot_spot_a_stock",
    "hot_spot_us_stock",
    "hot_spot_hk_stock",
)


class HotSpotArtifactError(RuntimeError):
    pass


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def load_universes():
    from config import PROJECT_ROOT
    project_root = PROJECT_ROOT
    uni_path = os.path.join(project_root, "universes.json")
    try:
        with open(uni_path, "r") as f:
            data = json.load(f)
            return {
                "US": data.get("US", []),
                "HK": data.get("HK", [])
            }
    except Exception as e:
        print(f"Failed to load universes.json: {e}")
        return {"US": [], "HK": []}

def load_hot_spot_today(*, expected_date=None, expected_run_id=None, artifact_path=None):
    """Load only a complete hot-spot artifact from the current pipeline run."""
    from config import PROJECT_ROOT
    from core.clock import clock

    date_text = str(expected_date or clock.today().isoformat())
    run_text = expected_run_id or os.environ.get("RUN_ID") or f"daily-{date_text}"
    path = artifact_path or os.path.join(PROJECT_ROOT, "hot_spot_today.json")

    try:
        with open(path, "r", encoding="utf-8") as handle:
            artifact = json.load(handle)
    except Exception as error:
        raise HotSpotArtifactError(
            f"Cannot load hot-spot artifact {path}: {error}"
        ) from error

    if not isinstance(artifact, dict):
        raise HotSpotArtifactError("Hot-spot artifact must be a JSON object")
    if artifact.get("schema_version") != HOT_SPOT_SCHEMA_VERSION:
        raise HotSpotArtifactError("Unsupported or legacy hot-spot artifact schema")
    if artifact.get("effective_date") != date_text:
        raise HotSpotArtifactError(
            f"Stale hot-spot artifact: expected {date_text}, "
            f"found {artifact.get('effective_date')!r}"
        )
    if artifact.get("run_id") != run_text:
        raise HotSpotArtifactError(
            f"Hot-spot run mismatch: expected {run_text!r}, "
            f"found {artifact.get('run_id')!r}"
        )
    if artifact.get("status") not in {"ok", "ok_empty"}:
        raise HotSpotArtifactError("Hot-spot artifact has a non-success status")

    generated_at = artifact.get("generated_at")
    try:
        parsed_generated_at = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise HotSpotArtifactError("Hot-spot artifact has invalid generated_at") from error
    if parsed_generated_at.tzinfo is None:
        raise HotSpotArtifactError("Hot-spot generated_at must include a timezone")

    source_report = artifact.get("source_report")
    if not isinstance(source_report, dict) or not re.fullmatch(
        r"[0-9a-f]{64}", str(source_report.get("sha256", ""))
    ):
        raise HotSpotArtifactError("Hot-spot artifact has invalid source report hash")
    source_path = source_report.get("path")
    if not isinstance(source_path, str) or not os.path.isfile(source_path):
        raise HotSpotArtifactError("Hot-spot source report is missing")
    if _sha256_file(source_path) != source_report["sha256"]:
        raise HotSpotArtifactError("Hot-spot source report hash mismatch")

    hot_news_count = artifact.get("hot_news_count")
    if type(hot_news_count) is not int or hot_news_count < 0:
        raise HotSpotArtifactError("Hot-spot artifact has invalid hot_news_count")

    data = artifact.get("data")
    if not isinstance(data, dict) or set(data) != set(HOT_SPOT_STRATEGIES):
        raise HotSpotArtifactError("Hot-spot artifact has invalid strategy keys")
    if any(not isinstance(data[key], list) for key in HOT_SPOT_STRATEGIES):
        raise HotSpotArtifactError("Hot-spot strategy payloads must be lists")
    is_empty = all(not data[key] for key in HOT_SPOT_STRATEGIES)
    if (artifact["status"] == "ok_empty") != is_empty:
        raise HotSpotArtifactError("Hot-spot status does not match its payload")
    return data

def get_current_prices_for_portfolio(all_portfolio, a_prices):
    current_prices = {}
    if a_prices:
        for k, v in a_prices.items():
            current_prices[k] = {"最新价": v}
            
    a_codes = []
    us_hk_codes = []
    yf_to_k_map = {}
    
    for strat, positions in all_portfolio.items():
        if not positions: continue
        if '_a_' in strat:
            pass # A-shares already in current_prices via a_prices
        else:
            for k in positions.keys():
                yf_sym = f"{k}.HK" if '_hk_' in strat and not k.upper().endswith('.HK') else k
                us_hk_codes.append(yf_sym)
                yf_to_k_map[yf_sym] = k
                
    a_codes = list(set(a_codes))
    us_hk_codes = list(set(us_hk_codes))
    
    if a_codes:
        df = fetch_quote_snapshot_cached(a_codes)
        for _, row in df.iterrows():
            current_prices[row["股票代码"]] = {"最新价": row["最新价"]}
            
    if us_hk_codes:
        try:
            import pandas as pd
            import pytz
            from datetime import datetime, timedelta
            from core.clock import clock
            now = clock.now().astimezone(pytz.utc)
            start_date = now - timedelta(days=7)
            data = yf.download(us_hk_codes, start=start_date.strftime("%Y-%m-%d"), progress=False)
            if not data.empty and "Close" in data:
                last_closes = data["Close"].ffill().iloc[-1]
                if isinstance(last_closes, pd.Series):
                    for sym, price in last_closes.items():
                        k = yf_to_k_map.get(sym, sym)
                        if pd.notna(price):
                            current_prices[k] = {"最新价": price}
                else:
                    if pd.notna(last_closes):
                        sym = us_hk_codes[0]
                        k = yf_to_k_map.get(sym, sym)
                        current_prices[k] = {"最新价": last_closes}
        except Exception as e:
            raise ConnectionError(f"CRITICAL: Failed to fetch YF spot prices: {e}. Aborting pipeline.")
            
    return current_prices
