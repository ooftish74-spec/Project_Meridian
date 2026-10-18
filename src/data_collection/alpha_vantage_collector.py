from __future__ import annotations
from pathlib import Path
_DATA_DIR = Path('data')
'[Phase 57: Alpha Vantage Macro Sentiment] 글로벌 매크로 감성 수집기.\n\nAlpha Vantage NEWS_SENTIMENT API를 통해 글로벌 매크로 시장의 종합 감성 점수를\n수집하여 data/sentiment/global_macro_sentiment.json에 저장한다.\n\n주의: 무료 API 한도 25회/일 → daily_pipeline에서 단 1회만 호출\n'
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
import requests
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.api_key_manager import get_key
from src.utils.file_ops import atomic_write_json
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger('alpha_vantage_collector')

def collect_global_macro_sentiment() -> dict:
    """✎ Alpha Vantage NEWS_SENTIMENT API 호출 → 글로벌 매크로 감성 점수 수집.

    Returns:
        {
            'average_sentiment_score': float,   # -1~+1 (음수=부정적, 양수=금정적)
            'article_count':           int,     # 처리된 뉴스 건수
            'source':                  str,
            'timestamp':               str,
        }
        API 실패 또는 한도 초과 시 빈 dict {} 반환.
    """
    from src.utils.credential_manager import CredentialManager
    api_key = CredentialManager().read_from_env('ALPHA_VANTAGE_API_KEY') or ''
    if not api_key:
        logger.error('  ❌ ALPHA_VANTAGE_API_KEY가 환경 변수에 없습니다.')
        return {}
    url = f'https://www.alphavantage.co/query?function=NEWS_SENTIMENT&topics=economy_macro&apikey={api_key}'
    try:
        logger.info('  🌍 [Phase 57] Alpha Vantage 매크로 감성 데이터 수집 중...')
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        data = response.json()
        if 'Information' in data:
            logger.warning(f"  ⚠️ Alpha Vantage API 한도 초과/제한: {data['Information']}")
            return {}
        if 'Note' in data:
            logger.warning(f"  ⚠️ Alpha Vantage Note: {data['Note']}")
            return {}
        feed = data.get('feed', [])
        if not feed:
            logger.warning('  ⚠️ 가져온 뉴스가 없습니다.')
            return {}
        scores = [float(item['overall_sentiment_score']) for item in feed[:50] if 'overall_sentiment_score' in item]
        avg_score = round(sum(scores) / len(scores), 4) if scores else 0.0
        result = {'average_sentiment_score': avg_score, 'article_count': len(feed), 'scored_count': len(scores), 'source': 'Alpha Vantage NEWS_SENTIMENT', 'timestamp': datetime.now().isoformat()}
        out_dir = ROOT / 'data' / 'sentiment'
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / 'global_macro_sentiment.json'
        atomic_write_json(out_path, result, indent=2, ensure_ascii=False)
        logger.info(f"  ✅ [Phase 57] 글로벌 매크로 감성 점수: {avg_score:+.4f} (기사 {len(feed)}건 / 점수 {len(scores)}건) → {out_path}")
        return result
    except requests.exceptions.Timeout:
        logger.error('  ❌ Alpha Vantage 타임아웃 (20s 초과)', exc_info=True)
        return {}
    except requests.exceptions.HTTPError as _he:
        logger.error(f'  ❌ Alpha Vantage HTTP 오류: {_he}', exc_info=True)
        return {}
    except Exception as e:
        logger.error(f'  ❌ [Phase 57] Alpha Vantage NEWS_SENTIMENT 에러: {e}', exc_info=True)
        return {}

