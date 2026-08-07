"""
Project_First — Unified Data Collector
=========================================
전체 투자 유니버스의 데이터를 독립적으로 수집.
Project-A의 pykrx_fetcher, credential_manager를 활용하되,
수집 대상/저장 경로/스케줄은 완전 독립.

수집 대상:
  1. KR 개별종목 (KOSPI200 + KOSDAQ150 상위)
  2. 섹터 ETF (12종)
  3. 방향성 ETF (A1: 레버리지/인버스 등)
  4. 자산배분 ETF (채권, 금, 달러)
  5. 슬리브 B ETF
  6. 글로벌 시그널 (VIX, US10Y, S&P500, USDKRW, WTI, 금, 구리)
  7. 외국인/기관 수급 데이터

Usage:
    python src/data_collection/unified_collector.py               # 전체 수집
    python src/data_collection/unified_collector.py --signals      # 시그널만
    python src/data_collection/unified_collector.py --sectors      # 섹터 ETF만
    python src/data_collection/unified_collector.py --initial      # 최초 10년 백필
"""
import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, date
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from src.infra.safe_io import safe_json_write, safe_parquet_write
from typing import Dict, List, Optional
import pandas as pd
try:
    from tenacity import retry, wait_exponential, stop_after_attempt, RetryError
    _HAS_TENACITY = True
except ImportError as e:

    def retry(*args, **kwargs):

        def decorator(fn):
            return fn
        return decorator

    def wait_exponential(**kwargs):
        return None

    def stop_after_attempt(n):
        return None

    class RetryError(Exception):
        pass
    _HAS_TENACITY = False
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
from config.dynamic_config import DynamicConfig
from config.universe import Universe
from src.utils.logger import setup_logger
import re
logger = setup_logger('data_collector')
cfg = DynamicConfig()
universe = Universe()
if not _HAS_TENACITY:
    logger.warning('  [Step 2] tenacity 미설치 — API 재시도 비활성화. pip install tenacity')
_DATA_DIR = _PROJECT_ROOT / 'data' / 'kr_markets'
_SIGNAL_DIR = _PROJECT_ROOT / 'data' / 'signals'
_SIGNAL_CACHE = _PROJECT_ROOT / 'results' / 'signal_cache.json'
_KR_TICKER_RE = re.compile('^\\d{5}[0-9KLM]$')
_PYKRX_DELAY = 1.0

@retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(3), reraise=True)
def _fetch_kr_ohlcv_with_retry(pykrx_stock, start: str, end: str, ticker: str):
    """[Step 2: Tenacity] pykrx API 호출 래퍼 — 지수적 백오프 재시도."""
    time.sleep(_PYKRX_DELAY)
    try:
        df = pykrx_stock.get_market_ohlcv_by_date(start, end, ticker)
        if df is None or df.empty:
            raise ValueError(f"{ticker} returned empty pykrx data")
        return df
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"  [pykrx Retry] API failed for {ticker}: {e}")
        raise e  # Must raise to trigger Tenacity retry

def collect_kr_ohlcv(ticker: str, days: int=60, backfill: bool=False) -> Optional[pd.DataFrame]:
    """KIS API를 최우선(Primary)으로 호출하고 실패 시 pykrx로 Fallback.

    Args:
        ticker: 종목코드 (6자리)
        days: 수집 일수
        backfill: True면 10년 백필
    """
    if backfill:
        start = (datetime.now() - timedelta(days=3650)).strftime('%Y%m%d')
    else:
        start = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
    end = datetime.now().strftime('%Y%m%d')
    
    # 1. KIS API 호출 시도 (Primary)
    try:
        from src.data_collection.kis_data_collector import KISDataCollector
        kis = KISDataCollector()
        if kis._ensure_auth():
            df = kis.get_kr_daily_ohlcv(ticker, start, end)
            if df is not None and not df.empty:
                # KIS 데이터 포맷을 통일 (소문자 전환 및 이름 변경)
                df = df.reset_index()
                df.columns = [c.lower() for c in df.columns]
                rename = {'날짜': 'date', '시가': 'open', '고가': 'high', '저가': 'low', '종가': 'close', '거래량': 'volume'}
                df = df.rename(columns=rename)
                if 'date' not in df.columns:
                    df = df.rename(columns={df.columns[0]: 'date'})
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                # 🛡️ Data Poisoning Defense: Drop any row with NaNs in critical OHLCV columns
                df = df.dropna(subset=['open', 'high', 'low', 'close', 'volume'])
                logger.debug(f'  [KIS API] {ticker} 수집 성공')
                return df
    except Exception as e:
        logger.warning(f'  ⚠️ KIS API 호출 오류 ({ticker}): {e} -> pykrx Fallback 시도')
        
    # 2. pykrx 호출 (Fallback 1)
    try:
        from pykrx import stock as pykrx_stock
        df = _fetch_kr_ohlcv_with_retry(pykrx_stock, start, end, ticker)
        if df is not None and not df.empty:
            df = df.reset_index()
            df.columns = [c.lower() for c in df.columns]
            rename = {'날짜': 'date', '시가': 'open', '고가': 'high', '저가': 'low', '종가': 'close', '거래량': 'volume'}
            df = df.rename(columns=rename)
            if 'date' not in df.columns:
                df = df.rename(columns={df.columns[0]: 'date'})
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            # 🛡️ Data Poisoning Defense: Drop any row with NaNs in critical OHLCV columns
            df = df.dropna(subset=['open', 'high', 'low', 'close', 'volume'])
            logger.debug(f'  [pykrx Fallback] {ticker} 수집 성공')
            return df
    except Exception as e:
        logger.warning(f'  {ticker}: pykrx 수집 실패 — {e}')

    # 3. Naver API 호출 (Final Fallback via FinanceDataReader)
    logger.warning(f'  ⚠️ pykrx Fallback 실패 ({ticker}) -> Naver API(FDR) 최종 Fallback 시도')
    try:
        import FinanceDataReader as fdr
        # start, end formats are YYYYMMDD string. fdr accepts this format natively.
        df = fdr.DataReader(ticker, start, end)
        
        if df is None or df.empty:
            logger.error(f'  {ticker}: Naver API(FDR) 데이터 없음')
            return None
            
        df = df.reset_index()
        df.columns = [str(c).lower() for c in df.columns]
        
        rename = {'date': 'date', 'open': 'open', 'high': 'high', 'low': 'low', 'close': 'close', 'volume': 'volume'}
        df = df.rename(columns=rename)
        if 'date' not in df.columns:
            df = df.rename(columns={df.columns[0]: 'date'})
            
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        # 🛡️ Data Poisoning Defense: Drop any row with NaNs in critical OHLCV columns
        df = df.dropna(subset=['open', 'high', 'low', 'close', 'volume'])
        
        if pd.api.types.is_datetime64tz_dtype(df['date']):
            df['date'] = df['date'].dt.tz_localize(None)
            
        logger.info(f'  ✅ [Naver API Fallback] {ticker} 수집 성공')
        return df
    except Exception as naver_e:
        logger.error(f'  ❌ {ticker}: Naver API 수집 최종 실패 — {naver_e}')
        return None

