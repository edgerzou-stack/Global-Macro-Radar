import yfinance as yf
ticker = yf.Ticker("AAPL")
fast = ticker.fast_info
print("Last Price:", fast.last_price)
print("Previous Close:", fast.previous_close)
print("Open:", fast.open)
