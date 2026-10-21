import urllib.request
import re

url = "https://www.google.com/finance/quote/NQW00:CME_EMINIS"
req = urllib.request.Request(url, headers={
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9'
})
html = urllib.request.urlopen(req, timeout=5).read().decode('utf-8')

# Search for AF_initDataCallback blocks or quote JSON blocks
# In Google Finance HTML, data is loaded via AF_initDataCallback
callbacks = re.findall(r'AF_initDataCallback\(\{key: \'ds:[0-9]+\', hash: \'[0-9]+\', data:(.*?), sideChannel:', html)
print(f"Found {len(callbacks)} callbacks")

for i, cb in enumerate(callbacks):
    if 'NQW00' in cb or 'NASDAQ' in cb or '30730' in cb or '30698' in cb:
        print(f"--- Callback {i} ---")
        print(cb[:1000])