def collect_all_kr_etfs(backfill: bool=False) -> Dict[str, int]:
    """전체 KR ETF 유니버스 수집.

    ★ Dynamic Parking ETF 주입:
      dynamic_parking_etfs.json 파일이 존재하면 해당 티커를 수집 대상에 추가하여
      S5 파킹 스윕에 필요한 가격 데이터 결측을 방지한다.
    """
    results = {}
    all_tickers = set()
    for etf in universe.A1_DIRECTIONAL.values():
        all_tickers.add(etf.ticker)
    for etf in universe.A2_SECTORS.values():
        all_tickers.add(etf.ticker)
    for etf in universe.ASSET_ALLOCATION.values():
        all_tickers.add(etf.ticker)
    for etf in universe.SLEEVE_B_ETFS.values():
        all_tickers.add(etf.ticker)
    _parking_cache = _PROJECT_ROOT / 'results' / 'dynamic_parking_etfs.json'
    try:
        if _parking_cache.exists():
            _parking_list = json.loads(_parking_cache.read_text())
            _parking_tickers = {p['ticker'] for p in _parking_list if p.get('ticker')}
            _new = _parking_tickers - all_tickers
            all_tickers.update(_parking_tickers)
            if _new:
                logger.info(f'  🅿️ 동적 파킹 ETF 주입: {sorted(_new)} ({len(_new)}종목 추가)')
    except Exception as _pe:
        logger.error(f'  dynamic_parking_etfs 로드 실패 (무시): {_pe}', exc_info=True)
    total = len(all_tickers)
    logger.info(f'  KR ETF 수집 시작: {total}종목')
    for i, ticker in enumerate(sorted(all_tickers), 1):
        out_file = _DATA_DIR / f'kr_{ticker}.parquet'
        if out_file.exists() and (not backfill):
            existing = pd.read_parquet(out_file)
            last_date = pd.to_datetime(existing['date']).max()
            days_since = (datetime.now() - last_date).days
            if days_since <= 0:
                logger.debug(f'  [{i}/{total}] {ticker}: 최신 (스킵)')
                results[ticker] = 0
                continue
            df = collect_kr_ohlcv(ticker, days=days_since + 5)
            if df is not None and (not df.empty):
                existing['date'] = pd.to_datetime(existing['date'])
                df['date'] = pd.to_datetime(df['date']) if 'date' in df.columns else df
                combined = pd.concat([existing, df]).drop_duplicates(subset='date', keep='last').sort_values('date')
                safe_parquet_write(combined, out_file)
                results[ticker] = len(df)
                logger.info(f'  [{i}/{total}] {ticker}: +{len(df)}행 추가')
            else:
                results[ticker] = 0
        else:
            df = collect_kr_ohlcv(ticker, backfill=backfill)
            if df is not None and (not df.empty):
                _DATA_DIR.mkdir(parents=True, exist_ok=True)
                safe_parquet_write(df, out_file)
                results[ticker] = len(df)
                logger.info(f'  [{i}/{total}] {ticker}: {len(df)}행 저장')
            else:
                results[ticker] = -1
                logger.warning(f'  [{i}/{total}] {ticker}: 수집 실패')
    return results

def collect_kr_stocks(tickers: List[str], backfill: bool=False) -> Dict[str, int]:
    """KR 개별종목 수집."""
    results = {}
    valid_tickers = [t for t in tickers if _KR_TICKER_RE.match(t)]
    total = len(valid_tickers)
    logger.info(f'  KR 개별종목 수집 시작: {total}종목')

    def _ensure_date_col(df):
        """DataFrame에 date 컬럼이 있는지 보장."""
        if df is None or df.empty:
            return df
        if 'date' in df.columns:
            return df
        if hasattr(df.index, 'dtype') and ('datetime' in str(df.index.dtype) or df.index.name in ('날짜', 'date', 'Date')):
            df = df.reset_index()
            if df.columns[0] != 'date':
                df = df.rename(columns={df.columns[0]: 'date'})
            return df
        try:
            pd.to_datetime(df.iloc[:, 0])
            df = df.rename(columns={df.columns[0]: 'date'})
        except Exception as _dt_e:
            logger.error(f'  _ensure_date_col: 첫 컬럼 날짜 변환 실패 — {_dt_e}', exc_info=True)
        return df
    for i, ticker in enumerate(valid_tickers, 1):
        out_file = _DATA_DIR / f'kr_{ticker}.parquet'
        if out_file.exists() and (not backfill):
            try:
                existing = pd.read_parquet(out_file)
                existing = _ensure_date_col(existing)
                if 'date' not in existing.columns:
                    df = collect_kr_ohlcv(ticker, backfill=backfill)
                    if df is not None and (not df.empty):
                        _DATA_DIR.mkdir(parents=True, exist_ok=True)
                        safe_parquet_write(df, out_file)
                        results[ticker] = len(df)
                    else:
                        results[ticker] = -1
                    continue
                last_date = pd.to_datetime(existing['date']).max()
                days_since = (datetime.now() - last_date).days
                if days_since <= 0:
                    results[ticker] = 0
                    continue
                df = collect_kr_ohlcv(ticker, days=days_since + 5)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
                df = collect_kr_ohlcv(ticker, days=60)
        else:
            df = collect_kr_ohlcv(ticker, backfill=backfill)
        if df is not None and (not df.empty):
            if out_file.exists() and (not backfill):
                try:
                    existing = pd.read_parquet(out_file)
                    existing = _ensure_date_col(existing)
                    df = _ensure_date_col(df)
                    merge_key = 'date' if 'date' in existing.columns else None
                    if merge_key:
                        existing[merge_key] = pd.to_datetime(existing[merge_key])
                        df[merge_key] = pd.to_datetime(df[merge_key])
                        combined = pd.concat([existing, df]).drop_duplicates(subset=merge_key, keep='last').sort_values(merge_key)
                    else:
                        combined = df
                    safe_parquet_write(combined, out_file)
                except Exception as _merge_e:
                    logger.error(f'  {ticker} parquet merge 실패 — {_merge_e}, 새 데이터로 덮어쓰기', exc_info=True)
                    safe_parquet_write(df, out_file)
            else:
                _DATA_DIR.mkdir(parents=True, exist_ok=True)
                safe_parquet_write(df, out_file)
            results[ticker] = len(df)
        else:
            results[ticker] = -1
        if i % 20 == 0:
            logger.info(f'  [{i}/{total}] 진행 중...')
    return results

def collect_global_signals(backfill: bool=False) -> Dict[str, float]:
    """[Phase 1] 글로벌 시그널 데이터 수집 (yfinance 대체 - Alpha Vantage 사용).

    주의: Alpha Vantage GLOBAL_QUOTE는 최신(당일) 값만 반환합니다.
    따라서 backfill(과거 10년치 생성)은 지원하지 않으며, 기존 캐시를 append 합니다.
    """
    from src.data_collection.alpha_vantage_collector import collect_global_macro
    _CROSS_SYNC_MAP = {'vix': ['cross_vix.parquet', 'us_vix.parquet'], 'sp500': ['cross_sp500.parquet'], 'usdkrw': ['cross_usdkrw.parquet'], 'us10y': ['cross_us10y.parquet'], 'wti': ['cross_oil_futures.parquet'], 'gold_us': ['cross_gold_futures.parquet']}
    _US_STOCKS_SYNC_MAP = {'sp500': ['SPY.parquet'], 'nasdaq': ['QQQ.parquet'], 'vix': ['^VIX.parquet'], 'ewy': ['EWY.parquet']}
    signals = {}
    today_dt = datetime.now()
    from src.data_collection.bok_ecos_collector import BOKEcosCollector
    from src.data_collection.usa_collector import USADataCollector

    BOK_MACROS = ['KR_BASE_RATE', 'KR_EXPORT', 'KR_LEI', 'KR_CEI']
    FRED_MACROS = ['UNRATE', 'FEDFUNDS', 'USSLIND']
    
    symbols_to_fetch = [v for v in universe.SIGNAL_ONLY.values() if v not in BOK_MACROS and v not in FRED_MACROS]
    logger.info('  🌍 [Alpha Vantage] 글로벌 시그널 수집 시작')
    av_data = collect_global_macro(symbols_to_fetch)
    
    bok_collector = None
    usa_collector = None
    
    for name, yf_ticker in universe.SIGNAL_ONLY.items():
        price = None
        if yf_ticker in BOK_MACROS:
            if bok_collector is None:
                bok_collector = BOKEcosCollector()
            try:
                if yf_ticker == 'KR_BASE_RATE': df = bok_collector.get_base_rate()
                elif yf_ticker == 'KR_EXPORT': df = bok_collector.get_export_index()
                elif yf_ticker == 'KR_LEI': df = bok_collector.get_leading_index()
                elif yf_ticker == 'KR_CEI': df = bok_collector.get_coincident_index()
                if df is not None and not df.empty:
                    price = float(df.iloc[-1].values[0])
                    av_data[yf_ticker] = {'price': price}
            except Exception as e:
                logger.warning(f"Failed to fetch {yf_ticker} via BOK ECOS: {e}")
        elif yf_ticker in FRED_MACROS:
            if usa_collector is None:
                usa_collector = USADataCollector()
            try:
                fred_id = usa_collector.FRED_SERIES_MAP.get(yf_ticker, yf_ticker)
                fred = usa_collector._get_fred()
                if fred is not None:
                    series = fred.get_series(fred_id)
                    if series is not None and not series.empty:
                        price = float(series.iloc[-1])
                        av_data[yf_ticker] = {'price': price}
            except Exception as e:
                logger.warning(f"Failed to fetch {yf_ticker} via FRED: {e}")
        
        if yf_ticker in av_data:
            price = av_data[yf_ticker]['price']
        else:
            try:
                import yfinance as yf
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    vix_data = yf.download(yf_ticker, period='1d', progress=False, timeout=10)
                if vix_data is not None and not vix_data.empty and len(vix_data) > 0:
                    close = vix_data['Close']
                    if hasattr(close, 'columns'): close = close.iloc[:, 0]
                    price = float(close.iloc[-1])
                    av_data[yf_ticker] = {'price': price}
            except Exception as e:
                logger.warning(f"Failed to fetch {yf_ticker} via yfinance fallback: {e}")

        if price is not None:
            df = pd.DataFrame([{'date': today_dt, 'open': price, 'high': price, 'low': price, 'close': price, 'volume': 0}])
            out_file = _SIGNAL_DIR / f'signal_{name.lower()}.parquet'
            _SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
            safe_parquet_write(df, out_file)
            name_lower = name.lower()
            for cross_file in _CROSS_SYNC_MAP.get(name_lower, []):
                cross_path = _DATA_DIR / cross_file
                try:
                    if cross_path.exists():
                        existing = pd.read_parquet(cross_path)
                        existing.columns = [c.lower() for c in existing.columns]
                        if 'date' in existing.columns:
                            combined = pd.concat([existing, df]).drop_duplicates(subset='date', keep='last').sort_values('date')
                            safe_parquet_write(combined, cross_path)
                    else:
                        safe_parquet_write(df, cross_path)
                except Exception as e:
                    logger.error(f'  cross sync {cross_file} 실패: {e}', exc_info=True)
            for us_file in _US_STOCKS_SYNC_MAP.get(name_lower, []):
                us_path = _DATA_DIR / 'us_stocks' / us_file
                try:
                    us_path.parent.mkdir(parents=True, exist_ok=True)
                    if us_path.exists():
                        existing = pd.read_parquet(us_path)
                        existing.columns = [c.lower() for c in existing.columns]
                        combined = pd.concat([existing, df])
                        combined = combined[~combined.index.duplicated(keep='last')]
                        safe_parquet_write(combined, us_path)
                    else:
                        df.set_index('date', inplace=True)
                        safe_parquet_write(df, us_path)
                except Exception as e:
                    logger.error(f'  us_stocks sync {us_file} 실패: {e}', exc_info=True)
            signals[name] = price
    try:
        from src.data_collection.alpha_vantage_collector import collect_options_pcr
        pcr = collect_options_pcr('SPY')
        signals['options_pcr'] = pcr
    except Exception as e:
        logger.error(f'  옵션 PCR 수집 실패: {e}', exc_info=True)
    try:
        from src.data_collection.alpha_vantage_collector import collect_news_sentiment
        avg_sentiment = collect_news_sentiment()
        signals['macro_sentiment'] = avg_sentiment
    except Exception as e:
        logger.error(f'  AI Sentiment 수집 실패: {e}', exc_info=True)
    _save_signal_cache(signals)
    return signals

