from data_provider import stock_yjbb_em_cached
import pandas as pd
df = stock_yjbb_em_cached("20240331")
print(df[df["股票代码"] == "300308"][["股票代码", "净利润-同比增长", "营业总收入-同比增长"]])
