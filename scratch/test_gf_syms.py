import urllib.request
import re

symbols = [
    ('DXY_1', 'DXY:INDEXCBOE'),
    ('DXY_2', 'DX-Y.NYB:INDEXCBOE'),
    ('DXY_3', '.DXY:INDEXCBOE'),
    ('WTI_1', 'CL1:NYMEX'),
    ('WTI_2', 'CL1:COMEX'),
    ('WTI_3', 'CL:NYMEX'),
    ('GOLD_1', 'GC1:COMEX'),
    ('GOLD_2', 'GC:COMEX'),
    ('USDKRW_1', 'USD-KRW'),
    ('USDKRW_2', 'USDKRW:CURRENCY')
]

for name, symbol in symbols:
    url = f"https://www.google.com/finance/quote/{symbol}"
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9'
    })
    try:
        html = urllib.request.urlopen(req, timeout=5).read().decode('utf-8')
        sym_code = symbol.split(':')[0].replace('.', r'\.')
        m1 = re.search(r'\[\"' + sym_code + r'\"\,\"[^\"]*\"\]\,[^\,]*\,[^\,]*\,[^\,]*\,\[([0-9\.]+)\,([+-]?[0-9\.]+)\,([+-]?[0-9\.]+)', html)
        if m1:
            print(f"{name:12s} ({symbol:18s}): MATCH -> {m1.group(1)}, chg={m1.group(2)}")
        else:
            # Look for ANY callback matching price
            m_cb = re.search(r'\[\"([A-Z0-9\.\=\-]+)\"\,\"[^\"]*\"\]\,[^\,]*\,[^\,]*\,[^\,]*\,\[([0-9\.]+)\,([+-]?[0-9\.]+)', html)
            if m_cb:
                print(f"{name:12s} ({symbol:18s}): CB MATCH -> {m_cb.group(1)} price={m_cb.group(2)}")
            else:
                print(f"{name:12s} ({symbol:18s}): NO MATCH")
    except Exception as e:
        print(f"{name:12s} ({symbol:18s}): ERROR {e}")
