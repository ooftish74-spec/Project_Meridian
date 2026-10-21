import urllib.request
import re

targets = [
    ('VIX', 'VIX:INDEXCBOE'),
    ('SPX', '.INX:INDEXSP'),
    ('NDX', 'NDX:INDEXNASDAQ'),
    ('COMP', 'COMP:INDEXNASDAQ'),
    ('DJI', '.DJI:INDEXDJX'),
    ('ES_F', 'ESW00:CME_MINI'),
    ('NQ_F', 'NQW00:CME_MINI'),
    ('YM_F', 'YMW00:CBOT'),
    ('EWY', 'EWY:NYSEARCA')
]

for name, symbol in targets:
    url = f"https://www.google.com/finance/quote/{symbol}"
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9'
    })
    try:
        res = urllib.request.urlopen(req, timeout=5)
        html = res.read().decode('utf-8')
        
        # Look for main price div/span in top section: class contains YMlKec or fx7wfc or similar
        # Google Finance puts the main price in <div class="YMlKec fx7wfc">...</div> or <span class="...">...</span>
        price = None
        m_price = re.search(r'class="[^\"]*YMlKec[^\"]*"[^>]*>\$?([0-9,]+\.?[0-9]*)<', html)
        if m_price:
            price = float(m_price.group(1).replace(',', ''))
            
        # Change percent: Google Finance puts change inside <div class="JwA6da"> or span near main price
        # e.g., <div class="JwA6da" ... aria-label="Up by 0.04%">...</div> or text like +0.04% / -0.25%
        chg_pct = None
        # Pattern 1: aria-label="Up by 0.04%" or "Down by 0.25%"
        m_aria = re.search(r'aria-label="(Up|Down) by ([0-9\.]+)%"', html)
        if m_aria:
            sign = 1.0 if m_aria.group(1) == 'Up' else -1.0
            chg_pct = sign * float(m_aria.group(2))
        else:
            # Pattern 2: sign with percent inside span/div near price
            m_pct = re.search(r'>([+-][0-9\.]+)%<', html)
            if m_pct:
                chg_pct = float(m_pct.group(1))

        print(f"{name:8s} ({symbol:16s}): price={price}, change_pct={chg_pct}%")
    except Exception as e:
        print(f"{name:8s} ({symbol:16s}): EXCEPTION {e}")
