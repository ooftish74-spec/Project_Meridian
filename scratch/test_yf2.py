import requests
import json

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}
try:
    url = "https://query2.finance.yahoo.com/v8/finance/chart/KRW=X"
    r = requests.get(url, headers=headers, timeout=5)
    print("Yahoo:", r.status_code, r.text[:100])
except Exception as e:
    print("Yahoo failed:", e)
