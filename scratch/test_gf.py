import urllib.request
import re

tickers = [
    ('VIX', 'VIX:INDEXCBOE'),
    ('SP500', '.INX:INDEXSP'),
    ('NASDAQ100', 'NDX:INDEXNASDAQ'),
    ('DOW', '.DJI:INDEXDJX'),
    ('ES_FUT', 'ES1:COMEX'),
    ('NQ_FUT', 'NQ1:CME_MINI'),
    ('YM_FUT', 'YM1:CBOT'),
    ('EWY', 'EWY:NYSEARCA')
]

for name, symbol in tickers:
    url = f"https://www.google.com/finance/quote/{symbol}"
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9'
    })
    try:
        html = urllib.request.urlopen(req, timeout=5).read().decode('utf-8')
        price_m = re.findall(r'class="[^"]*YMlKec[^"]*">\$?\s*([0-9\.,]+)<', html) or re.findall(r'data-last-price="([0-9\.]+)"', html)
        aria_pct = re.findall(r'aria-label="([^"]*%)"', html)
        print(f"{name} ({symbol}): price={price_m[:1] if price_m else None}, aria_pct={aria_pct[:2]}")
    except Exception as e:
        print(f"{name} ({symbol}): error {e}")
