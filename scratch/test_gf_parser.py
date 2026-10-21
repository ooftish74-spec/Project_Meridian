import urllib.request
import re
import json

def fetch_gf_quote_details(ticker_symbol):
    """
    Fetches real-time price, change_amount, change_pct, and prev_close from Google Finance.
    
    ticker_symbol examples:
      'VIX:INDEXCBOE'
      '.INX:INDEXSP'
      'NDX:INDEXNASDAQ'
      'COMP:INDEXNASDAQ'
      '.DJI:INDEXDJX'
      'NQW00:CME_EMINIS'
      'ESW00:CME_EMINIS'
      'YMW00:CBOT'
      'EWY:NYSEARCA'
    """
    url = f"https://www.google.com/finance/quote/{ticker_symbol}"
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9'
    })
    try:
        html = urllib.request.urlopen(req, timeout=5).read().decode('utf-8')
        
        parts = ticker_symbol.split(':')
        symbol_code = parts[0].replace('.', r'\.')
        exchange_code = parts[1] if len(parts) > 1 else ''
        
        # 1. Primary Regex Pattern matching callback array:
        # ["SYMBOL","EXCHANGE"], ... [price, chg_amt, chg_ratio, ...], null, prev_close
        pattern1 = r'\[\"' + symbol_code + r'\"\,\"[^\"]*\"\]\,[^\,]*\,[^\,]*\,[^\,]*\,\[([0-9\.]+)\,([+-]?[0-9\.]+)\,([+-]?[0-9\.]+)'
        m1 = re.search(pattern1, html)
        if m1:
            price = float(m1.group(1))
            chg_amt = float(m1.group(2))
            chg_ratio = float(m1.group(3))
            
            # Find prev close near the match
            prev_close = price - chg_amt if price != chg_amt else price
            m_prev = re.search(pattern1 + r'[^\n\r]*?\]\,null\,([0-9\.]+)', html)
            if m_prev:
                prev_close = float(m_prev.group(4))
                
            calc_pct = ((price - prev_close) / prev_close) * 100.0 if prev_close > 0 else (chg_ratio if abs(chg_ratio) < 100 else chg_ratio/100)
            return {
                'symbol': ticker_symbol,
                'price': round(price, 4),
                'chg_amt': round(chg_amt, 4),
                'change_pct': round(calc_pct, 4),
                'prev_close': round(prev_close, 4)
            }
            
        # 2. General Pattern matching any main quote card callback:
        # e.g., [price, chg_amt, chg_ratio, ...], null, prev_close
        pattern2 = r'\[([0-9\.]+),([+-]?[0-9\.]+),([+-]?[0-9\.]+),\d+,\d+,\d+\],null,([0-9\.]+)'
        matches = re.findall(pattern2, html)
        if matches:
            # First match in main card
            p_str, c_str, r_str, prev_str = matches[0]
            price = float(p_str)
            chg_amt = float(c_str)
            prev_close = float(prev_str)
            calc_pct = ((price - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0
            return {
                'symbol': ticker_symbol,
                'price': round(price, 4),
                'chg_amt': round(chg_amt, 4),
                'change_pct': round(calc_pct, 4),
                'prev_close': round(prev_close, 4)
            }
            
        return {'symbol': ticker_symbol, 'error': 'No quote pattern matched'}
    except Exception as e:
        return {'symbol': ticker_symbol, 'error': str(e)}

targets = [
    ('VIX', 'VIX:INDEXCBOE'),
    ('S&P 500 Index', '.INX:INDEXSP'),
    ('Nasdaq Composite', 'COMP:INDEXNASDAQ'),
    ('Nasdaq 100 Index', 'NDX:INDEXNASDAQ'),
    ('Dow Jones Index', '.DJI:INDEXDJX'),
    ('E-Mini Nasdaq 100 Futures', 'NQW00:CME_EMINIS'),
    ('E-Mini S&P 500 Futures', 'ESW00:CME_EMINIS'),
    ('E-Mini Dow Futures', 'YMW00:CBOT'),
    ('EWY ETF', 'EWY:NYSEARCA')
]

for name, sym in targets:
    res = fetch_gf_quote_details(sym)
    print(f"{name:25s} ({sym:18s}): {res}")
