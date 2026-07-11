import requests
import os

key = "ICdpIPxUjAvS64EleyYcIBuqSphXTx1p"

print("--- Testing /api/v3/income-statement/ ---")
r1 = requests.get(f"https://financialmodelingprep.com/api/v3/income-statement/AAPL?period=quarter&limit=2&apikey={key}")
print(f"Status Code: {r1.status_code}")

print("\n--- Testing /stable/income-statement ---")
r2 = requests.get(f"https://financialmodelingprep.com/stable/income-statement?symbol=AAPL&period=quarter&limit=2&apikey={key}")
print(f"Status Code: {r2.status_code}")
