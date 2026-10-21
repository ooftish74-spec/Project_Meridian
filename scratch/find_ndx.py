import urllib.request
import re

url = "https://www.google.com/finance/quote/NDX:INDEXNASDAQ"
req = urllib.request.Request(url, headers={
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9'
})
html = urllib.request.urlopen(req, timeout=5).read().decode('utf-8')

# Search for "NASDAQ-100" or "NDX" in html
idx = html.find('NASDAQ 100')
if idx == -1:
    idx = html.find('Nasdaq-100')
if idx == -1:
    idx = html.find('NDX')

print("Found index at:", idx)
if idx != -1:
    print(html[idx-100:idx+500])
