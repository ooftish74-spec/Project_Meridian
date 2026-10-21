import urllib.request
import re

url = "https://www.google.com/finance/quote/VIX:INDEXCBOE"
req = urllib.request.Request(url, headers={
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9'
})
html = urllib.request.urlopen(req, timeout=5).read().decode('utf-8')

# Search for VIX value in html: e.g., 16.34 or 15.93
pos = html.find('INDEXCBOE')
if pos != -1:
    print("Found INDEXCBOE at", pos)
    print(html[pos-100:pos+1000])
else:
    print("INDEXCBOE not found in HTML")
