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
            logger.warning(f'  ⚠️ Alpha Vantage API 한도 초과/제한: {data['Information']}')
            return {}
        if 'Note' in data:
            logger.warning(f'  ⚠️ Alpha Vantage Note: {data['Note']}')
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
        logger.info(f'  ✅ [Phase 57] 글로벌 매크로 감성 점수: {avg_score:+.4f} (기사 {len(feed)}건 / 점수 {len(scores)}건) → {out_path}')
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

def collect_global_macro(symbols: list) -> dict:
    """[Phase 1] Alpha Vantage를 통한 글로벌 매크로 지표 수집 (yfinance 대체).
    Yahoo Finance 호환 티커를 받아 적절한 Alpha Vantage 전용 엔드포인트로 라우팅합니다.
    """
    from src.utils.credential_manager import CredentialManager
    import time
    
    api_key = CredentialManager().read_from_env('ALPHA_VANTAGE_API_KEY') or ''
    if not api_key:
        logger.error('  ❌ ALPHA_VANTAGE_API_KEY 누락.')
        return {}
        
    results = {}
    
    # [라우팅 매핑 테이블] 
    # YF티커 -> (엔드포인트_타입, AV_심볼_또는_파라미터)
    mapping = {
        # 상품 (Commodities)
        'CL=F': ('WTI', ''),
        'GC=F': ('ETF', 'GLD'),   # AV Gold 직접조회 불가 -> GLD ETF로 우회
        'SI=F': ('ETF', 'SLV'),   # AV Silver 직접조회 불가 -> SLV ETF로 우회
        'HG=F': ('COPPER', ''),   # Copper는 monthly만 제공되나 예외적으로 조회 (daily 부족 시 실패 가능)
        
        # 금리 (Yields)
        '^TNX': ('YIELD', '10year'),
        '^FVX': ('YIELD', '5year'),
        '^TYX': ('YIELD', '30year'),
        
        # 환율 (FX)
        'KRW=X': ('FX', 'USD/KRW'),
        'USDJPY=X': ('FX', 'USD/JPY'),
        'EURUSD=X': ('FX', 'EUR/USD'),
        'DX-Y.NYB': ('ETF', 'UUP'), # 달러인덱스 대체
        
        # 지수 (Indices - AV는 지수 미지원, ETF로 대체 매핑)
        '^GSPC': ('ETF', 'SPY'),
        '^IXIC': ('ETF', 'QQQ'),
        '^DJI': ('ETF', 'DIA'),
        '^SOX': ('ETF', 'SOXX'),
        '^TWII': ('ETF', 'EWT'),
        '^N225': ('ETF', 'EWJ'),
        '^HSI': ('ETF', 'EWH'),
        '^VIX': ('UNSUPPORTED', '') # VIX는 AV에서 조회 불가. 야후 파이낸스 폴백 유도 위해 에러 리턴.
    }

    for symbol in symbols:
        try:
            route, param = mapping.get(symbol, ('ETF', symbol)) # 기본값은 ETF/주식 취급
            
            if route == 'UNSUPPORTED':
                logger.warning(f'  ⚠️ [Alpha Vantage] {symbol} (VIX 등)는 지원하지 않는 지표입니다.')
                continue
                
            price = None
            
            # [1] 주식/ETF 라우팅 (GLOBAL_QUOTE)
            if route == 'ETF':
                url = f'https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={param}&apikey={api_key}'
                resp = requests.get(url, timeout=10).json()
                if 'Global Quote' in resp and '05. price' in resp['Global Quote']:
                    price = float(resp['Global Quote']['05. price'])
                    chg_pct = float(resp['Global Quote'].get('10. change percent', '0%').replace('%', ''))
                    results[symbol] = {'price': price, 'change_1d': chg_pct}
                    logger.info(f'  🌍 [Alpha Vantage] {symbol} (Mapped: {param}) 수집 성공: {price} ({chg_pct:+.2f}%)')
                else:
                    logger.warning(f'  ⚠️ [Alpha Vantage] {symbol}({param}) 데이터 없음 또는 한도 도달.')
                    
            # [2] WTI (원유) 라우팅
            elif route == 'WTI':
                url = f'https://www.alphavantage.co/query?function=WTI&interval=daily&apikey={api_key}'
                resp = requests.get(url, timeout=10).json()
                if 'data' in resp and len(resp['data']) > 0:
                    price = float(resp['data'][0]['value'])
                    results[symbol] = {'price': price, 'change_1d': 0.0} # 과거값 계산 생략
                    logger.info(f'  🌍 [Alpha Vantage] {symbol} (WTI) 수집 성공: {price}')
                    
            # [3] COPPER (구리) 라우팅
            elif route == 'COPPER':
                url = f'https://www.alphavantage.co/query?function=COPPER&interval=monthly&apikey={api_key}'
                resp = requests.get(url, timeout=10).json()
                if 'data' in resp and len(resp['data']) > 0 and resp['data'][0]['value'] != '.':
                    price = float(resp['data'][0]['value'])
                    results[symbol] = {'price': price, 'change_1d': 0.0}
                    logger.info(f'  🌍 [Alpha Vantage] {symbol} (COPPER) 수집 성공: {price}')
                    
            # [4] 국채 금리 라우팅
            elif route == 'YIELD':
                url = f'https://www.alphavantage.co/query?function=TREASURY_YIELD&interval=daily&maturity={param}&apikey={api_key}'
                resp = requests.get(url, timeout=10).json()
                if 'data' in resp and len(resp['data']) > 0:
                    price = float(resp['data'][0]['value'])
                    results[symbol] = {'price': price, 'change_1d': 0.0}
                    logger.info(f'  🌍 [Alpha Vantage] {symbol} ({param} Yield) 수집 성공: {price}')
                    
            # [5] 환율 (FX) 라우팅
            elif route == 'FX':
                base, quote = param.split('/')
                url = f'https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE&from_currency={base}&to_currency={quote}&apikey={api_key}'
                resp = requests.get(url, timeout=10).json()
                if 'Realtime Currency Exchange Rate' in resp:
                    price = float(resp['Realtime Currency Exchange Rate']['5. Exchange Rate'])
                    results[symbol] = {'price': price, 'change_1d': 0.0}
                    logger.info(f'  🌍 [Alpha Vantage] {symbol} (FX {param}) 수집 성공: {price}')
            
            time.sleep(0.15) # Premium Burst 방지 딜레이
            
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
                    logger.info(f'  📊 [Alpha Vantage] {symbol} Options PCR 수집 성공: {pcr:.2f}')
                    return round(pcr, 4)
            logger.warning(f'  ⚠️ [Alpha Vantage] Options PCR 데이터 없음 (포맷 변경 또는 데이터 부재)')
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
                logger.info(f'  📰 [Alpha Vantage] News Sentiment 수집 성공: {avg_score:+.3f} ({len(scores)} articles)')
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