def collect_us_daily_ohlcv(ticker: str) -> 'pd.DataFrame':
    """Alpha Vantage TIME_SERIES_DAILY_ADJUSTED API를 통해 미국 주식 일봉 수집."""
    from src.utils.credential_manager import CredentialManager
    import pandas as pd
    
    api_key = CredentialManager().read_from_env('ALPHA_VANTAGE_API_KEY') or ''
    if not api_key:
        logger.error('  ❌ ALPHA_VANTAGE_API_KEY 누락.')
        return None
        
    # AV uses outputsize=full for complete history (up to 20 years). We just need recent data.
    url = f'https://www.alphavantage.co/query?function=TIME_SERIES_DAILY_ADJUSTED&symbol={ticker}&outputsize=full&apikey={api_key}'
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        if 'Time Series (Daily)' not in data:
            if 'Information' in data:
                logger.warning(f"  ⚠️ Alpha Vantage API 제한: {data['Information']}")
            elif 'Note' in data:
                logger.warning(f"  ⚠️ Alpha Vantage Note: {data['Note']}")
            else:
                logger.warning(f"  ⚠️ Alpha Vantage 데이터 없음 ({ticker})")
            return None
            
        ts = data['Time Series (Daily)']
        records = []
        for date_str, v in ts.items():
            records.append({
                'date': date_str,
                'open': float(v['1. open']),
                'high': float(v['2. high']),
                'low': float(v['3. low']),
                'close': float(v['5. adjusted close']), # use adjusted close for consistency
                'volume': int(v['6. volume']),
                'split_coefficient': float(v.get('8. split coefficient', 1.0))
            })
            
        df = pd.DataFrame(records)
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()
        # Drop rows where any of OHLCV are NaN or zero (except volume can be zero, but usually isn't)
        df = df.dropna(subset=['open', 'high', 'low', 'close', 'volume'])
        
        # We only need the last ~180 days max, but typically we just keep the last 1000 days or so 
        if len(df) > 1000:
            df = df.tail(1000)
            
        return df
    except Exception as e:
        logger.error(f'  ❌ [Alpha Vantage] {ticker} 주식 데이터 수집 실패: {e}', exc_info=True)
        return None

def collect_fx_daily_ohlcv(from_sym: str, to_sym: str) -> 'pd.DataFrame':
    """Alpha Vantage FX_DAILY API를 통해 환율 일봉 수집."""
    from src.utils.credential_manager import CredentialManager
    import pandas as pd
    
    api_key = CredentialManager().read_from_env('ALPHA_VANTAGE_API_KEY') or ''
    if not api_key:
        logger.error('  ❌ ALPHA_VANTAGE_API_KEY 누락.')
        return None
        
    url = f'https://www.alphavantage.co/query?function=FX_DAILY&from_symbol={from_sym}&to_symbol={to_sym}&outputsize=full&apikey={api_key}'
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        if 'Time Series FX (Daily)' not in data:
            logger.warning(f"  ⚠️ Alpha Vantage FX_DAILY 데이터 없음 ({from_sym}/{to_sym})")
            return None
            
        ts = data['Time Series FX (Daily)']
        records = []
        for date_str, v in ts.items():
            records.append({
                'date': date_str,
                'open': float(v['1. open']),
                'high': float(v['2. high']),
                'low': float(v['3. low']),
                'close': float(v['4. close']),
                'volume': 0
            })
            
        df = pd.DataFrame(records)
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()
        df = df.dropna(subset=['close'])
        
        if len(df) > 1000:
            df = df.tail(1000)
            
        return df
    except Exception as e:
        logger.error(f'  ❌ [Alpha Vantage] {from_sym}/{to_sym} 환율 수집 실패: {e}', exc_info=True)
        return None

