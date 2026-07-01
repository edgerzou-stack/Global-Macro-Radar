import asyncio
import aiohttp

async def fetch():
    url = "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params={"secids": "0.300308", "fields": "f1,f2,f17,f18"}, headers=headers) as resp:
            data = await resp.json(content_type=None)
            print(data.get("data").get("diff"))

asyncio.run(fetch())
