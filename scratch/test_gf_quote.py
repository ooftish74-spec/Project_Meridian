import urllib.request
import re

def get_gf_quote_and_change(ticker_symbol):
    url = f"https://www.google.com/finance/quote/{ticker_symbol}"
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9'
    })
    try:
        html = urllib.request.urlopen(req, timeout=5).read().decode('utf-8')
        
        # In Google Finance HTML, data is often in JS arrays like:
        # [["...","VIX",...],[price, change, change_percent...]]
        # Or in data-last-price, data-price-change-percent
        # Let's search for price and percentage in the main card block
        
        # 1. Main price:
        price = None
        # Try matching data-last-price or span class
        m_dlp = re.search(r'data-last-price="([0-9\.]+)"', html)
        if m_dlp:
            price = float(m_dlp.group(1))
        else:
            # Match class YMlKec or span with price
            m_price = re.findall(r'class="[^\"]*YMlKec[^\"]*"[^>]*>([0-9,]+\.?[0-9]*)<', html)
            if m_price:
                price = float(m_price[0].replace(',', ''))
            else:
                m_span = re.findall(r'>([0-9,]+\.[0-9]{2})</span', html)
                if m_span:
                    price = float(m_span[0].replace(',', ''))
                    
        # 2. Daily change percentage:
        chg_pct = None
        # Search for aria-label="Up by 0.04%" or "Down by 0.25%" in main price container
        # Google Finance aria-label format: aria-label="Up by 0.24%" or aria-label="Down by 0.25%"
        m_aria = re.findall(r'aria-label="(Up|Down) by ([0-9\.]+)%"', html)
        if m_aria:
            # Main card percentage is usually the first matching aria-label
            direction, val = m_aria[0]
            chg_pct = float(val) if direction == 'Up' else -float(val)
        else:
            # Search for JS array pattern: e.g. [null,null,0.24,0.0004...]
            m_pct = re.findall(r'data-price-change-percent="([+-]?[0-9\.]+)"', html)
            if m_pct:
                chg_pct = float(m_pct[0])

        return price, chg_pct
    except Exception as e:
        print(f"Error fetching {ticker_symbol}: {e}")
        return None, None

targets = [
    ('VIX', 'VIX:INDEXCBOE'),
    ('Nasdaq Composite', 'COMP:INDEXNASDAQ'),
    ('Nasdaq 100', 'NDX:INDEXNASDAQ'),
    ('S&P 500', '.INX:INDEXSP'),
    ('E-Mini Nasdaq 100 Futures', 'NQW00:CME_MINI'),
    ('E-Mini S&P 500 Futures', 'ESW00:CME_MINI'),
    ('E-Mini Dow Futures', 'YMW00:CBOT'),
    ('EWY ETF', 'EWY:NYSEARCA')
]

for name, sym in targets:
    p, c = get_gf_quote_and_change(sym)
    print(f"{name:25s} ({sym:16s}) -> Price: {p}, Change: {c}%")