def collect_econ_indicator_daily_ohlcv(function: str, maturity: str = None) -> 'pd.DataFrame':
    """Alpha Vantage 매크로/원자재 API (TREASURY_YIELD, WTI 등)를 OHLCV 포맷으로 변환."""
    from src.utils.credential_manager import CredentialManager
    import pandas as pd
    
    api_key = CredentialManager().read_from_env('ALPHA_VANTAGE_API_KEY') or ''
    if not api_key:
        logger.error('  ❌ ALPHA_VANTAGE_API_KEY 누락.')
        return None
        
    mat_param = f"&maturity={maturity}" if maturity else ""
    url = f'https://www.alphavantage.co/query?function={function}&interval=daily{mat_param}&apikey={api_key}'
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        if 'data' not in data:
            logger.warning(f"  ⚠️ Alpha Vantage {function} 데이터 없음")
            return None
            
        records = []
        for item in data['data']:
            if item['value'] == '.':
                continue
            val = float(item['value'])
            records.append({
                'date': item['date'],
                'open': val,
                'high': val,
                'low': val,
                'close': val,
                'volume': 0
            })
            
        df = pd.DataFrame(records)
        if df.empty:
            return None
            
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()
        df = df.dropna(subset=['close'])
        
        if len(df) > 1000:
            df = df.tail(1000)
            
        return df
    except Exception as e:
        logger.error(f'  ❌ [Alpha Vantage] {function} 데이터 수집 실패: {e}', exc_info=True)
        return None

def _fetch_yahoo_chart_quote(symbol: str) -> Optional[dict]:
    """Yahoo Finance Direct Chart REST API를 통해 지수/선물/환율의 실제 레벨 및 1일 등락률 조회."""
    import urllib.request
    import urllib.parse
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=5d&interval=1d'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.loads(r.read().decode('utf-8'))['chart']['result'][0]
            closes = [float(c) for c in d.get('indicators', {}).get('quote', [{}])[0].get('close', []) if c is not None and float(c) > 0]
            p = float(d.get('meta', {}).get('regularMarketPrice') or (closes[-1] if closes else 0.0))
            if p <= 0:
                return None
            prev = closes[-2] if len(closes) >= 2 else float(d.get('meta', {}).get('previousClose') or p)
            chg_1d = round((p / prev - 1.0) * 100.0, 2) if prev > 0 else 0.0
            return {'price': round(p, 4), 'change_1d': chg_1d}
    except Exception as e:
        logger.debug(f"  [YahooDirect] {symbol} 조회 스킵: {e}")
        return None


