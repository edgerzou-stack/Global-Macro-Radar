from screen_a_share import attach_latest_financial_fields
import pandas as pd
from datetime import date
df = pd.DataFrame([{"股票代码": "300308", "股票简称": "中际旭创", "最新价": 100, "PE": 20, "PB": 3, "总市值": 1e11}])
res, _, _ = attach_latest_financial_fields(df, as_of_date=date.today())
print("4个季度连续加速增长:", res["4个季度连续加速增长"].iloc[0])
print("净利润同比:", res["净利润-同比增长"].iloc[0])
