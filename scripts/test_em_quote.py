import asyncio
from data_provider import _fetch_quote_snapshot_async
rows = asyncio.run(_fetch_quote_snapshot_async(["300308"]))
print(rows[0])
