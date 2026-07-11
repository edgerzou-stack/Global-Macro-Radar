import akshare as ak
try:
    df = ak.stock_us_spot_em()
    print("\n--- AKSHARE US SPOT EM ---")
    print(df.columns.tolist())
    print(df.head(2))
except Exception as e:
    print(e)
