import urllib.request
import re

targets = [
    ('VIX', 'VIX:INDEXCBOE'),
    ('DXY', 'DXY:INDEXCBOE'),
    ('US10Y', 'TNX:INDEXCBOE'),
    ('WTI', 'CL:NYMEX'),
    ('GOLD', 'GC:COMEX'),
    ('USDKRW', 'USD-KRW')
]

for name, symbol in targets:
    url = f"https://www.google.com/finance/quote/{symbol}"
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9'
    })
    try:
        html = urllib.request.urlopen(req, timeout=5).read().decode('utf-8')
        sym_code = symbol.split(':')[0].replace('.', r'\.')
        
        # Primary match
        p1 = r'\[\"' + sym_code + r'\"\,\"[^\"]*\"\]\,[^\,]*\,[^\,]*\,[^\,]*\,\[([0-9\.]+)\,([+-]?[0-9\.]+)\,([+-]?[0-9\.]+)'
        m1 = re.search(p1, html)
        if m1:
            print(f"{name:10s} ({symbol:16s}): P1 MATCH -> price={m1.group(1)}, chg={m1.group(2)}, ratio={m1.group(3)}")
        else:
            # Check DLP or YMlKec
            m_dlp = re.search(r'data-last-price="([0-9\.]+)"', html)
            print(f"{name:10s} ({symbol:16s}): P1 FAIL -> DLP={m_dlp.group(1) if m_dlp else 'None'}")
    except Exception as e:
        print(f"{name:10s} ({symbol:16s}): ERROR -> {e}")
