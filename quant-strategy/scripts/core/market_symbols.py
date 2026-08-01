from core.market_data_contracts import InvalidMarketDataRequest


def to_yfinance_symbol(symbol):
    symbol_text = str(symbol)
    if "." in symbol_text and symbol_text.upper().endswith(
        ("HK", "US", "SS", "SZ", "BJ")
    ):
        return symbol
    if len(symbol_text) == 6 and symbol_text.isdigit():
        if symbol_text.startswith("6"):
            return f"{symbol_text}.SS"
        if symbol_text.startswith(("8", "4", "9")):
            return f"{symbol_text}.BJ"
        return f"{symbol_text}.SZ"
    return symbol


def to_baostock_symbol(symbol):
    symbol_text = str(symbol)
    if len(symbol_text) == 6 and symbol_text.isdigit():
        if symbol_text.startswith("6"):
            return f"sh.{symbol_text}"
        if symbol_text.startswith(("8", "4", "9")):
            return f"bj.{symbol_text}"
        return f"sz.{symbol_text}"
    return symbol


def to_sina_symbol(symbol):
    symbol_text = str(symbol)
    if len(symbol_text) == 6 and symbol_text.isdigit():
        if symbol_text.startswith("6"):
            return f"sh{symbol_text}"
        if symbol_text.startswith(("8", "4", "9")):
            return f"bj{symbol_text}"
        return f"sz{symbol_text}"
    return symbol


def to_tencent_symbol(symbol):
    symbol_text = str(symbol)
    if len(symbol_text) != 6 or not symbol_text.isdigit():
        raise InvalidMarketDataRequest(
            f"Tencent A-share fallback cannot route symbol {symbol!r}"
        )
    if symbol_text.startswith("6"):
        return f"sh{symbol_text}"
    if symbol_text.startswith(("8", "4", "9")):
        return f"bj{symbol_text}"
    return f"sz{symbol_text}"