def collect_global_macro(symbols: list) -> dict:
    """[Phase 1] 글로벌 매크로 지표 수집 (지수/선물/환율 실제 레벨 보장 — ETF 가격 오염 차단)."""
    from src.utils.credential_manager import CredentialManager
    from src.utils.google_finance_collector import GoogleFinanceCollector
    import time

    api_key = CredentialManager().read_from_env('ALPHA_VANTAGE_API_KEY') or ''
    results = {}

    # 실제 지수/선물/금리/환율 심볼은 ETF 가격(SPY, QQQ, SOXX, GLD, UUP 등)으로 절대 덮어쓰지 않고
    # Yahoo Chart Direct REST / Google Finance에서 실제 지수 레벨과 1D 등락률을 우선 수집한다.
    DIRECT_INDEX_SYMBOLS = {
        '^GSPC', '^IXIC', '^DJI', '^SOX', '^VIX',
        '^TNX', '^FVX', '^TYX', 'DX-Y.NYB',
        'CL=F', 'GC=F', 'SI=F', 'HG=F',
        'KRW=X', 'USDJPY=X', 'EURUSD=X',
        '^KS11', '^KQ11', '^TWII', '^N225', '^HSI',
    }

    mapping = {
        'CL=F': ('WTI', ''),
        'HG=F': ('COPPER', ''),
        '^TNX': ('YIELD', '10year'),
        '^FVX': ('YIELD', '5year'),
        '^TYX': ('YIELD', '30year'),
        'KRW=X': ('FX', 'USD/KRW'),
        'USDJPY=X': ('FX', 'USD/JPY'),
        'EURUSD=X': ('FX', 'EUR/USD'),
    }

    for symbol in symbols:
        try:
            # [0] 지수/선물/환율/금리: Yahoo Direct Chart REST -> GoogleFinanceCollector 우선 조회
            if symbol in DIRECT_INDEX_SYMBOLS or symbol.startswith('^') or symbol.endswith('=F') or symbol.endswith('=X'):
                yd = _fetch_yahoo_chart_quote(symbol)
                if yd and yd.get('price', 0) > 0:
                    results[symbol] = yd
                    logger.info(f"  🌍 [GlobalMacro] {symbol} 수집 성공: {yd['price']} ({yd['change_1d']:+.2f}%)")
                    continue
                gf = GoogleFinanceCollector.get_quote_details(symbol)
                if gf and gf.get('price', 0) > 0:
                    results[symbol] = {'price': float(gf['price']), 'change_pct': float(gf.get('change_pct', 0.0)), 'change_1d': float(gf.get('change_pct', 0.0))}
                    logger.info(f"  🌍 [GoogleFinance] {symbol} 수집 성공: {gf['price']} ({gf.get('change_pct', 0.0):+.2f}%)")
                    continue
                if symbol not in mapping:
                    logger.debug(f"  [GlobalMacro] {symbol} 실시간 지수 소스 응답 없음 — 스킵")
                    continue

            if not api_key:
                continue

            route, param = mapping.get(symbol, ('ETF', symbol))
            price = None

            # [1] 실제 US 주식/ETF 라우팅 (EWY, FLKR, FXI 등)
            if route == 'ETF':
                url = f'https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={param}&apikey={api_key}'
                resp = requests.get(url, timeout=10).json()
                if 'Global Quote' in resp and '05. price' in resp['Global Quote']:
                    price = float(resp['Global Quote']['05. price'])
                    chg_pct = float(resp['Global Quote'].get('10. change percent', '0%').replace('%', ''))
                    results[symbol] = {'price': price, 'change_1d': chg_pct}
                    logger.info(f"  🌍 [Alpha Vantage] {symbol} 수집 성공: {price} ({chg_pct:+.2f}%)")
                else:
                    yd = _fetch_yahoo_chart_quote(symbol)
                    if yd and yd.get('price', 0) > 0:
                        results[symbol] = yd
                        logger.info(f"  🌍 [GlobalMacro Fallback] {symbol} 수집 성공: {yd['price']} ({yd['change_1d']:+.2f}%)")

            # [2] WTI (원유) 라우팅
            elif route == 'WTI':
                url = f'https://www.alphavantage.co/query?function=WTI&interval=daily&apikey={api_key}'
                resp = requests.get(url, timeout=10).json()
                if 'data' in resp and len(resp['data']) > 0:
                    price = float(resp['data'][0]['value'])
                    results[symbol] = {'price': price, 'change_1d': 0.0}
                    logger.info(f"  🌍 [Alpha Vantage] {symbol} (WTI) 수집 성공: {price}")

            # [3] COPPER (구리) 라우팅
            elif route == 'COPPER':
                url = f'https://www.alphavantage.co/query?function=COPPER&interval=monthly&apikey={api_key}'
                resp = requests.get(url, timeout=10).json()
                if 'data' in resp and len(resp['data']) > 0 and resp['data'][0]['value'] != '.':
                    price = float(resp['data'][0]['value'])
                    results[symbol] = {'price': price, 'change_1d': 0.0}
                    logger.info(f"  🌍 [Alpha Vantage] {symbol} (COPPER) 수집 성공: {price}")

            # [4] 국채 금리 라우팅
            elif route == 'YIELD':
                url = f'https://www.alphavantage.co/query?function=TREASURY_YIELD&interval=daily&maturity={param}&apikey={api_key}'
                resp = requests.get(url, timeout=10).json()
                if 'data' in resp and len(resp['data']) > 0:
                    price = float(resp['data'][0]['value'])
                    results[symbol] = {'price': price, 'change_1d': 0.0}
                    logger.info(f"  🌍 [Alpha Vantage] {symbol} ({param} Yield) 수집 성공: {price}")

            # [5] 환율 (FX) 라우팅
            elif route == 'FX':
                base, quote = param.split('/')
                url = f'https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE&from_currency={base}&to_currency={quote}&apikey={api_key}'
                resp = requests.get(url, timeout=10).json()
                if 'Realtime Currency Exchange Rate' in resp:
                    price = float(resp['Realtime Currency Exchange Rate']['5. Exchange Rate'])
                    results[symbol] = {'price': price, 'change_1d': 0.0}
                    logger.info(f"  🌍 [Alpha Vantage] {symbol} (FX {param}) 수집 성공: {price}")

            time.sleep(0.1)

        except Exception as e:
            logger.error(f'  ❌ [Alpha Vantage] {symbol} 수집 실패: {e}', exc_info=True)

    return results

