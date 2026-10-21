import urllib.request
import re

def parse_gf_specific_quote(symbol):
    url = f"https://www.google.com/finance/quote/{symbol}"
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9'
    })
    try:
        html = urllib.request.urlopen(req, timeout=5).read().decode('utf-8')
        
        # Google Finance ticker pattern in JSON:
        # ["<SYMBOL>", "<EXCHANGE>"] followed by ... [price, chg_amt, chg_ratio, ...],null,prev_close
        # Extract quote ticker symbol part (e.g. NQW00 from NQW00:CME_EMINIS or NDX from NDX:INDEXNASDAQ)
        sym_code = symbol.split(':')[0].replace('.', r'\.')
        
        # Regex search specifically after symbol_code:
        pattern = r'\"' + sym_code + r'\"[^\]]*\],\[([0-9\.]+),([+-]?[0-9\.]+),([+-]?[0-9\.]+),\d+,\d+,\d+\],null,([0-9\.]+)'
        m = re.search(pattern, html)
        if m:
            price = float(m.group(1))
            chg_amt = float(m.group(2))
            chg_ratio = float(m.group(3))
            prev_close = float(m.group(4))
            calc_pct = ((price - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0
            return {
                'symbol': symbol,
                'price': price,
                'chg_amt': chg_amt,
                'calc_pct': round(calc_pct, 4),
                'prev_close': prev_close
            }
            
        # Alternative pattern: match the main card JSON array right before window.AF_initDataCallback
        # Google Finance main price is always rendered in the primary quote JSON block:
        # e.g., [price, chg_amt, chg_ratio, ...],null,prev_close
        m_main = re.findall(r'\[([0-9\.]+),([+-]?[0-9\.]+),([+-]?[0-9\.]+),\d+,\d+,\d+\],null,([0-9\.]+)', html)
        if m_main:
            # Check for the main card array (usually where price matches the target ticker magnitude)
            for price_str, chg_str, ratio_str, prev_str in m_main:
                price = float(price_str)
                prev_close = float(prev_str)
                chg_amt = float(chg_str)
                if price > 0 and prev_close > 0:
                    calc_pct = ((price - prev_close) / prev_close) * 100.0
                    return {
                        'symbol': symbol,
                        'price': price,
                        'chg_amt': chg_amt,
                        'calc_pct': round(calc_pct, 4),
                        'prev_close': prev_close
                    }

        return {'symbol': symbol, 'error': 'Quote pattern not found'}
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
    res = parse_gf_specific_quote(sym)
    print(f"{name:25s} ({sym:18s}): {res}")
