from data_provider import fetch_quote_snapshot_cached
df = fetch_quote_snapshot_cached(["002001"])
print(df[["股票代码", "股票简称", "最新价_raw", "decimal_scale", "最新价"]])