def _save_signal_cache(signals: Dict):
    """시그널 캐시 저장."""
    try:
        existing = {}
        if _SIGNAL_CACHE.exists():
            existing = json.loads(_SIGNAL_CACHE.read_text())
        existing.update(signals)
        existing['last_update'] = datetime.now().isoformat()
        _SIGNAL_CACHE.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(_SIGNAL_CACHE, existing, indent=2, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f'  시그널 캐시 저장 실패: {e}', exc_info=True)

def collect_investor_flow() -> Dict:
    """외국인/기관 순매수 수집."""
    try:
        from pykrx import stock as pykrx_stock
    except ImportError as e:
        return {}
    end = datetime.now().strftime('%Y%m%d')
    start = (datetime.now() - timedelta(days=60)).strftime('%Y%m%d')
    flows = {}
    try:
        time.sleep(_PYKRX_DELAY)
        try:
            df = pykrx_stock.get_market_trading_volume_by_date(start, end, 'KOSPI')
        except Exception as e:
            logger.warning(f"  [SILENT_BYPASS] pykrx investor flow failed (IP block): {e}")
            return {}
        if df is not None and (not df.empty):
            out = _DATA_DIR / 'investor_flow_kospi.parquet'
            df.reset_index().to_parquet(out, index=False)
            flows['kospi_flow_saved'] = len(df)
            logger.info(f'  KOSPI 수급: {len(df)}행')
    except Exception as e:
        logger.error(f'  수급 수집 실패: {e}', exc_info=True)
    return flows

def _last_business_day() -> str:
    """최근 거래일 (주말 + 공휴일 보정 — market_calendar 통합)."""
    try:
        from src.utils.market_calendar import get_calendar
        cal = get_calendar()
        today = datetime.now().strftime('%Y%m%d')
        if cal.is_trading_day(today):
            return today
        return cal.get_previous_trading_day(today)
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        d = datetime.now()
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.strftime('%Y%m%d')

def update_stock_universe() -> List[str]:
    """KOSPI200 + KOSDAQ150 유니버스 갱신."""
    try:
        from pykrx import stock as pykrx_stock
    except ImportError as e:
        return []
    tickers = set()
    biz_day = _last_business_day()
    try:
        time.sleep(_PYKRX_DELAY)
        kospi_tickers = pykrx_stock.get_index_portfolio_deposit_file('1028', biz_day)
        if kospi_tickers is not None and len(kospi_tickers) > 0:
            tickers.update(kospi_tickers)
            logger.info(f'  KOSPI200: {len(kospi_tickers)}종목')
        time.sleep(_PYKRX_DELAY)
        kosdaq_tickers = pykrx_stock.get_index_portfolio_deposit_file('2203', biz_day)
        if kosdaq_tickers is not None and len(kosdaq_tickers) > 0:
            tickers.update(kosdaq_tickers)
            logger.info(f'  KOSDAQ150: {len(kosdaq_tickers)}종목')
    except Exception as e:
        logger.error(f'  유니버스 갱신 실패: {e}', exc_info=True)
    ticker_list = sorted(tickers)
    if ticker_list:
        out = _PROJECT_ROOT / 'results' / 'dynamic_universe.json'
        out.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(out, ticker_list, indent=2)
        logger.info(f'  유니버스: {len(ticker_list)}종목 저장')
    return ticker_list

@retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(3), reraise=True)
def _pykrx_get_etf_list_with_retry(pykrx, biz_day: str) -> list:
    """[Step 2: Tenacity] pykrx ETF 목록 조회 래퍼."""
    time.sleep(_PYKRX_DELAY)
    try:
        res = pykrx.get_etf_ticker_list(biz_day)
        if not res:
            raise ValueError(f"Empty ETF list for {biz_day}")
        return res
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"  [pykrx Retry] get_etf_ticker_list failed: {e}")
        raise e

class DataStaleException(Exception):
    pass
_GLOBAL_FALLBACK_EVENTS = []

def update_parking_etf_universe(keywords: Optional[List[str]]=None, top_n: int=5) -> List[Dict]:
    """전체 ETF 스캔 → 키워드 필터 → 거래대금 상위 top_n 선정 → 저장.

    results/dynamic_parking_etfs.json 형태:
        [{"ticker": "430740", "name": "KODEX KOFR금리액티브(합성)", "volume": 12345678}, ...]

    Args:
        keywords: 이름에 포함될 키워드 목록 (default: KOFR/CD금리/단기자금/파킹)
        top_n:    거래대금 상위 선정 개수 (기본 5)

    Returns:
        선정된 파킹 ETF 목록
    """
    if keywords is None:
        keywords = cfg.get('s5.parking_etf_keywords', ['KOFR', 'CD금리', '단기자금', '파킹'])

    def _load_last_known_good() -> List[Dict]:
        out_path = _PROJECT_ROOT / 'results' / 'dynamic_parking_etfs.json'
        if out_path.exists():
            try:
                import json
                data = json.loads(out_path.read_text())
                if data:
                    _GLOBAL_FALLBACK_EVENTS.append({'time': datetime.now().isoformat(), 'type': 'LAST_KNOWN_GOOD', 'target': 'parking_etfs', 'message': 'API 실패로 인해 마지막 캐시(dynamic_parking_etfs.json)를 로드했습니다.'})
                    logger.warning('  ⚠️ pykrx API 실패 → Last Known Good 파킹 ETF 캐시 사용')
                    return data
            except Exception as e:
                logger.error(f'  Last Known Good 캐시 로드 실패: {e}', exc_info=True)
        msg = '파킹 ETF 동적 캐시가 없으며 API도 실패했습니다. 시스템 보호를 위해 Halt를 발동합니다.'
        _GLOBAL_FALLBACK_EVENTS.append({'time': datetime.now().isoformat(), 'type': 'STALE_HALT', 'target': 'parking_etfs', 'message': msg})
        raise DataStaleException(msg)
    try:
        import FinanceDataReader as fdr
    except ImportError as e:
        logger.error('  FinanceDataReader 미설치 — 파킹 ETF 탐색 스킵', exc_info=True)
        return _load_last_known_good()
    
    biz_day = _last_business_day()
    logger.info(f'  🔍 파킹 ETF 동적 탐색 시작 (기준일={biz_day}, keywords={keywords}, source=FinanceDataReader)')
    
    try:
        df_etf = fdr.StockListing('ETF/KR')
    except Exception as e:
        logger.error(f'  ETF 전체 목록 조회 최종 실패 (FDR): {e}', exc_info=False)
        return _load_last_known_good()
        
    if df_etf is None or df_etf.empty:
        logger.warning('  ETF 목록 비어 있음 — 폴백 사용')
        return _load_last_known_good()
        
    candidates = []
    for _, row in df_etf.iterrows():
        name = str(row.get('Name', ''))
        tk = str(row.get('Symbol', ''))
        amount = int(row.get('Amount', 0)) if pd.notna(row.get('Amount')) else 0
        if name and any((kw in name for kw in keywords)):
            candidates.append({
                'ticker': tk,
                'name': name,
                'volume': amount
            })
            
    if not candidates:
        logger.warning(f'  키워드 매칭 ETF 없음 — 폴백 사용 (keywords={keywords})')
        return _load_last_known_good()
        
    logger.info(f'  ✅ 키워드 필터: {len(candidates)}종목 발견 → 거래대금 정렬')
    candidates.sort(key=lambda x: x['volume'], reverse=True)
    selected = candidates[:top_n]
    if not selected:
        logger.warning('  선정 결과 없음 — 폴백 사용')
        return _load_last_known_good()
    _save_parking_etfs(selected)
    return selected

