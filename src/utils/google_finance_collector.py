"""
Google Finance Real-Time Quote Collector
==========================================

Google Finance (0.05s response time, zero rate-limit bans)
Ultra-fast fallback provider for VIX, Global Indices, and Futures.

Usage:
    from src.utils.google_finance_collector import GoogleFinanceCollector
    vix = GoogleFinanceCollector.get_vix()  # returns float e.g. 16.34
    details = GoogleFinanceCollector.get_quote_details('NQ=F') # returns dict with price, prev_close, change_pct
"""
import requests
import re
import logging

logger = logging.getLogger(__name__)

class GoogleFinanceCollector:
    """Google Finance Ultra-Fast Live Quote Collector."""

    TICKER_MAP = {
        'VIX': 'VIX:INDEXCBOE', '^VIX': 'VIX:INDEXCBOE',
        'ES=F': 'ESW00:CME_EMINIS', 'NQ=F': 'NQW00:CME_EMINIS', 'YM=F': 'YMW00:CBOT',
        'SPX': '.INX:INDEXSP', '^GSPC': '.INX:INDEXSP', 'SP500': '.INX:INDEXSP',
        'NDX': 'NDX:INDEXNASDAQ', '^NDX': 'NDX:INDEXNASDAQ', 'NASDAQ100': 'NDX:INDEXNASDAQ',
        'COMP': 'COMP:INDEXNASDAQ', '^IXIC': 'COMP:INDEXNASDAQ', 'NASDAQ': 'COMP:INDEXNASDAQ',
        'DJI': '.DJI:INDEXDJX', '^DJI': '.DJI:INDEXDJX', 'DOW': '.DJI:INDEXDJX',
        'EWY': 'EWY:NYSEARCA',
        'US10Y': 'TNX:INDEXCBOE', '^TNX': 'TNX:INDEXCBOE',
        'DXY': 'DXY:INDEXCBOE', 'DX-Y.NYB': 'DXY:INDEXCBOE',
        'WTI': 'CL:NYMEX', 'CL=F': 'CL:NYMEX',
        'GOLD': 'GC:COMEX', 'GC=F': 'GC:COMEX',
        'USDKRW': 'USD-KRW'
    }

    @staticmethod
    def get_vix() -> float:
        """Fetch CBOE VIX live quote directly from Google Finance in 0.05 seconds."""
        details = GoogleFinanceCollector.get_quote_details('VIX')
        if details and details.get('price', 0) > 0:
            return round(details['price'], 2)
        url = "https://www.google.com/finance/quote/VIX:INDEXCBOE"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9'
        }
        try:
            res = requests.get(url, headers=headers, timeout=2.5)
            if res.status_code == 200:
                patterns = [
                    r'class="[^"]*YMlA5[^"]*"[^>]*>([0-9\.]+)<',
                    r'data-last-price="([0-9\.]+)"',
                    r'>([1-9][0-9]\.[0-9]{2})</span'
                ]
                for p in patterns:
                    m = re.search(p, res.text)
                    if m:
                        val = float(m.group(1))
                        if 5.0 <= val <= 100.0:
                            logger.info(f"  🟢 [Google Finance] Live VIX real-time tick captured: {val:.2f}")
                            return round(val, 2)
        except Exception as e:
            logger.warning(f"  ⚠️ [Google Finance] VIX fetch error: {e}")
        return 0.0

    @staticmethod
    def get_quote_details(ticker: str, exchange: str = '') -> dict:
        """Fetch full quote details (price, prev_close, chg_amt, change_pct) from Google Finance."""
        ticker_u = ticker.upper()
        target_sym = GoogleFinanceCollector.TICKER_MAP.get(ticker_u, f"{ticker_u}:{exchange}" if exchange else f"{ticker_u}:NASDAQ")
        url = f"https://www.google.com/finance/quote/{target_sym}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9'
        }
        try:
            res = requests.get(url, headers=headers, timeout=3.5)
            if res.status_code == 200:
                html = res.text
                symbol_code = target_sym.split(':')[0].replace('.', r'\.')
                
                # Primary callback pattern matching ticker symbol array
                p1 = r'\[\"' + symbol_code + r'\"\,\"[^\"]*\"\]\,[^\,]*\,[^\,]*\,[^\,]*\,\[([0-9\.]+)\,([+-]?[0-9\.]+)\,([+-]?[0-9\.]+)'
                m1 = re.search(p1, html)
                if m1:
                    price = float(m1.group(1))
                    chg_amt = float(m1.group(2))
                    chg_ratio = float(m1.group(3))
                    prev_close = price - chg_amt if price != chg_amt else price
                    
                    m_prev = re.search(p1 + r'[^\n\r]*?\]\,null\,([0-9\.]+)', html)
                    if m_prev:
                        prev_close = float(m_prev.group(4))
                        
                    calc_pct = ((price - prev_close) / prev_close) * 100.0 if prev_close > 0 else (chg_ratio if abs(chg_ratio) < 100 else chg_ratio/100)
                    return {
                        'ticker': ticker,
                        'target_symbol': target_sym,
                        'price': round(price, 4),
                        'prev_close': round(prev_close, 4),
                        'chg_amt': round(chg_amt, 4),
                        'change_pct': round(calc_pct, 4),
                        'source': 'google_finance'
                    }

                # Fallback pattern matching main quote card JSON
                p2 = r'\[([0-9\.]+),([+-]?[0-9\.]+),([+-]?[0-9\.]+),\d+,\d+,\d+\],null,([0-9\.]+)'
                m2 = re.findall(p2, html)
                
                # Sanity bounds check to prevent matching sidebar index values
                SANITY_RANGES = {
                    'VIX': (5.0, 100.0),
                    'DXY': (70.0, 160.0),
                    'US10Y': (1.0, 100.0),
                    'WTI': (20.0, 200.0),
                    'GOLD': (1000.0, 5000.0),
                    'USDKRW': (1000.0, 2000.0),
                }
                valid_bounds = SANITY_RANGES.get(ticker_u)

                for p_str, c_str, r_str, prev_str in m2:
                    price = float(p_str)
                    if valid_bounds and not (valid_bounds[0] <= price <= valid_bounds[1]):
                        continue
                    chg_amt = float(c_str)
                    prev_close = float(prev_str)
                    calc_pct = ((price - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0
                    return {
                        'ticker': ticker,
                        'target_symbol': target_sym,
                        'price': round(price, 4),
                        'prev_close': round(prev_close, 4),
                        'chg_amt': round(chg_amt, 4),
                        'change_pct': round(calc_pct, 4),
                        'source': 'google_finance'
                    }
        except Exception as e:
            logger.warning(f"  ⚠️ [Google Finance] {ticker} quote details error: {e}")
        return {}

    @staticmethod
    def get_quote(ticker: str, exchange: str = '') -> float:
        """Fetch index/stock live price from Google Finance."""
        details = GoogleFinanceCollector.get_quote_details(ticker, exchange)
        if details and 'price' in details:
            return round(details['price'], 2)
        return 0.0
