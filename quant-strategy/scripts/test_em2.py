import asyncio
from data_provider import _fetch_em_report_async
rows = asyncio.run(_fetch_em_report_async("20240331", "RPT_DMSK_FN_BALANCE"))
if rows:
    for k, v in rows[0].items():
        print(f"{k}: {v}")