def _save_parking_etfs(etfs_list: List[Dict]) -> None:
    """파킹 ETF 목록을 JSON 캐시에 저장합니다."""
    out_path = _PROJECT_ROOT / 'results' / 'dynamic_parking_etfs.json'
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        import json
        atomic_write_json(out_path, etfs_list, indent=2, ensure_ascii=False)
        _names_str = ', '.join((f"{s['name']}({s['ticker']})" for s in etfs_list))
        logger.info(f'  ✅ 동적 파킹 ETF 저장: [{_names_str}] → {out_path.name}')
    except Exception as _se:
        logger.error(f'  dynamic_parking_etfs.json 저장 실패: {_se}', exc_info=True)

def update_ticker_names() -> Dict[str, str]:
    """종목명 매핑 갱신 (Target-driven).

    ★ 리팩토링 (무의미한 전체 시장 스캔 제거):
      - 기존: get_market_ticker_list("ALL") → 2,000+ 종목 스캔 → 500번째 break
      - 변경: universe_loader.get_universe_tickers(include_etf=True) 로
              실제 추적 대상(~400종목)만 정확히 스캔
      - ETF: get_market_ticker_name 실패 시 get_etf_ticker_name 폴백

    Returns:
        ticker → 종목명 dict
    """
    try:
        from pykrx import stock as pykrx_stock
    except ImportError as e:
        logger.error('  pykrx 미설치 — 종목명 갱신 스킵', exc_info=True)
        return {}
    tickers: List[str] = []
    try:
        from src.data_collection.universe_loader import get_universe_tickers
        tickers = get_universe_tickers(include_etf=True)
        logger.info(f'  유니버스 로더: {len(tickers)}종목 (주식+ETF)')
    except Exception as _ue:
        logger.error(f'  universe_loader 임포트 실패 → 파킹 ETF + Config 유니버스 폴백: {_ue}', exc_info=True)
        try:
            for etf in universe.A1_DIRECTIONAL.values():
                tickers.append(etf.ticker)
            for etf in universe.A2_SECTORS.values():
                tickers.append(etf.ticker)
            for etf in universe.ASSET_ALLOCATION.values():
                tickers.append(etf.ticker)
            for etf in universe.SLEEVE_B_ETFS.values():
                tickers.append(etf.ticker)
        except Exception as _univ_e:
            logger.error(f'  Config universe 폴백 실패: {_univ_e}', exc_info=True)
        _pk = _PROJECT_ROOT / 'results' / 'dynamic_parking_etfs.json'
        try:
            if _pk.exists():
                for p in json.loads(_pk.read_text()):
                    tickers.append(p['ticker'])
        except Exception as _pk_e:
            logger.error(f'  dynamic_parking_etfs 로드 실패: {_pk_e}', exc_info=True)
    tickers = list(dict.fromkeys((t for t in tickers if t)))
    if not tickers:
        logger.warning('  종목명 갱신 대상 없음')
        return {}
    logger.info(f'  종목명 갱신 시작: {len(tickers)}종목')
    names: Dict[str, str] = {}
    for i, t in enumerate(tickers, 1):
        time.sleep(0.05)
        try:
            name = pykrx_stock.get_market_ticker_name(t)
            if not name:
                name = pykrx_stock.get_etf_ticker_name(t)
            if name:
                names[t] = name
        except Exception as _nm_e:
            logger.error(f'  종목명 조회 실패 {t}: {_nm_e}', exc_info=True)
        if i % 100 == 0:
            logger.info(f'  [{i}/{len(tickers)}] 진행 중... ({len(names)}건 수집)')
    if names:
        out = _PROJECT_ROOT / 'results' / 'ticker_names.json'
        out.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(out, names, indent=2, ensure_ascii=False)
        logger.info(f'  ✅ 종목명 갱신 완료: {len(names)}/{len(tickers)}종목 → ticker_names.json')
    return names

def _count_stale_files(threshold_h: float=20) -> int:
    """미갱신 KR parquet 파일 수 (파일 수정 시간 기반).

    stale 파일명을 로그에 출력하여 디버깅 가능.
    """
    stale_names = []
    if _DATA_DIR.exists():
        now = datetime.now()
        for f in _DATA_DIR.glob('kr_*.parquet'):
            age_h = (now - datetime.fromtimestamp(f.stat().st_mtime)).total_seconds() / 3600
            if age_h > threshold_h:
                stale_names.append(f'{f.name} ({age_h:.1f}h)')
    if stale_names:
        logger.info(f'  📋 Stale 파일 목록: {stale_names[:20]}')
    return len(stale_names)

