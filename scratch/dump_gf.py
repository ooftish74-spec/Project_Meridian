import urllib.request
import re

url = "https://www.google.com/finance/quote/VIX:INDEXCBOE"
req = urllib.request.Request(url, headers={
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9'
})
html = urllib.request.urlopen(req, timeout=5).read().decode('utf-8')
print("HTML length:", len(html))

# Let's find all occurrences of numbers with decimal points
numbers = re.findall(r'>([0-9,]+\.[0-9]{2})<', html)
print("First 20 span numbers:", numbers[:20])

# Search for % signs
pcts = re.findall(r'([+-]?[0-9,]+\.[0-9]{2}%)', html)
print("First 10 pcts:", pcts[:10])
