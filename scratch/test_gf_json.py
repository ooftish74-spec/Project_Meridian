import urllib.request
import re
import json

def parse_gf_json(symbol):
    url = f"https://www.google.com/finance/quote/{symbol}"
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9'
    })
    try:
        html = urllib.request.urlopen(req, timeout=5).read().decode('utf-8')
        
        # Regex pattern matching Google Finance quote JSON structure:
        # e.g., ["NQW00","CME_EMINIS"],"E-mini NASDAQ 100",4,"USD",[price, change, change_pct, ...],null,prev_close
        # Or more generally: \[[0-9\.]+,[+-]?[0-9\.]+,[+-]?[0-9\.]+,[0-9]+,[0-9]+,[0-9]+\],null,[0-9\.]+
        
        # Search for pattern: [price, change_amt, change_pct, ...],null,prev_close
        m = re.search(r'\[([0-9\.]+),([+-]?[0-9\.]+),([+-]?[0-9\.]+),\d+,\d+,\d+\],null,([0-9\.]+)', html)
        if m:
            price = float(m.group(1))
            chg_amt = float(m.group(2))
            chg_pct_ratio = float(m.group(3)) # ratio or percentage
            prev_close = float(m.group(4))
            
            # If chg_pct_ratio is small decimal (e.g. 0.0004 or 0.1034), calculate percent or ratio
            # Let's check: (price - prev_close) / prev_close * 100
            calc_pct = ((price - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0
            return {
                'symbol': symbol,
                'price': price,
                'chg_amt': chg_amt,
                'calc_pct': round(calc_pct, 4),
                'prev_close': prev_close
            }
        
        # Fallback search for any array with [price, chg_amt, ...]
        m2 = re.search(r'\"' + re.escape(symbol.split(':')[0]) + r'\"[^\]]*\[([0-9\.]+),([+-]?[0-9\.]+),([+-]?[0-9\.]+)', html)
        if m2:
            return {'symbol': symbol, 'price': float(m2.group(1)), 'chg_amt': float(m2.group(2)), 'chg_pct': float(m2.group(3))}

        return {'symbol': symbol, 'error': 'Pattern not found'}
    except Exception as e:
        return {'symbol': symbol, 'error': str(e)}

targets = [
    ('VIX', 'VIX:INDEXCBOE'),
    ('S&P 500 Index', '.INX:INDEXSP'),
    ('Nasdaq Composite', 'COMP:INDEXNASDAQ'),
    ('Nasdaq 100 Index', 'NDX:INDEXNASDAQ'),
    ('E-Mini Nasdaq 100 Futures', 'NQW00:CME_EMINIS'),
    ('E-Mini S&P 500 Futures', 'ESW00:CME_EMINIS'),
    ('E-Mini Dow Futures', 'YMW00:CBOT'),
    ('EWY ETF', 'EWY:NYSEARCA')
]

for name, sym in targets:
    res = parse_gf_json(sym)
    print(f"{name:25s} ({sym:18s}): {res}")