def _run_freshness_gate() -> Dict:
    """[Step 3] 시그널 생성 전 전체 데이터 신선도 확인 및 보정 수집.

    전일 evening/krx_refresh에서 수집 실패한 데이터를 탐지하고
    시그널 생성 전에 보정. 대상:
      1. KR 개별종목 (kr_0*.parquet 등 6자리 종목코드)
      2. KR ETF (kr_*.parquet 중 ETF 코드)
      3. Cross-market (cross_*.parquet)
      4. signal_cache 핵심 키

    [Step 3: Hardcoding 제거]
      기존 weekday 하드코딩(월요일=65h, 평일=20h) → market_calendar.is_trading_day() 동적 판단
      - 오늘이 실제 영업일: stale 기준 = config data.freshness_stale_h (기본 20h)
      - 오늘이 공휴일/주말: stale 기준 완화 = 마지막 영업일 이후 경과 시간 + 버퍼
    """
    gate_results = {'status': 'pass', 'repairs': {}}
    now = datetime.now()
    _default_stale_h = float(cfg.get('data.freshness_stale_h', 20.0))
    _holiday_buffer_h = float(cfg.get('data.freshness_holiday_buffer_h', 4.0))
    stale_h = _default_stale_h
    try:
        from src.utils.market_calendar import get_calendar
        _cal = get_calendar()
        _today_str = now.strftime('%Y%m%d')
        _is_trading = _cal.is_trading_day(_today_str)
        if not _is_trading:
            try:
                _prev_str = _cal.get_previous_trading_day(_today_str)
                _prev_date = datetime.strptime(_prev_str, '%Y%m%d').replace(hour=16, minute=30)
                _elapsed_h = (now - _prev_date).total_seconds() / 3600
                stale_h = _elapsed_h + _holiday_buffer_h
                logger.info(f'  [Freshness] 비영업일 — 이전 영업일: {_prev_str}, 경과 {_elapsed_h:.1f}h + 버퍼 {_holiday_buffer_h}h → stale_h={stale_h:.1f}h')
            except Exception as _prev_e:
                stale_h = 72.0
                logger.error(f'  [Freshness] 이전 영업일 계산 실패 → stale_h=72h: {_prev_e}', exc_info=True)
        else:
            stale_h = _default_stale_h
            logger.info(f'  [Freshness] 영업일 — stale_h={stale_h:.1f}h')
    except Exception as _cal_e:
        _wd = now.weekday()
        stale_h = 65.0 if _wd == 0 else 48.0 if _wd >= 5 else _default_stale_h
        logger.warning(f'  [Freshness] market_calendar 조회 실패 → 요일({_wd}) 기반 stale_h={stale_h:.1f}h: {_cal_e}')
    logger.info(f'  Freshness 기준: {stale_h:.1f}h')
    stale_kr = []
    if _DATA_DIR.exists():
        for f in sorted(_DATA_DIR.glob('kr_*.parquet'))[:500]:
            age_h = (now - datetime.fromtimestamp(f.stat().st_mtime)).total_seconds() / 3600
            if age_h > stale_h:
                ticker = f.stem.replace('kr_', '')
                if _KR_TICKER_RE.match(ticker):
                    stale_kr.append(ticker)
    if stale_kr:
        logger.info(f'  🔧 KR stale: {len(stale_kr)}개 → 보정 수집')
        try:
            repair_results = collect_kr_stocks(stale_kr)
            n_fixed = sum((1 for v in repair_results.values() if v > 0))
            n_still = sum((1 for v in repair_results.values() if v <= 0))
            gate_results['repairs']['kr_stocks'] = {'attempted': len(stale_kr), 'fixed': n_fixed, 'still_stale': n_still}
            if n_still > 0:
                still_list = [t for t, v in repair_results.items() if v <= 0]
                logger.warning(f'  ⚠️ KR 미보정 {n_still}개: {still_list[:10]}')
                gate_results['status'] = 'partial'
            else:
                logger.info(f'  ✅ KR 보정 완료: {n_fixed}/{len(stale_kr)}')
        except Exception as e:
            logger.error(f'  ❌ KR 보정 실패: {e}', exc_info=True)
            gate_results['repairs']['kr_stocks'] = {'error': str(e)}
            gate_results['status'] = 'error'
    else:
        logger.info(f'  ✅ KR: 전 종목 Fresh')
        gate_results['repairs']['kr_stocks'] = {'attempted': 0, 'fixed': 0}
    stale_cross = []
    if _DATA_DIR.exists():
        for f in sorted(_DATA_DIR.glob('cross_*.parquet')):
            age_h = (now - datetime.fromtimestamp(f.stat().st_mtime)).total_seconds() / 3600
            if age_h > stale_h:
                stale_cross.append(f.name)
    if stale_cross:
        logger.info(f'  🔧 Cross-market stale: {len(stale_cross)}개 → 재수집')
        try:
            cross = collect_cross_market()
            gate_results['repairs']['cross_market'] = {'stale': stale_cross, 'recollected': True}
            logger.info(f'  ✅ Cross-market 보정 완료')
        except Exception as e:
            logger.error(f'  ❌ Cross-market 보정 실패: {e}', exc_info=True)
            gate_results['repairs']['cross_market'] = {'error': str(e)}
            gate_results['status'] = 'error'
    else:
        logger.info(f'  ✅ Cross-market: Fresh')
    try:
        sc_file = _PROJECT_ROOT / 'results' / 'signal_cache.json'
        if sc_file.exists():
            sc = json.loads(sc_file.read_text())
            missing_keys = []
            critical_keys = ['vix', 'sp500', 'usdkrw', 'kospi']
            for k in critical_keys:
                v = sc.get(k)
                if v is None or (isinstance(v, float) and v == 0.0):
                    missing_keys.append(k)
            if missing_keys:
                logger.warning(f'  ⚠️ signal_cache 누락: {missing_keys} → 글로벌 시그널 재수집')
                gate_results['repairs']['signal_cache'] = {'missing': missing_keys}
                gate_results['status'] = 'partial'
            else:
                logger.info(f'  ✅ signal_cache: 핵심 키 정상')
    except Exception as e:
        logger.error(f'  signal_cache 점검 실패: {e}', exc_info=True)
    total_stale = _count_stale_files(threshold_h=stale_h)
    gate_results['remaining_stale'] = total_stale
    gate_results['threshold_h'] = stale_h
    logger.info(f"  📊 Freshness Gate 결과: {gate_results['status']}, 잔여 stale={total_stale}")
    return gate_results

