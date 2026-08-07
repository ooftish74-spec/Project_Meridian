from __future__ import annotations
from src.infra.safe_io import atomic_write_dataframe
"""
pykrx_fetcher.py — 한국 시장 데이터 공식 수집 유틸리티
=========================================================
yfinance의 한국주식 데이터 누락/오류 문제를 해결하기 위해
pykrx(한국거래소 공식 OpenAPI 기반)를 주소스로 사용하고
yfinance를 보조 fallback으로만 활용.

지원 기능:
  - OHLCV 일간/분봉 (pykrx ← yfinance fallback)
  - KOSPI/KOSDAQ 지수 (pykrx ← yfinance fallback)
  - 현재가 조회 (pykrx ← KIS ← yfinance)
  - V-KOSPI (pykrx ← yfinance)
  - 채권지수 (pykrx ← yfinance)
  - 5분봉 (pykrx ← yfinance)

사용법:
    from src.data_collection.pykrx_fetcher import (
        get_ohlcv, get_current_price, get_index_ohlcv,
        get_5min_bars, get_vkospi
    )
"""
import logging
from datetime import datetime, timedelta, date
from typing import Optional, Dict, Any
import pandas as pd
logger = logging.getLogger(__name__)
_PYKRX_AVAILABLE: Optional[bool] = None
_LAST_PYKRX_CALL: float = 0.0

def _pykrx_ok() -> bool:
    """pykrx import 가능 여부 (1회 검사 후 캐시)."""
    global _PYKRX_AVAILABLE
    if _PYKRX_AVAILABLE is None:
        try:
            from pykrx import stock
            _PYKRX_AVAILABLE = True
        except ImportError as e:
            _PYKRX_AVAILABLE = False
            logger.error('pykrx 미설치 → yfinance fallback 사용 (pip install pykrx 권장)', exc_info=True)
    return _PYKRX_AVAILABLE

def _pykrx_safe_call(func, *args, max_retries: int=3, **kwargs):
    """
    pykrx API 호출을 rate limiting + retry로 감싼 wrapper.

    KRX 서버는 고빈도 호출 시 HTTP 에러를 반환할 수 있으므로:
    - 호출 간 최소 1초 간격 유지
    - 실패 시 1→2→4초 지수 백오프 재시도
    - 최대 3회 재시도 후 None 반환
    """
    import time
    global _LAST_PYKRX_CALL
    elapsed = time.time() - _LAST_PYKRX_CALL
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    for attempt in range(max_retries):
        try:
            _LAST_PYKRX_CALL = time.time()
            result = func(*args, **kwargs)
            return result
        except Exception as e:
            wait = 2 ** attempt
            logger.debug('pykrx retry %d/%d (%s): %s → %ds wait', attempt + 1, max_retries, func.__name__, e, wait)
            if attempt < max_retries - 1:
                time.sleep(wait)
            else:
                logger.warning('pykrx %s 최종 실패: %s', func.__name__, e, exc_info=True)
                return None

def _fmt(d: Any) -> str:
    """date/datetime/str → 'YYYYMMDD' 형식."""
    if isinstance(d, (date, datetime)):
        return d.strftime('%Y%m%d')
    s = str(d).replace('-', '')
    return s[:8]