def collect_options_pcr(symbol: str='SPY') -> float:
    """[Phase 2] Alpha Vantage HISTORICAL_OPTIONS API를 활용해 Put-Call Ratio(PCR) 수집.

    현재 Alpha Vantage 무료/일반 Premium 플랜에서 HISTORICAL_OPTIONS는 추가 결제가 필요할 수 있습니다.
    호출 실패 시 Fallback으로 1.0(중립)을 반환합니다.
    """
    api_key = get_key('ALPHA_VANTAGE_API_KEY')
    if not api_key:
        return 1.0
    url = f'https://www.alphavantage.co/query?function=HISTORICAL_OPTIONS&symbol={symbol}&apikey={api_key}'
    try:
        resp = requests.get(url, timeout=25)
        if resp.status_code == 200:
            data = resp.json()
            if 'data' in data and isinstance(data['data'], list):
                puts = sum(int(c.get('volume', 0)) for c in data['data'] if c.get('type') == 'put')
                calls = sum(int(c.get('volume', 0)) for c in data['data'] if c.get('type') == 'call')
                if calls > 0:
                    pcr = puts / calls
                    logger.info(f"  📊 [Alpha Vantage] {symbol} Options PCR 수집 성공: {pcr:.2f}")
                    return round(pcr, 4)
            logger.warning(f"  ⚠️ [Alpha Vantage] Options PCR 데이터 없음 (포맷 변경 또는 데이터 부재)")
    except Exception as e:
        logger.error(f'  ❌ [Alpha Vantage] Options PCR 수집 실패: {e}', exc_info=True)
    return 1.0

def collect_news_sentiment() -> float:
    """[Phase 3] Alpha Vantage NEWS_SENTIMENT API를 활용해 매크로/시장 센티먼트 수집.
    
    Topics: economy_macro, financial_markets
    
    Returns:
        float: 평균 센티먼트 점수 (보통 -0.35 ~ 0.35 사이, 낮을수록 부정적)
    """
    api_key = get_key('ALPHA_VANTAGE_API_KEY')
    if not api_key:
        return 0.0
    url = f'https://www.alphavantage.co/query?function=NEWS_SENTIMENT&topics=economy_macro,financial_markets&limit=50&apikey={api_key}'
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if 'feed' in data and len(data['feed']) > 0:
                scores = []
                for item in data['feed']:
                    score = float(item.get('overall_sentiment_score', 0.0))
                    scores.append(score)
                avg_score = sum(scores) / len(scores) if scores else 0.0
                import json
                out_dir = _DATA_DIR / 'sentiment'
                out_dir.mkdir(parents=True, exist_ok=True)
                out_file = out_dir / 'global_macro_sentiment.json'
                result = {'average_sentiment_score': round(avg_score, 4), 'article_count': len(scores), 'last_updated': datetime.now().isoformat()}
                atomic_write_json(out_file, result, indent=2, ensure_ascii=False)
                logger.info(f"  📰 [Alpha Vantage] News Sentiment 수집 성공: {avg_score:+.3f} ({len(scores)} articles)")
                return round(avg_score, 4)
            else:
                logger.warning('  ⚠️ [Alpha Vantage] News Sentiment 데이터 없음.')
    except Exception as e:
        logger.error(f'  ❌ [Alpha Vantage] News Sentiment 수집 실패: {e}', exc_info=True)
    return 0.0
if __name__ == '__main__':
    res = collect_global_macro_sentiment()
    if res:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print('[⚠️] 수집 실패 또는 빈 결과 — API 키 확인 후 재시도')
        sys.exit(1)