def run_daily(mode: str='full', **kwargs):
    """일일 수집 — 모드별 분리.

    ★ Pipeline Timing Optimization (2026-05-29)

    mode='morning' (06:00 KST = US 17:00 EDT):
      - 글로벌 시그널 (VIX, 금리, 환율, 원자재, SOX, EWY, FXI 등)
      - US 매크로 경제지표 (FRED 일간 — US 장 마감 후 최신)
      - 크로스마켓 (US-JP 스프레드, Yield Curve — US 장 마감 후 확정)
      - 뉴스 감성 (야간 한국/글로벌 뉴스)
      - ATR 사전 적재

    mode='evening' (17:00 KST = 아시아 장 마감 후):
      - KR ETF/개별종목 확정 종가 (pykrx)
      - 섹터 배치 (상관관계 확정)
      - 시장 브레드스 (VKOSPI, Put/Call)
      - 이브닝 글로벌 시그널 (TAIEX, Nikkei, HangSeng — 아시아 장 마감 후)
      - 유니버스 갱신

    mode='full': 전체 10단계 (legacy 호환)
    """
    logger.info(f'═══ Daily Collection (mode={mode}) ═══')
    start = datetime.now()
    results = {}
    if mode == 'morning':
        logger.info('\n[0/6] 📋 Data Freshness Gate (미수집 보정)')
        freshness_results = _run_freshness_gate()
        results['freshness_gate'] = freshness_results
        logger.info('\n[1/6] 글로벌 시그널 수집 (US 장 마감 후 최신)')
        try:
            signals = collect_global_signals()
            results['signals'] = len(signals)
            logger.info(f'  → {len(signals)}개 시그널')
        except Exception as e:
            logger.error(f'  글로벌 시그널 실패: {e}', exc_info=True)
        logger.info('\n[2/6] US 매크로 경제지표 (FRED)')
        try:
            macro = collect_us_macro()
            results['macro'] = macro
        except Exception as e:
            logger.error(f'  FRED 매크로 실패: {e}', exc_info=True)
        logger.info('\n[3/6] 크로스마켓 수집 (US-JP, Yield Curve)')
        try:
            cross = collect_cross_market()
            results['cross_market'] = cross
        except Exception as e:
            logger.error(f'  크로스마켓 실패: {e}', exc_info=True)
        logger.info('\n[4/6] 뉴스 감성 수집 (야간 뉴스)')
        try:
            sentiment = collect_sentiment()
            results['sentiment'] = sentiment
        except Exception as e:
            logger.error(f'  뉴스 감성 실패: {e}', exc_info=True)
        logger.info('\n[5/6] ATR/VIX 사전 적재')
        try:
            from src.data_collection.market_data_prefetch import run_prefetch
            run_prefetch()
            results['prefetch'] = True
        except ImportError as e:
            logger.error('[SILENT_BYPASS] Suppressed exception at unified_collector.py:1020', exc_info=True)
        except Exception as e:
            logger.error(f'  사전 적재 실패: {e}', exc_info=True)
        logger.info('\n[6/6] 최종 Data Freshness 검증')
        final_stale = _count_stale_files(threshold_h=20)
        results['final_stale_count'] = final_stale
        if final_stale > 0:
            logger.warning(f'  ⚠️ 시그널 생성 전 미갱신 파일 {final_stale}개 잔존')
        else:
            logger.info('  ✅ 전 데이터 Fresh — 시그널 생성 준비 완료')
    elif mode == 'evening':
        logger.info('\n[0/7] 🅿️ 동적 파킹 ETF 탐색 (S5 Cash Sweep)')
        try:
            parking_result = update_parking_etf_universe()
            results['parking_etfs'] = len(parking_result)
            logger.info(f'  → 파킹 ETF {len(parking_result)}종목 선정')
        except Exception as e:
            logger.error(f'  파킹 ETF 탐색 실패 (폴백 유지): {e}', exc_info=True)
        logger.info('\n[1/7] KR ETF 확정 종가')
        try:
            etf_results = collect_all_kr_etfs()
            updated = sum((1 for v in etf_results.values() if v > 0))
            results['etf'] = updated
            logger.info(f'  → {updated}/{len(etf_results)} ETF 업데이트')
        except Exception as e:
            logger.error(f'  KR ETF 실패: {e}', exc_info=True)
        logger.info('\n[2/6] KR 개별종목 확정 종가')
        try:
            uni_file = _PROJECT_ROOT / 'results' / 'dynamic_universe.json'
            if uni_file.exists():
                tickers = json.loads(uni_file.read_text())
                max_stocks = cfg.get('collector.max_stock_tickers', 350)
                stock_results = collect_kr_stocks(tickers[:max_stocks])
                n_updated = sum((1 for v in stock_results.values() if v > 0))
                n_failed = sum((1 for v in stock_results.values() if v < 0))
                failed_tickers = [t for t, v in stock_results.items() if v < 0]
                results['stocks'] = n_updated
                results['stocks_attempted'] = len(stock_results)
                results['stocks_failed'] = n_failed
                if failed_tickers:
                    results['stocks_failed_list'] = failed_tickers[:20]
        except Exception as e:
            logger.error(f'  개별종목 실패: {e}', exc_info=True)
        logger.info('\n[3/7] 섹터 배치 수집')
        try:
            sector = collect_sector_batch()
            results['sector_batch'] = sector
        except Exception as e:
            logger.error(f'  섹터 배치 실패: {e}', exc_info=True)
        logger.info('\n[4/7] 시장 브레드스 (VKOSPI, Put/Call)')
        try:
            breadth = collect_market_breadth()
            results['breadth'] = breadth
        except Exception as e:
            logger.error(f'  시장 브레드스 실패: {e}', exc_info=True)
        logger.info('\n[5/7] 외국인/기관 수급')
        try:
            flows = collect_investor_flow()
            results['flow'] = flows
        except Exception as e:
            logger.error(f'  수급 수집 실패: {e}', exc_info=True)
        logger.info('\n[6/7] 이브닝 글로벌 (TAIEX, Nikkei, HangSeng)')
        try:
            _collect_evening_signals()
            results['evening_signals'] = True
        except Exception as e:
            logger.error(f'  이브닝 시그널 실패: {e}', exc_info=True)
        logger.info('\n[7/7] Stale Sweep (미수집 종목 보정) + 파킹 ETF 점검')
        try:
            stale_threshold_h = 20
            stale_tickers = []
            for f in sorted(_DATA_DIR.glob('kr_*.parquet'))[:500]:
                age_h = (datetime.now() - datetime.fromtimestamp(f.stat().st_mtime)).total_seconds() / 3600
                if age_h > stale_threshold_h:
                    ticker = f.stem.replace('kr_', '')
                    if _KR_TICKER_RE.match(ticker):
                        stale_tickers.append(ticker)
            if stale_tickers:
                logger.info(f'  → {len(stale_tickers)}개 미수집 종목 보정 수집')
                sweep_results = collect_kr_stocks(stale_tickers)
                n_fixed = sum((1 for v in sweep_results.values() if v > 0))
                n_still_stale = sum((1 for v in sweep_results.values() if v <= 0))
                results['stale_sweep'] = {'attempted': len(stale_tickers), 'fixed': n_fixed, 'still_stale': n_still_stale}
                logger.info(f'  ✅ Stale sweep: {n_fixed}/{len(stale_tickers)} 보정 완료')
                if n_still_stale > 0:
                    still = [t for t, v in sweep_results.items() if v <= 0]
                    logger.warning(f'  ⚠️ 미보정: {still[:10]}')
            else:
                logger.info('  → 미수집 종목 없음 (all fresh)')
                results['stale_sweep'] = {'attempted': 0, 'fixed': 0, 'still_stale': 0}
        except Exception as e:
            logger.error(f'  Stale sweep 실패: {e}', exc_info=True)
    else:
        logger.info('\n[1/10] 글로벌 시그널 수집')
        try:
            signals = collect_global_signals()
            results['signals'] = len(signals)
            logger.info(f'  → {len(signals)}개 시그널')
        except Exception as e:
            logger.error(f'  글로벌 시그널 실패: {e}', exc_info=True)
        logger.info('\n[2/10] KR ETF 수집')
        try:
            etf_results = collect_all_kr_etfs()
            updated = sum((1 for v in etf_results.values() if v > 0))
            results['etf'] = updated
        except Exception as e:
            logger.error(f'  KR ETF 실패: {e}', exc_info=True)
        logger.info('\n[3/10] 수급 데이터 수집')
        try:
            flows = collect_investor_flow()
            results['flow'] = flows
        except Exception as e:
            logger.error(f'  수급 수집 실패: {e}', exc_info=True)
        logger.info('\n[4/10] KR 개별종목 수집')
        try:
            uni_file = _PROJECT_ROOT / 'results' / 'dynamic_universe.json'
            if uni_file.exists():
                tickers = json.loads(uni_file.read_text())
                max_stocks = cfg.get('collector.max_stock_tickers', 350)
                stock_results = collect_kr_stocks(tickers[:max_stocks])
                results['stocks'] = sum((1 for v in stock_results.values() if v > 0))
                results['stocks_attempted'] = len(stock_results)
                results['stocks_failed'] = sum((1 for v in stock_results.values() if v < 0))
        except Exception as e:
            logger.error(f'  개별종목 실패: {e}', exc_info=True)
        logger.info('\n[5/10] US 매크로 경제지표 (FRED)')
        try:
            macro = collect_us_macro()
            results['macro'] = macro
        except Exception as e:
            logger.error(f'  FRED 매크로 실패: {e}', exc_info=True)
        logger.info('\n[6/10] 크로스마켓 수집')
        try:
            cross = collect_cross_market()
            results['cross_market'] = cross
        except Exception as e:
            logger.error(f'  크로스마켓 실패: {e}', exc_info=True)
        logger.info('\n[7/10] 시장 브레드스')
        try:
            breadth = collect_market_breadth()
            results['breadth'] = breadth
        except Exception as e:
            logger.error(f'  시장 브레드스 실패: {e}', exc_info=True)
        logger.info('\n[8/10] 뉴스 감성 수집')
        try:
            sentiment = collect_sentiment()
            results['sentiment'] = sentiment
        except Exception as e:
            logger.error(f'  뉴스 감성 실패: {e}', exc_info=True)
        logger.info('\n[9/10] 섹터 배치 데이터')
        try:
            sector = collect_sector_batch()
            results['sector_batch'] = sector
        except Exception as e:
            logger.error(f'  섹터 배치 실패: {e}', exc_info=True)
        logger.info('\n[10/10] DART 공시 수집')
        try:
            dart = collect_dart()
            results['dart'] = dart
        except Exception as e:
            logger.error(f'  DART 실패: {e}', exc_info=True)
    elapsed = (datetime.now() - start).total_seconds()
    results['elapsed_sec'] = round(elapsed)
    results['mode'] = mode
    results['timestamp'] = datetime.now().isoformat()
    _save_collection_log(results)
    _save_collection_status(results, mode)
    logger.info(f'\n═══ 수집 완료 (mode={mode}, {elapsed:.0f}초) ═══')
    return results

def _collect_evening_signals():
    """이브닝 전용 시그널 수집 (아시아 장 마감 후 확정 지표).

    [Maintenance] 강화된 Fallback 구조:
      1단: yf.download (최대 2회 retry, 1초 간격)
      2단: 실패 시 기존 parquet의 마지막 행 ffill 저장
      → 누락 티커는 WARNING으로 명시 (Fail-silent 방지)
    """
    import time as _t
    try:
        import yfinance as yf
    except ImportError as e:
        logger.error('  ⚠️ yfinance 미설치 — evening signals 스킵', exc_info=True)
        return
    from config.universe import Universe
    u = Universe()
    failed_tickers = []
    ffill_tickers = []
    for name, yf_ticker in u.SIGNAL_EVENING.items():
        out_file = _SIGNAL_DIR / f'signal_{name.lower()}.parquet'
        data = None
        for attempt in range(2):
            try:
                raw = yf.download(yf_ticker, period='3mo', progress=False, auto_adjust=True, timeout=12)
                if raw is not None and (not raw.empty):
                    data = raw
                    break
            except Exception as _e:
                if attempt == 0:
                    logger.error(f'  {name}({yf_ticker}) retry 1/2: {_e}', exc_info=True)
                    _t.sleep(1.0)
        if data is not None:
            try:
                _SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
                df = data.reset_index()
                df.columns = [c.lower() if isinstance(c, str) else c for c in df.columns]
                if hasattr(df.columns, 'levels'):
                    df.columns = ['_'.join(c).strip('_') if isinstance(c, tuple) else c for c in df.columns]
                safe_parquet_write(df, out_file)
                logger.info(f'  ✅ {name}: {len(df)}행')
                continue
            except Exception as _save_e:
                logger.error(f'  ⚠️ {name} parquet 저장 실패: {_save_e}', exc_info=True)
        if out_file.exists():
            try:
                import pandas as pd
                old_df = pd.read_parquet(out_file)
                if len(old_df) > 0:
                    last_row = old_df.iloc[[-1]].copy()
                    from datetime import date
                    if 'date' in last_row.columns:
                        last_row['date'] = pd.Timestamp(date.today())
                    old_df = pd.concat([old_df, last_row], ignore_index=True)
                    safe_parquet_write(old_df, out_file)
                    ffill_tickers.append(name)
                    logger.warning(f'  📋 {name}({yf_ticker}): 실시간 실패 → parquet ffill 적용 ({len(old_df)}행, 무결성 유지)')
                    continue
            except Exception as _ff_e:
                logger.error(f'  ⚠️ {name} ffill 실패: {_ff_e}', exc_info=True)
        failed_tickers.append(name)
        logger.warning(f'  ❌ {name}({yf_ticker}): 실시간 수집 + ffill 모두 실패 — 신호 누락 (네트워크 차단 또는 미상장)')
    total = len(u.SIGNAL_EVENING)
    ok = total - len(failed_tickers) - len(ffill_tickers)
    if failed_tickers or ffill_tickers:
        logger.warning(f'  📊 Evening Signals: 성공={ok}/{total}, ffill={len(ffill_tickers)}, 실패={len(failed_tickers)}' + (f' — 실패 티커: {failed_tickers}' if failed_tickers else ''))