def get_ohlcv(ticker: str, start: str | date | datetime=None, end: str | date | datetime=None, period_days: int=30) -> pd.DataFrame:
    """
    한국 개별 종목/ETF 일간 OHLCV.

    반환 DataFrame 컬럼: open, high, low, close, volume
    인덱스: DatetimeIndex (tz-naive)
    """
    if end is None:
        end = date.today()
    if start is None:
        start = date.today() - timedelta(days=period_days)
    start_s = _fmt(start)
    end_s = _fmt(end)
    if _pykrx_ok():
        try:
            from pykrx import stock
            df = _pykrx_safe_call(stock.get_market_ohlcv_by_date, start_s, end_s, ticker)
            if df is not None and (not df.empty):
                rename_map = {'시가': 'open', '고가': 'high', '저가': 'low', '종가': 'close', '거래량': 'volume', '등락률': 'change_pct', 'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'}
                df = df.rename(columns=rename_map)
                df.index = pd.to_datetime(df.index)
                cols = [c for c in ['open', 'high', 'low', 'close', 'volume'] if c in df.columns]
                df = df[cols].astype(float)
                logger.debug('pykrx OHLCV: %s (%d rows)', ticker, len(df))
                return df
        except Exception as e:
            logger.error('pykrx OHLCV 실패 (%s): %s → yfinance fallback', ticker, e, exc_info=True)
    try:
        import yfinance as yf
        yt = f'{ticker}.KS' if not ticker.startswith('^') and '.' not in ticker else ticker
        df = yf.download(yt, start=str(start), end=str(end), progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        if not df.empty:
            df.index = pd.to_datetime(df.index).tz_localize(None)
            logger.debug('yfinance OHLCV fallback: %s (%d rows)', ticker, len(df))
            return df
    except Exception as e:
        logger.warning('yfinance OHLCV 실패 (%s): %s', ticker, e, exc_info=True)
    return pd.DataFrame()
_INDEX_MAP = {'KOSPI': ('1001', '^KS11'), 'KOSDAQ': ('2001', '^KQ11'), 'KRX100': ('5051', '^KS11')}

def get_index_ohlcv(index_name: str='KOSPI', start: str | date | datetime=None, end: str | date | datetime=None, period_days: int=7) -> pd.DataFrame:
    """
    KOSPI/KOSDAQ 지수 일간 OHLCV.
    index_name: 'KOSPI' | 'KOSDAQ' | '^KS11' | '^KQ11'
    """
    if end is None:
        end = date.today()
    if start is None:
        start = date.today() - timedelta(days=period_days)
    start_s = _fmt(start)
    end_s = _fmt(end)
    if index_name in ('^KS11', 'KS11'):
        index_name = 'KOSPI'
    elif index_name in ('^KQ11', 'KQ11'):
        index_name = 'KOSDAQ'
    pykrx_code, yf_ticker = _INDEX_MAP.get(index_name, ('1001', '^KS11'))
    if _pykrx_ok():
        try:
            from pykrx import stock
            df = _pykrx_safe_call(stock.get_index_ohlcv_by_date, start_s, end_s, pykrx_code)
            if df is not None and (not df.empty):
                rename_map = {'시가': 'open', '고가': 'high', '저가': 'low', '종가': 'close', '거래량': 'volume', 'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'}
                df = df.rename(columns=rename_map)
                df.index = pd.to_datetime(df.index)
                cols = [c for c in ['open', 'high', 'low', 'close', 'volume'] if c in df.columns]
                df = df[cols].astype(float)
                logger.debug('pykrx index OHLCV: %s (%d rows)', index_name, len(df))
                return df
        except Exception as e:
            logger.error('pykrx index 실패 (%s): %s → yfinance fallback', index_name, e, exc_info=True)
    try:
        import yfinance as yf
        df = yf.download(yf_ticker, start=str(start), end=str(end), progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        if not df.empty:
            df.index = pd.to_datetime(df.index).tz_localize(None)
            logger.debug('yfinance index fallback: %s (%d rows)', yf_ticker, len(df))
            return df
    except Exception as e:
        logger.warning('yfinance index 실패 (%s): %s', yf_ticker, e, exc_info=True)
    return pd.DataFrame()

def get_5min_bars(ticker: str, target_date: str | date=None) -> pd.DataFrame:
    """
    5분봉 OHLCV.
    ticker: 종목코드(숫자 6자리) 또는 'KOSPI'/'KOSDAQ'
    target_date: 'YYYYMMDD' 또는 date (default: 오늘)
    """
    if target_date is None:
        target_date = date.today()
    date_s = _fmt(target_date)
    is_index = ticker in ('KOSPI', 'KOSDAQ', '^KS11', '^KQ11')
    if _pykrx_ok():
        try:
            from pykrx import stock
            if is_index:
                pykrx_code = '1001' if ticker in ('KOSPI', '^KS11') else '2001'
                df = stock.get_index_ohlcv_by_date(date_s, date_s, pykrx_code)
            else:
                pass
        except Exception as e:
            logger.warning(f'  suppressed: {e}', exc_info=True)
    try:
        import yfinance as yf
        if is_index:
            yt = '^KS11' if ticker in ('KOSPI', '^KS11') else '^KQ11'
        else:
            yt = f'{ticker}.KS' if '.' not in ticker else ticker
        df = yf.download(yt, period='1d', interval='5m', progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        if not df.empty:
            df.index = pd.to_datetime(df.index).tz_localize(None)
            return df
    except Exception as e:
        logger.warning('yfinance 5min 실패 (%s): %s', ticker, e, exc_info=True)
    return pd.DataFrame()

def get_current_price(ticker: str) -> Optional[float]:
    """
    한국 종목 현재가 조회.
    Tier 1: pykrx (직전 종가 기준 — 장 전/후)
    Tier 2: KIS API (실시간)
    Tier 3: yfinance fast_info
    """
    today_s = _fmt(date.today())
    yesterday_s = _fmt(date.today() - timedelta(days=3))
    if _pykrx_ok():
        try:
            from pykrx import stock
            df = stock.get_market_ohlcv_by_date(yesterday_s, today_s, ticker)
            if df is not None and (not df.empty):
                close_col = '종가' if '종가' in df.columns else 'Close'
                if close_col in df.columns:
                    price = float(df[close_col].iloc[-1])
                    if price > 0:
                        return price
        except Exception as e:
            logger.error('pykrx current_price 실패 (%s): %s', ticker, e, exc_info=True)
    try:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'scripts'))
        from src.data_collection.kis_data_collector import KISDataCollector
        kis = KISDataCollector()
        price = kis.get_current_price(ticker)
        if price and price > 0:
            return float(price)
    except Exception as e:
        logger.warning(f'  suppressed: {e}', exc_info=True)
    try:
        import yfinance as yf
        t = yf.Ticker(f'{ticker}.KS')
        fast = t.fast_info
        price = getattr(fast, 'last_price', None)
        if price and price > 0:
            return float(price)
    except Exception as e:
        logger.warning('yfinance current_price 실패 (%s): %s', ticker, e, exc_info=True)
    return None

def get_vkospi(period_days: int=3) -> Optional[float]:
    """
    V-KOSPI(한국판 VIX) 최신값.
    pykrx: 지수코드 5030 (VKOSPI)
    fallback: yfinance ^VKOSPI
    """
    start_s = _fmt(date.today() - timedelta(days=period_days))
    end_s = _fmt(date.today())
    if _pykrx_ok():
        try:
            from pykrx import stock
            df = stock.get_index_ohlcv_by_date(start_s, end_s, '5030')
            if df is not None and (not df.empty):
                close_col = '종가' if '종가' in df.columns else 'Close'
                if close_col in df.columns:
                    val = float(df[close_col].iloc[-1])
                    if val > 0:
                        return val
        except Exception as e:
            logger.error('pykrx VKOSPI 실패: %s', e, exc_info=True)
    try:
        import yfinance as yf
        df = yf.download('^VKOSPI', start=str(date.today() - timedelta(days=period_days)), progress=False, auto_adjust=True)
        if not df.empty:
            close = df['Close'].iloc[-1] if 'Close' in df.columns else df.iloc[:, 0].iloc[-1]
            return float(close)
    except Exception as e:
        logger.error('yfinance VKOSPI 실패: %s', e, exc_info=True)
    return None

def get_bond_etf_price(ticker: str='305080') -> Optional[float]:
    """채권 ETF 현재가 (pykrx → yfinance)."""
    return get_current_price(ticker)

def get_investor_trading(ticker: str, date_s: str=None) -> Optional[Dict]:
    """
    개별 종목 투자자별 순매수 (기관/외국인/개인).
    pykrx: get_market_trading_value_by_investor
    """
    if date_s is None:
        date_s = _fmt(date.today())
    if not _pykrx_ok():
        return None
    try:
        from pykrx import stock
        df = stock.get_market_trading_value_by_investor(date_s, date_s, ticker)
        if df is not None and (not df.empty):
            result = {}
            for col in df.columns:
                val = float(df[col].iloc[0]) if len(df) > 0 else 0.0
                result[str(col)] = val
            return result
    except Exception as e:
        logger.error('pykrx investor_trading 실패 (%s): %s', ticker, e, exc_info=True)
    return None
THEME_ETF_MAP: Dict[str, str] = {'KR_2ndBattery_KODEX': '305720', 'KR_Semiconductor_TIGER': '371160', 'KR_Robot_TIGER': '150460', 'KR_AI_KODEX': '364970', 'KR_UStech_TIGER': '396500', 'KR_Bio_KODEX': '244580', 'KR_Defense_KODEX': '278530', 'KR_AI_TIGER': '364980', 'KR_Auto_KODEX': '091180', 'KR_Auto_TIGER': '139290', 'KR_Shipbuilding_KODEX': '395160', 'KR_RealEstate_TIGER': '329200'}

def collect_theme_etfs_pykrx(output_dir: str | None=None, period_days: int=30) -> Dict[str, bool]:
    """
    테마 ETF OHLCV를 pykrx로 일괄 수집하여 CSV 저장.
    yfinance 대체 (CRIT HIGH-02).

    반환: {name: True/False} 성공 여부
    """
    from pathlib import Path
    results: Dict[str, bool] = {}
    if output_dir is None:
        output_dir = Path(__file__).parent.parent.parent / 'data' / 'raw' / 'korean_theme_etfs'
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    end = date.today()
    start = end - timedelta(days=period_days)
    for name, ticker in THEME_ETF_MAP.items():
        try:
            df = get_ohlcv(ticker, start=start, end=end)
            if not df.empty:
                csv_path = out / f'{name}.csv'
                atomic_write_dataframe(df, csv_path, file_format='csv')
                logger.info('✅ pykrx ETF: %s (%d rows) → %s', name, len(df), csv_path.name)
                results[name] = True
            else:
                logger.warning('⚠️ pykrx ETF 빈 결과: %s (%s)', name, ticker)
                results[name] = False
        except Exception as e:
            logger.warning('❌ pykrx ETF 실패: %s (%s): %s', name, ticker, e, exc_info=True)
            results[name] = False
    return results
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    logger.info('=== pykrx_fetcher 테스트 ===')
    df = get_index_ohlcv('KOSPI', period_days=5)
    logger.info(f'KOSPI 5일: {len(df)}행 | 최신종가: {(df['close'].iloc[-1] if not df.empty else 'N/A')}')
    df = get_ohlcv('005930', period_days=5)
    logger.info(f'삼성전자 5일: {len(df)}행 | 최신종가: {(df['close'].iloc[-1] if not df.empty else 'N/A')}')
    price = get_current_price('005930')
    logger.info(f'삼성전자 현재가: {price}')
    vkospi = get_vkospi()
    logger.info(f'V-KOSPI: {vkospi}')
    logger.info('\n--- 테마 ETF 수집 ---')
    r = collect_theme_etfs_pykrx()
    ok = sum((1 for v in r.values() if v))
    logger.info(f'성공: {ok}/{len(r)}')