def collect_us_macro() -> Dict:
    """FRED API 경제지표 수집."""
    try:
        from src.data_collection.usa_collector import USADataCollector
        from datetime import datetime, timedelta
        collector = USADataCollector()
        end = datetime.now().strftime('%Y-%m-%d')
        start = (datetime.now() - timedelta(days=3650)).strftime('%Y-%m-%d')
        result = collector.collect_all_usa_data(start, end)
        if result:
            economic = result.get('economic', {})
            key_map = {'CPI': 'cpi', 'Unemployment': 'unemployment_rate', 'Fed_Funds_Rate': 'federal_funds_rate', 'Consumer_Sentiment': 'consumer_sentiment', 'HY_Credit_Spread': 'hy_credit_spread', 'IG_Credit_Spread': 'ig_credit_spread', 'Industrial_Production': 'industrial_production', 'Retail_Sales': 'retail_sales', 'GDP_Growth': 'gdp_growth'}
            cache_updates = {}
            for src_key, dst_key in key_map.items():
                val = economic.get(src_key)
                if val is not None:
                    try:
                        if hasattr(val, 'iloc') and len(val) > 0:
                            _v = val.iloc[-1]
                            _v_val = float(_v.item() if hasattr(_v, 'item') else _v)
                            if pd.isna(_v_val) or _v_val == float('inf') or _v_val == float('-inf'):
                                logger.warning(f'  [Data Validation] {dst_key} contains poisoned data: {_v_val}')
                            else:
                                cache_updates[dst_key] = _v_val
                        elif isinstance(val, (int, float)) and not pd.isna(val):
                            cache_updates[dst_key] = float(val)
                    except Exception as _e1379:
                        logger.error(f'  [collect_us_macro] US 매크로 캐시 로드 실패: {_e1379}', exc_info=True)

            def _extract_last(series):
                if series is not None and hasattr(series, 'iloc') and (len(series) > 0):
                    v = series.iloc[-1]
                    val = float(v.item() if hasattr(v, 'item') else v)
                    if pd.isna(val) or val == float('inf') or val == float('-inf'):
                        return None
                    return val
                return None
            gdp_growth_series = economic.get('GDP_Growth')
            gdp_g = _extract_last(gdp_growth_series)
            if gdp_g is not None:
                cache_updates['gdp_growth'] = round(gdp_g, 2)
            else:
                real_gdp = economic.get('Real_GDP')
                if real_gdp is not None and hasattr(real_gdp, 'iloc') and (len(real_gdp) >= 2):
                    try:
                        curr = _extract_last(real_gdp)
                        v_prev = real_gdp.iloc[-2]
                        prev = float(v_prev.item() if hasattr(v_prev, 'item') else v_prev)
                        if prev > 0:
                            qoq = curr / prev - 1
                            annualized = ((1 + qoq) ** 4 - 1) * 100
                            cache_updates['gdp_growth'] = round(annualized, 2)
                    except Exception as _e1406:
                        logger.error(f'  [collect_cross_market] 크로스마켓 데이터 실패: {_e1406}', exc_info=True)
            if cache_updates:
                _save_signal_cache(cache_updates)
                logger.info(f'  ✅ FRED: {len(cache_updates)}개 지표 → signal_cache')
            else:
                logger.warning(f'  ⚠️ FRED: economic 데이터 추출 실패')
            logger.info(f'  ✅ FRED: {len(result)}개 카테고리, economic={len(economic)}개 시리즈')
            return {'series_count': len(economic)}
        return {'series_count': 0}
    except ImportError as e:
        logger.error('  usa_collector 없음 (스킵)', exc_info=True)
        return {}
    except Exception as e:
        logger.error(f'  FRED 수집 실패: {e}', exc_info=True)
        return {}

def collect_cross_market() -> Dict:
    """US-JP 스프레드, ISM/Caixin PMI, 수익률곡선."""
    try:
        from src.data_collection.cross_market_collector import CrossMarketCollector
        collector = CrossMarketCollector()
        result = collector.collect_all()
        cache_updates = {}
        if 'us_jp_spread' in result and result['us_jp_spread'] is not None:
            try:
                spread = result['us_jp_spread']
                if hasattr(spread, 'iloc') and len(spread) > 0:
                    _val = float(spread.iloc[-1, -1])
                    if not pd.isna(_val):
                        cache_updates['us_jp_spread'] = _val
            except Exception as _e1443:
                logger.error(f'  [collect_market_breadth] 시장 폭 데이터 1 실패: {_e1443}', exc_info=True)
        if 'yield_curve' in result and result['yield_curve'] is not None:
            try:
                yc = result['yield_curve']
                if hasattr(yc, 'iloc') and len(yc) > 0:
                    _val = float(yc.iloc[-1, -1])
                    if not pd.isna(_val):
                        cache_updates['us_2y10y_spread'] = _val
            except Exception as _e1450:
                logger.error(f'  [collect_market_breadth] 시장 폭 데이터 2 실패: {_e1450}', exc_info=True)
        if cache_updates:
            _save_signal_cache(cache_updates)
        logger.info(f'  ✅ 크로스마켓: {len(result)}개 지표')
        return {'indicators': len(result)}
    except ImportError as e:
        logger.error('  cross_market_collector 없음 (스킵)', exc_info=True)
        return {}
    except Exception as e:
        logger.error(f'  크로스마켓 실패: {e}', exc_info=True)
        return {}

def collect_market_breadth() -> Dict:
    """V-KOSPI, Put/Call ratio, 경제서프라이즈."""
    try:
        from src.data_collection.market_breadth_collector import MarketBreadthCollector
        collector = MarketBreadthCollector()
        result = collector.collect_all()
        cache_updates = {}
        if 'vkospi' in result:
            vkospi_val = result['vkospi'].get('vkospi')
            if vkospi_val:
                cache_updates['vkospi'] = vkospi_val
        if 'put_call' in result:
            pcr_val = result['put_call'].get('put_call_ratio')
            if pcr_val:
                cache_updates['put_call_ratio'] = pcr_val
        if cache_updates:
            _save_signal_cache(cache_updates)
        logger.info(f'  ✅ 시장 브레드스: {len(result)}개 지표')
        return result
    except ImportError as e:
        logger.error('  market_breadth_collector 없음 (스킵)', exc_info=True)
        return {}
    except Exception as e:
        logger.error(f'  시장 브레드스 실패: {e}', exc_info=True)
        return {}

def collect_sentiment() -> Dict:
    """통합 감성 수집 (네이버 뉴스 + FinBERT + Fear/Greed)."""
    results = {}
    try:
        from src.data_collection.unified_sentiment_collector import UnifiedSentimentCollector
        collector = UnifiedSentimentCollector()
        sentiment = collector.collect_all(phase='morning')
        if sentiment:
            cache_updates = {}
            if 'fear_greed' in sentiment:
                fg = sentiment['fear_greed']
                if isinstance(fg, dict) and 'score' in fg:
                    cache_updates['fear_greed_index'] = fg['score']
            if 'news_sentiment' in sentiment:
                nb = sentiment['news_sentiment']
                if isinstance(nb, dict) and 'score' in nb:
                    cache_updates['news_sentiment'] = nb['score']
            if cache_updates:
                _save_signal_cache(cache_updates)
            results['unified'] = len(sentiment)
            logger.info(f'  ✅ 통합 감성: {len(sentiment)}개 소스')
    except ImportError as e:
        logger.error('  unified_sentiment_collector 없음', exc_info=True)
    except Exception as e:
        logger.error(f'  통합 감성 실패: {e}', exc_info=True)
    try:
        from src.data_collection.naver_news_sentiment import NaverNewsSentiment
        nns = NaverNewsSentiment()
        if nns.is_available:
            uni_file = _PROJECT_ROOT / 'results' / 'dynamic_universe.json'
            if uni_file.exists():
                tickers = json.loads(uni_file.read_text())[:20]
                nns.collect_all(tickers=tickers)
                results['naver_tickers'] = len(tickers)
                logger.info(f'  ✅ 네이버 뉴스: {len(tickers)}종목')
    except ImportError as e:
        logger.error('  naver_news_sentiment 없음', exc_info=True)
    except Exception as e:
        logger.error(f'  네이버 뉴스 실패: {e}', exc_info=True)
    return results

def collect_sector_batch() -> Dict:
    """섹터 공급망, PER밴드, US-KR 베타."""
    try:
        from src.data_collection.sector_batch_collector import SectorBatchCollector
        collector = SectorBatchCollector()
        result = collector.collect_all()
        logger.info(f'  ✅ 섹터 배치: {len(result)}개')
        return result
    except ImportError as e:
        logger.error('  sector_batch_collector 없음 (스킵)', exc_info=True)
        return {}
    except Exception as e:
        logger.error(f'  섹터 배치 실패: {e}', exc_info=True)
        return {}

def collect_dart() -> Dict:
    """DART 내부자 거래, 자사주 매입."""
    try:
        from src.data_collection.dart_daily_collector import DARTDailyCollector
        collector = DARTDailyCollector()
        result = collector.collect_incremental()
        if result:
            logger.info(f'  ✅ DART: {len(result)}건 공시')
            return {'disclosures': len(result)}
        return {}
    except ImportError as e:
        logger.error('  dart_daily_collector 없음 (스킵)', exc_info=True)
        return {}
    except Exception as e:
        logger.error(f'  DART 실패: {e}', exc_info=True)
        return {}

def _save_collection_status(results: Dict, mode: str):
    """수집 결과를 data_collection_status.json에 기록.

    대시보드 Data Collection Status 섹션이 이 파일을 읽어 표시.
    """
    status_file = _PROJECT_ROOT / 'results' / 'data_collection_status.json'
    try:
        existing = {}
        if status_file.exists():
            try:
                existing = json.loads(status_file.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                existing = {}
        phases = existing.get('phases', {})
        phase_key = f'{mode}'
        phase_entry = {'timestamp': datetime.now().isoformat(), 'elapsed_sec': results.get('elapsed_sec', 0)}
        for key in results:
            if key in ('timestamp', 'mode', 'elapsed_sec'):
                continue
            val = results[key]
            if isinstance(val, int):
                phase_entry[key] = val
            elif isinstance(val, dict):
                phase_entry[key] = val
            elif isinstance(val, bool):
                phase_entry[key] = val
            elif isinstance(val, list):
                phase_entry[key] = val
        if _GLOBAL_FALLBACK_EVENTS:
            phase_entry['fallback_events'] = _GLOBAL_FALLBACK_EVENTS.copy()
            _GLOBAL_FALLBACK_EVENTS.clear()
        phases[phase_key] = phase_entry
        stale_files = []
        stale_threshold_h = cfg.get('collector.stale_threshold_hours', 36)
        if _DATA_DIR.exists():
            for f in sorted(_DATA_DIR.glob('kr_*.parquet'))[:500]:
                age_h = (datetime.now() - datetime.fromtimestamp(f.stat().st_mtime)).total_seconds() / 3600
                if age_h > stale_threshold_h:
                    stale_files.append({'file': f.name, 'age_hours': round(age_h, 1)})
        total_attempted = 0
        total_success = 0
        total_failed = 0
        for p_key, p_val in phases.items():
            if isinstance(p_val, dict):
                _att = p_val.get('stocks_attempted', 0)
                _suc = p_val.get('stocks', 0)
                _fai = p_val.get('stocks_failed', 0)
                _skip = max(0, _att - _suc - _fai)
                total_attempted += _att
                total_success += _suc + _skip
                total_failed += _fai
        status = {'last_updated': datetime.now().isoformat(), 'phases': phases, 'stale_files': stale_files[:50], 'stale_count': len(stale_files), 'overall': {'total_kr_parquets': len(list(_DATA_DIR.glob('kr_*.parquet'))) if _DATA_DIR.exists() else 0, 'fresh_count': (len(list(_DATA_DIR.glob('kr_*.parquet'))) if _DATA_DIR.exists() else 0) - len(stale_files), 'stale_count': len(stale_files), 'success_rate': round(total_success / max(total_attempted, 1), 3)}}
        import tempfile, os
        status_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(status_file.parent), suffix='.tmp')
        with os.fdopen(fd, 'w') as f:
            json.dump(status, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, str(status_file))
    except Exception as e:
        logger.error(f'  수집 상태 기록 실패: {e}', exc_info=True)

def _save_collection_log(results: Dict):
    """수집 로그 저장."""
    try:
        log_file = _PROJECT_ROOT / 'results' / 'collection_log.json'
        log_file.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if log_file.exists():
            try:
                _raw = json.loads(log_file.read_text())
                if isinstance(_raw, list):
                    existing = _raw
                elif isinstance(_raw, dict):
                    existing = _raw.get('logs', [])
                    if not isinstance(existing, list):
                        existing = []
            except Exception as _e1706:
                logger.error(f'  [run_initial/run_daily] 수집 진행 로그 실패: {_e1706}', exc_info=True)
        existing.append(results)
        existing = existing[-30:]
        atomic_write_json(log_file, existing, indent=2, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f'  수집 로그 저장 실패: {e}', exc_info=True)

def run_initial():
    """최초 백필 (10년 데이터)."""
    logger.info('═══ Project_First Initial Backfill ═══')
    logger.info('\n[1/6] 유니버스 갱신')
    tickers = update_stock_universe()
    logger.info('\n[2/6] 종목명 갱신')
    update_ticker_names()
    logger.info('\n[3/6] 글로벌 시그널 10년 백필')
    collect_global_signals(backfill=True)
    logger.info('\n[4/6] KR ETF 10년 백필')
    collect_all_kr_etfs(backfill=True)
    logger.info('\n[5/6] KR 개별종목 10년 백필 (상위 50종목)')
    if tickers:
        max_stocks = cfg.get('collector.max_stock_tickers', 350)
        collect_kr_stocks(tickers[:max_stocks], backfill=True)
    logger.info('\n[6/6] US 매크로 + 크로스마켓 초기 수집')
    collect_us_macro()
    collect_cross_market()
    logger.info('\n═══ 초기 백필 완료 ═══')
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Project_First Data Collector')
    parser.add_argument('--initial', action='store_true', help='10년 초기 백필')
    parser.add_argument('--signals', action='store_true', help='글로벌 시그널만')
    parser.add_argument('--sectors', action='store_true', help='섹터 ETF만')
    parser.add_argument('--universe', action='store_true', help='유니버스 갱신')
    parser.add_argument('--daily', action='store_true', help='일일 수집 (전체)')
    parser.add_argument('--macro', action='store_true', help='US 매크로만')
    parser.add_argument('--cross', action='store_true', help='크로스마켓만')
    parser.add_argument('--breadth', action='store_true', help='시장 브레드스만')
    parser.add_argument('--sentiment', action='store_true', help='뉴스 감성만')
    parser.add_argument('--dart', action='store_true', help='DART 공시만')
    args = parser.parse_args()
    if args.initial:
        run_initial()
    elif args.signals:
        collect_global_signals()
    elif args.macro:
        collect_us_macro()
    elif args.cross:
        collect_cross_market()
    elif args.breadth:
        collect_market_breadth()
    elif args.sentiment:
        collect_sentiment()
    elif args.dart:
        collect_dart()
    elif args.sectors:
        for etf in universe.A2_SECTORS.values():
            df = collect_kr_ohlcv(etf.ticker, backfill=True)
            if df is not None:
                out = _DATA_DIR / f'kr_{etf.ticker}.parquet'
                out.parent.mkdir(parents=True, exist_ok=True)
                safe_parquet_write(df, out)
                logger.info(f'  ✅ {etf.name} ({etf.ticker}): {len(df)}행')
            else:
                logger.error(f'  ❌ {etf.name} ({etf.ticker})')
            time.sleep(_PYKRX_DELAY)
    elif args.universe:
        update_stock_universe()
        update_ticker_names()
    else:
        run_daily()