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
try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.file_ops import atomic_write_json
from src.infra.safe_io import safe_json_write, safe_parquet_write, safe_json_update
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
_KR_TICKER_RE = re.compile('^\\d{4}[0-9A-Z]{2}$')
_PYKRX_DELAY = 1.0

@retry(wait=wait_exponential(multiplier=1, min=1, max=3), stop=stop_after_attempt(1), reraise=True)
def _fetch_kr_ohlcv_with_retry(pykrx_stock, start: str, end: str, ticker: str):
    """[Step 2: Tenacity] pykrx API 호출 래퍼 — 빈 데이터 시 빠른 스킵."""
    time.sleep(_PYKRX_DELAY)
    try:
        df = pykrx_stock.get_market_ohlcv_by_date(start, end, ticker)
        if df is None or df.empty:
            return pd.DataFrame()
        return df
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(f"  [pykrx Skip] API exception for {ticker}: {e}")
        return pd.DataFrame()

def collect_kr_ohlcv(ticker: str, days: int=60, backfill: bool=False) -> Optional[pd.DataFrame]:
    """KIS API를 최우선(Primary)으로 호출하고, 장기 히스토리(days > 100 또는 backfill)는 FinanceDataReader(Naver)와 병합.

    Args:
        ticker: 종목코드 (6자리)
        days: 수집 일수
        backfill: True면 10년 백필
    """
    if backfill:
        target_days = 3650
    else:
        target_days = days
    start = (datetime.now() - timedelta(days=target_days)).strftime('%Y%m%d')
    end = datetime.now().strftime('%Y%m%d')

    def _normalize_ohlcv_df(raw_df: pd.DataFrame) -> pd.DataFrame:
        if raw_df is None or raw_df.empty:
            return pd.DataFrame()
        d = raw_df.reset_index()
        d.columns = [str(c).lower() for c in d.columns]
        rename = {'날짜': 'date', '시가': 'open', '고가': 'high', '저가': 'low', '종가': 'close', '거래량': 'volume'}
        d = d.rename(columns=rename)
        if 'date' not in d.columns:
            d = d.rename(columns={d.columns[0]: 'date'})
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col in d.columns:
                d[col] = pd.to_numeric(d[col], errors='coerce')
        d = d.dropna(subset=['open', 'high', 'low', 'close', 'volume'])
        d = d[d['volume'] > 0]
        d['date'] = pd.to_datetime(d['date'])
        if pd.api.types.is_datetime64tz_dtype(d['date']):
            d['date'] = d['date'].dt.tz_localize(None)
        return d.sort_values('date').drop_duplicates(subset='date', keep='last')

    df_kis = pd.DataFrame()
    # 1. KIS API 호출 시도 (Primary — 단, 단일 호출 최대 100영업일 반환)
    try:
        from src.data_collection.kis_data_collector import KISDataCollector
        kis = KISDataCollector()
        if kis._ensure_auth():
            raw_kis = kis.get_kr_daily_ohlcv(ticker, start, end)
            df_kis = _normalize_ohlcv_df(raw_kis)
            if not df_kis.empty and target_days <= 120:
                logger.debug(f'  [KIS API] {ticker} 수집 성공 ({len(df_kis)}행)')
                return df_kis
    except Exception as e:
        logger.warning(f'  ⚠️ KIS API 호출 오류 ({ticker}): {e} -> Fallback 시도')

    # 2. 장기 히스토리(target_days > 120 또는 backfill)이거나 KIS 실패 시 Naver API(FDR) 호출하여 병합
    df_fdr = pd.DataFrame()
    try:
        import FinanceDataReader as fdr
        raw_fdr = fdr.DataReader(ticker, start, end)
        df_fdr = _normalize_ohlcv_df(raw_fdr)
    except Exception as naver_e:
        logger.debug(f'  {ticker}: Naver API(FDR) 수집 예외 — {naver_e}')

    if not df_fdr.empty and not df_kis.empty:
        combined = pd.concat([df_fdr, df_kis]).drop_duplicates(subset='date', keep='last').sort_values('date')
        return combined
    if not df_fdr.empty:
        return df_fdr
    if not df_kis.empty:
        return df_kis

    # 3. pykrx_compat 호출 (최종 Fallback)
    try:
        from src.data_collection.pykrx_compat import stock as pykrx_stock
        raw_pykrx = _fetch_kr_ohlcv_with_retry(pykrx_stock, start, end, ticker)
        df_pykrx = _normalize_ohlcv_df(raw_pykrx)
        if not df_pykrx.empty:
            return df_pykrx
    except Exception as e:
        logger.warning(f'  {ticker}: pykrx 수집 실패 — {e}')

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
            existing.columns = [str(c).lower() for c in existing.columns]
            if 'volume' in existing.columns:
                existing = existing[pd.to_numeric(existing['volume'], errors='coerce').fillna(0) > 0]
            last_date = pd.to_datetime(existing['date']).max() if not existing.empty else pd.Timestamp('2000-01-01')
            now_dt = datetime.now()
            days_since = (now_dt.date() - last_date.date()).days
            needs_history = len(existing) < 300
            mtime_dt = datetime.fromtimestamp(out_file.stat().st_mtime)
            _close_hm = (int(cfg.get('collector.kr_close_hour', 15)), int(cfg.get('collector.kr_close_minute', 30)))
            needs_closing_refresh = (days_since == 0 and (now_dt.hour, now_dt.minute) >= _close_hm and (mtime_dt.date() < now_dt.date() or (mtime_dt.hour, mtime_dt.minute) < _close_hm))
            if days_since <= 0 and not needs_history and not needs_closing_refresh:
                logger.debug(f'  [{i}/{total}] {ticker}: 최신 (스킵)')
                results[ticker] = 0
                continue
            fetch_days = max(days_since + 5, 730 if needs_history else 60)
            df = collect_kr_ohlcv(ticker, days=fetch_days)
            if df is not None and (not df.empty):
                df.columns = [str(c).lower() for c in df.columns]
                existing['date'] = pd.to_datetime(existing['date'])
                df['date'] = pd.to_datetime(df['date']) if 'date' in df.columns else df
                combined = pd.concat([existing, df]).drop_duplicates(subset='date', keep='last').sort_values('date')
                safe_parquet_write(combined, out_file)
                results[ticker] = len(df)
                logger.info(f'  [{i}/{total}] {ticker}: +{len(df)}행 병합 (총 {len(combined)}행)')
            else:
                results[ticker] = 0
        else:
            df = collect_kr_ohlcv(ticker, days=730, backfill=backfill)
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
        """DataFrame에 date 컬럼이 있는지 보장 및 컬럼 소문자 정규화."""
        if df is None or df.empty:
            return df
        df = df.copy()
        df.columns = [str(c).lower() for c in df.columns]
        if 'date' in df.columns:
            return df
        if hasattr(df.index, 'dtype') and ('datetime' in str(df.index.dtype) or str(df.index.name).lower() in ('날짜', 'date')):
            df = df.reset_index()
            df.columns = [str(c).lower() for c in df.columns]
            if df.columns[0] != 'date':
                df = df.rename(columns={df.columns[0]: 'date'})
            return df
        try:
            pd.to_datetime(df.iloc[:, 0])
            df = df.rename(columns={df.columns[0]: 'date'})
        except Exception as _dt_e:
            logger.error(f'  _ensure_date_col: 첫 컬럼 날짜 변환 실패 — {_dt_e}', exc_info=True)
        return df
    _close_hm = (int(cfg.get('collector.kr_close_hour', 15)), int(cfg.get('collector.kr_close_minute', 30)))
    for i, ticker in enumerate(valid_tickers, 1):
        out_file = _DATA_DIR / f'kr_{ticker}.parquet'
        if out_file.exists() and (not backfill):
            try:
                existing = pd.read_parquet(out_file)
                existing = _ensure_date_col(existing)
                if 'date' not in existing.columns:
                    df = collect_kr_ohlcv(ticker, days=730, backfill=backfill)
                    if df is not None and (not df.empty):
                        _DATA_DIR.mkdir(parents=True, exist_ok=True)
                        safe_parquet_write(df, out_file)
                        results[ticker] = len(df)
                    else:
                        results[ticker] = -1
                    continue
                if 'volume' in existing.columns:
                    existing = existing[pd.to_numeric(existing['volume'], errors='coerce').fillna(0) > 0]
                last_date = pd.to_datetime(existing['date']).max() if not existing.empty else pd.Timestamp('2000-01-01')
                now_dt = datetime.now()
                days_since = (now_dt.date() - last_date.date()).days
                needs_history = len(existing) < 300
                mtime_dt = datetime.fromtimestamp(out_file.stat().st_mtime)
                needs_closing_refresh = (days_since == 0 and (now_dt.hour, now_dt.minute) >= _close_hm and (mtime_dt.date() < now_dt.date() or (mtime_dt.hour, mtime_dt.minute) < _close_hm))
                if days_since <= 0 and not needs_history and not needs_closing_refresh:
                    results[ticker] = 0
                    continue
                fetch_days = max(days_since + 5, 730 if needs_history else 60)
                df = collect_kr_ohlcv(ticker, days=fetch_days)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
                df = collect_kr_ohlcv(ticker, days=730)
        else:
            df = collect_kr_ohlcv(ticker, days=730, backfill=backfill)
        if df is not None and (not df.empty):
            if out_file.exists() and (not backfill):
                try:
                    existing = pd.read_parquet(out_file)
                    existing = _ensure_date_col(existing)
                    if 'volume' in existing.columns:
                        existing = existing[pd.to_numeric(existing['volume'], errors='coerce').fillna(0) > 0]
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
                    combined = df
                    safe_parquet_write(df, out_file)
            else:
                _DATA_DIR.mkdir(parents=True, exist_ok=True)
                combined = df
                safe_parquet_write(df, out_file)
            results[ticker] = len(df)
            try:
                from src.data_collection.feature_store import FeatureStore
                FeatureStore().sync_ticker_parquet(ticker, ohlcv_df=combined)
            except Exception as _fs_e:
                logger.debug(f'  {ticker} FeatureStore parquet 동기화 스킵: {_fs_e}')
        else:
            results[ticker] = -1
        if i % 20 == 0:
            logger.info(f'  [{i}/{total}] 진행 중...')
    return results


def _save_signal_parquet(df: pd.DataFrame, out_file: Path) -> pd.DataFrame:
    """시그널 parquet 병합 저장 (단일 행 덮어쓰기 방지 — 기존 시계열 보존 + date 중복 제거)."""
    out_file.parent.mkdir(parents=True, exist_ok=True)
    new_df = df.copy()
    new_df.columns = [str(c).lower() for c in new_df.columns]
    if 'date' not in new_df.columns:
        new_df = new_df.reset_index().rename(columns={new_df.index.name or 'index': 'date'})
    new_df['date'] = pd.to_datetime(new_df['date'], errors='coerce').dt.tz_localize(None).dt.normalize()
    if out_file.exists():
        try:
            existing = pd.read_parquet(out_file)
            existing.columns = [str(c).lower() for c in existing.columns]
            if 'date' not in existing.columns:
                existing = existing.reset_index().rename(columns={existing.index.name or 'index': 'date'})
            existing['date'] = pd.to_datetime(existing['date'], errors='coerce').dt.tz_localize(None).dt.normalize()
            combined = (
                pd.concat([existing, new_df], ignore_index=True)
                .dropna(subset=['date'])
                .drop_duplicates(subset=['date'], keep='last')
                .sort_values('date')
                .reset_index(drop=True)
            )
            safe_parquet_write(combined, out_file)
            return combined
        except Exception as e:
            logger.debug(f'  _save_signal_parquet merge fallback ({out_file.name}): {e}')
    safe_parquet_write(new_df, out_file)
    return new_df


def collect_global_signals(backfill: bool=False) -> Dict[str, float]:
    """[Phase 1] 글로벌 시그널 데이터 수집 (yfinance 대체 - Alpha Vantage 사용).

    주의: Alpha Vantage GLOBAL_QUOTE는 최신(당일) 값만 반환합니다.
    따라서 backfill(과거 10년치 생성)은 지원하지 않으며, 기존 캐시를 append 합니다.
    """
    from src.data_collection.alpha_vantage_collector import collect_global_macro
    _CROSS_SYNC_MAP = {'vix': ['cross_vix.parquet', 'us_vix.parquet'], 'sp500': ['cross_sp500.parquet'], 'usdkrw': ['cross_usdkrw.parquet', 'us_usdkrw.parquet'], 'us10y': ['cross_us10y.parquet'], 'wti': ['cross_oil_futures.parquet'], 'gold_us': ['cross_gold_futures.parquet']}
    _US_STOCKS_SYNC_MAP = {'vix': ['^VIX.parquet'], 'usdkrw': ['KRW=X.parquet']}
    signals = {}
    today_dt = pd.Timestamp(datetime.now().date())
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
                series = usa_collector._safe_fred_get_series(fred_id) if hasattr(usa_collector, '_safe_fred_get_series') else None
                if series is not None and not series.empty:
                    price = float(series.iloc[-1])
                    av_data[yf_ticker] = {'price': price}
            except Exception as e:
                logger.warning(f"Failed to fetch {yf_ticker} via FRED: {e}")
        
        if yf_ticker in av_data:
            price = av_data[yf_ticker]['price']
        else:
            # yfinance 폐기 — KIS / Alpha Vantage 우선 참조
            pass

        if price is not None:
            df = pd.DataFrame([{'date': today_dt, 'open': price, 'high': price, 'low': price, 'close': price, 'volume': 0}])
            out_file = _SIGNAL_DIR / f'signal_{name.lower()}.parquet'
            _save_signal_parquet(df, out_file)
            name_lower = name.lower()
            for cross_file in _CROSS_SYNC_MAP.get(name_lower, []):
                for cross_dir in (_PROJECT_ROOT / 'data' / 'cross_market', _DATA_DIR):
                    cross_path = cross_dir / cross_file
                    try:
                        _save_signal_parquet(df, cross_path)
                    except Exception as e:
                        logger.error(f'  cross sync {cross_file} 실패: {e}', exc_info=True)
            for us_file in _US_STOCKS_SYNC_MAP.get(name_lower, []):
                for us_dir in (_PROJECT_ROOT / 'data' / 'us_stocks' / 'prices', _DATA_DIR / 'us_stocks'):
                    us_path = us_dir / us_file
                    try:
                        us_path.parent.mkdir(parents=True, exist_ok=True)
                        df_us = df.set_index('date') if 'date' in df.columns else df.copy()
                        if us_path.exists():
                            existing = pd.read_parquet(us_path)
                            existing.columns = [c.lower() for c in existing.columns]
                            if 'date' in existing.columns:
                                existing['date'] = pd.to_datetime(existing['date']).dt.normalize()
                                existing = existing.set_index('date')
                            else:
                                existing.index = pd.to_datetime(existing.index).normalize()
                            combined = pd.concat([existing, df_us])
                            combined = combined[~combined.index.duplicated(keep='last')]
                            safe_parquet_write(combined, us_path)
                        else:
                            safe_parquet_write(df_us, us_path)
                    except Exception as e:
                        logger.error(f'  us_stocks sync {us_file} 실패: {e}', exc_info=True)
            signals[name] = price
            signals[name_lower] = price
            if yf_ticker in av_data and av_data[yf_ticker].get('change_1d') is not None:
                signals[f'{name_lower}_change_1d'] = float(av_data[yf_ticker]['change_1d'])
    try:
        from src.data_collection.alpha_vantage_collector import collect_options_pcr
        pcr = collect_options_pcr('SPY')
        signals['options_pcr'] = pcr
        signals['put_call_ratio'] = pcr
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
    """시그널 캐시 저장 (safe_json_update + 지수 범위 검증 및 별칭/계약 키 동기화)."""
    try:
        from src.data_collection.macro_realtime_refresher import MacroRealtimeRefresher
        bounds_map = MacroRealtimeRefresher._INDEX_SANITY_BOUNDS

        def _update(data):
            if not isinstance(data, dict):
                data = {}
            for _ek in list(data.keys()):
                if _ek in bounds_map and isinstance(data.get(_ek), (int, float)):
                    _lo, _hi = bounds_map[_ek]
                    if not (_lo <= float(data[_ek]) <= _hi):
                        data.pop(_ek, None)
            safe_signals = {}
            for k, v in (signals or {}).items():
                if k in bounds_map and isinstance(v, (int, float)):
                    _lo, _hi = bounds_map[k]
                    if not (_lo <= float(v) <= _hi):
                        logger.warning(f'  ⚠️ [_save_signal_cache] {k}={v} 정상 범위({_lo}~{_hi}) 이탈 — 기각')
                        continue
                safe_signals[k] = v
                if k.upper() in bounds_map and isinstance(v, (int, float)):
                    safe_signals[k.upper()] = v
            MacroRealtimeRefresher._enrich_and_sync_signal_dict(data, safe_signals, bounds_map)
            data['timestamp'] = datetime.now().isoformat()
            return data
        safe_json_update(_SIGNAL_CACHE, _update)
    except Exception as e:
        logger.error(f'  시그널 캐시 저장 실패: {e}', exc_info=True)

def collect_investor_flow() -> Dict:
    """외국인/기관 순매수 수집 (KIS API Primary -> pykrx_compat Fallback)."""
    flows = {}
    out = _DATA_DIR / 'investor_flow_kospi.parquet'
    _mkt_tk = '069500'  # hardcode-ok: L0 KOSPI200 benchmark ETF
    try:
        from src.data_collection.kis_data_collector import KISDataCollector
        kis = KISDataCollector()
        if kis._ensure_auth():
            url = f"{kis._base_url}/uapi/domestic-stock/v1/quotations/inquire-investor"
            data = kis._call(url, 'FHKST01010900', {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': _mkt_tk})
            rows = (data or {}).get('output', [])
            records = []
            for r in rows if isinstance(rows, list) else []:
                dt_s = str(r.get('stck_bsop_date', '')).strip()
                if not dt_s:
                    continue
                prsn = float(str(r.get('prsn_ntby_tr_pbmn', '0') or '0').replace(',', '')) * 1_000_000
                frgn = float(str(r.get('frgn_ntby_tr_pbmn', '0') or '0').replace(',', '')) * 1_000_000
                orgn = float(str(r.get('orgn_ntby_tr_pbmn', '0') or '0').replace(',', '')) * 1_000_000
                records.append({'날짜': pd.to_datetime(dt_s), '개인': prsn, '외국인합계': frgn, '기관합계': orgn})
            if records:
                df_new = pd.DataFrame(records).sort_values('날짜')
                if out.exists():
                    try:
                        df_old = pd.read_parquet(out)
                        if '날짜' in df_old.columns:
                            df_old['날짜'] = pd.to_datetime(df_old['날짜'])
                            df_new = pd.concat([df_old, df_new]).drop_duplicates(subset=['날짜'], keep='last').sort_values('날짜')
                    except Exception:
                        pass
                _DATA_DIR.mkdir(parents=True, exist_ok=True)
                safe_parquet_write(df_new.reset_index(drop=True), out)
                flows['kospi_flow_saved'] = len(df_new)
                logger.info(f'  ✅ KOSPI 수급 (KIS Primary): {len(df_new)}행')
                return flows
    except Exception as _kis_e:
        logger.debug(f'  KIS 수급 수집 스킵: {_kis_e}')
    try:
        from src.data_collection.pykrx_compat import stock as pykrx_stock
        end = datetime.now().strftime('%Y%m%d')
        start = (datetime.now() - timedelta(days=60)).strftime('%Y%m%d')
        df = pykrx_stock.get_market_trading_value_by_date(start, end, _mkt_tk)
        if df is not None and (not df.empty):
            safe_parquet_write(df.reset_index(), out)
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
    """KOSPI200 + KOSDAQ150 유니버스 갱신 (FDR 시가총액 상위 Primary)."""
    tickers = set()
    try:
        import FinanceDataReader as fdr
        df_kp = fdr.StockListing('KOSPI')
        if df_kp is not None and not df_kp.empty and 'Code' in df_kp.columns:
            kp_top = df_kp['Code'].dropna().astype(str).str.strip().tolist()[:200]
            tickers.update(kp_top)
            logger.info(f'  KOSPI 상위: {len(kp_top)}종목')
        df_kq = fdr.StockListing('KOSDAQ')
        if df_kq is not None and not df_kq.empty and 'Code' in df_kq.columns:
            kq_top = df_kq['Code'].dropna().astype(str).str.strip().tolist()[:150]
            tickers.update(kq_top)
            logger.info(f'  KOSDAQ 상위: {len(kq_top)}종목')
    except Exception as e:
        logger.error(f'  유니버스 갱신 실패: {e}', exc_info=True)
    ticker_list = sorted(t for t in tickers if _KR_TICKER_RE.match(t))
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
        from src.data_collection.pykrx_compat import stock as pykrx_stock
    except ImportError as e:
        logger.error('  pykrx_compat 미설치 — 종목명 갱신 스킵', exc_info=True)
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
    _default_stale_h = float(pget('data.freshness_stale_h', 20.0))
    _holiday_buffer_h = float(pget('data.freshness_holiday_buffer_h', 4.0))
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
        _final_thresh_h = float(freshness_results.get('threshold_h', 20.0)) if isinstance(freshness_results, dict) else 20.0
        final_stale = _count_stale_files(threshold_h=_final_thresh_h)
        results['final_stale_count'] = final_stale
        if final_stale > 0:
            logger.warning(f'  ⚠️ 시그널 생성 전 미갱신 파일 {final_stale}개 잔존 (기준: {_final_thresh_h:.1f}h)')
        else:
            logger.info(f'  ✅ 전 데이터 Fresh (기준: {_final_thresh_h:.1f}h) — 시그널 생성 준비 완료')
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
                raw_uni = json.loads(uni_file.read_text())
                tickers = raw_uni if isinstance(raw_uni, list) else raw_uni.get('tickers', raw_uni.get('kr_stocks', []))
                max_stocks = pget('collector.max_stock_tickers', 350)
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
                raw_uni = json.loads(uni_file.read_text())
                tickers = raw_uni if isinstance(raw_uni, list) else raw_uni.get('tickers', raw_uni.get('kr_stocks', []))
                max_stocks = pget('collector.max_stock_tickers', 350)
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

    1단: Direct Chart REST (fetch_yahoo_chart_ohlcv) / collect_global_macro
    2단: 실패 시 기존 parquet의 마지막 행 ffill 저장
    """
    from config.universe import Universe
    from src.data_collection.alpha_vantage_collector import fetch_yahoo_chart_ohlcv, collect_global_macro
    u = Universe()
    failed_tickers = []
    ffill_tickers = []
    for name, yf_ticker in u.SIGNAL_EVENING.items():
        out_file = _SIGNAL_DIR / f'signal_{name.lower()}.parquet'
        data = fetch_yahoo_chart_ohlcv(yf_ticker, range_str='3mo')
        if data is None or data.empty:
            macro_q = collect_global_macro([yf_ticker])
            if yf_ticker in macro_q and macro_q[yf_ticker].get('price', 0) > 0:
                px = float(macro_q[yf_ticker]['price'])
                today_dt = pd.Timestamp(datetime.now().date())
                data = pd.DataFrame([{'date': today_dt, 'open': px, 'high': px, 'low': px, 'close': px, 'volume': 0}]).set_index('date')
        if data is not None and not data.empty:
            try:
                _SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
                df = data.reset_index()
                df.columns = [c.lower() if isinstance(c, str) else c for c in df.columns]
                if hasattr(df.columns, 'levels'):
                    df.columns = ['_'.join(c).strip('_') if isinstance(c, tuple) else c for c in df.columns]
                if out_file.exists():
                    try:
                        old_df = pd.read_parquet(out_file)
                        old_df.columns = [str(c).lower() for c in old_df.columns]
                        if 'date' in old_df.columns and 'date' in df.columns:
                            old_df['date'] = pd.to_datetime(old_df['date']).dt.normalize()
                            df['date'] = pd.to_datetime(df['date']).dt.normalize()
                            df = pd.concat([old_df, df], ignore_index=True).drop_duplicates(subset=['date'], keep='last').sort_values('date')
                    except Exception:
                        pass
                safe_parquet_write(df, out_file)
                logger.info(f'  ✅ {name}({yf_ticker}): {len(df)}행 (실시간 1차 수집 성공)')
                continue
            except Exception as _save_e:
                logger.error(f'  ⚠️ {name} parquet 저장 실패: {_save_e}', exc_info=True)
        if out_file.exists():
            try:
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
        logger.warning(f'  ❌ {name}({yf_ticker}): 실시간 수집 + ffill 모두 실패 — 신호 누락')
    total = len(u.SIGNAL_EVENING)
    ok = total - len(failed_tickers) - len(ffill_tickers)
    logger.info(f'  📊 Evening Signals: 성공={ok}/{total}, ffill={len(ffill_tickers)}, 실패={len(failed_tickers)}')

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
            # 10년치 US 지수/국채/원자재/VIX/환율 시계열을 cross_*.parquet에 영속 병합
            _cross_dir = _PROJECT_ROOT / 'data' / 'cross_market'
            _cross_dir.mkdir(parents=True, exist_ok=True)
            _cross_series_targets = []
            _idx_df = result.get('indices')
            if isinstance(_idx_df, pd.DataFrame):
                if 'S&P_500' in _idx_df.columns:
                    _cross_series_targets.append((_idx_df['S&P_500'], 'cross_sp500.parquet'))
                if 'VIX' in _idx_df.columns:
                    _cross_series_targets.append((_idx_df['VIX'], 'cross_vix.parquet'))
            _tr_df = result.get('treasuries')
            if isinstance(_tr_df, pd.DataFrame) and 'US_10Y' in _tr_df.columns:
                _cross_series_targets.append((_tr_df['US_10Y'], 'cross_us10y.parquet'))
            _fx_df = result.get('currencies') or result.get('fx')
            if isinstance(_fx_df, pd.DataFrame):
                for _fx_col in ('USD_KRW', 'USDKRW', 'KRW'):
                    if _fx_col in _fx_df.columns:
                        _cross_series_targets.append((_fx_df[_fx_col], 'cross_usdkrw.parquet'))
                        break
            _cm_df = result.get('commodities')
            if isinstance(_cm_df, pd.DataFrame):
                if 'WTI_Oil' in _cm_df.columns:
                    _cross_series_targets.append((_cm_df['WTI_Oil'], 'cross_oil_futures.parquet'))
                if 'Gold' in _cm_df.columns:
                    _cross_series_targets.append((_cm_df['Gold'], 'cross_gold_futures.parquet'))
            for _ser, _cname in _cross_series_targets:
                try:
                    _s_clean = pd.to_numeric(_ser, errors='coerce').dropna()
                    _min_len = 10
                    if len(_s_clean) < _min_len:
                        continue
                    _new_df = pd.DataFrame({
                        'date': pd.to_datetime(_s_clean.index).tz_localize(None).normalize(),
                        'open': _s_clean.values,
                        'high': _s_clean.values,
                        'low': _s_clean.values,
                        'close': _s_clean.values,
                        'volume': 0,
                    })
                    for _tdir in (_cross_dir, _DATA_DIR):
                        _save_signal_parquet(_new_df, _tdir / _cname)
                except Exception as _ce:
                    logger.debug(f'  cross series sync skip ({_cname}): {_ce}')
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
    """US-JP 스프레드, ISM/Caixin PMI, 수익률곡선 및 cross_vix/cross_usdkrw 최신화."""
    try:
        from src.data_collection.cross_market_collector import CrossMarketCollector
        collector = CrossMarketCollector()
        result = collector.collect_all()
        # Sync cross_vix.parquet and cross_usdkrw.parquet from us_vix/signal_vix/^VIX and us_usdkrw/signal_usdkrw/KRW=X
        _cross_dir = _PROJECT_ROOT / 'data' / 'cross_market'
        _cross_dir.mkdir(parents=True, exist_ok=True)
        cross_map = {
            'us_vix.parquet': 'cross_vix.parquet',
            'us_usdkrw.parquet': 'cross_usdkrw.parquet',
            'signal_vix.parquet': 'cross_vix.parquet',
            'signal_usdkrw.parquet': 'cross_usdkrw.parquet',
            '^VIX.parquet': 'cross_vix.parquet',
            'KRW=X.parquet': 'cross_usdkrw.parquet',
        }
        _src_dirs = (_DATA_DIR, _SIGNAL_DIR, _PROJECT_ROOT / 'data' / 'us_stocks' / 'prices', _PROJECT_ROOT / 'data' / 'us_stocks')
        for src_name, dst_name in cross_map.items():
            for sdir in _src_dirs:
                spath = sdir / src_name
                if spath.exists():
                    try:
                        sdf = pd.read_parquet(spath)
                        if not sdf.empty:
                            for tdir in (_cross_dir, _DATA_DIR):
                                _save_signal_parquet(sdf, tdir / dst_name)
                    except Exception as _se:
                        logger.debug(f'  cross_map sync skip ({src_name} -> {dst_name}): {_se}')
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
            if pcr_val is not None:
                cache_updates['put_call_ratio'] = pcr_val
                cache_updates['options_pcr'] = pcr_val
            hz_val = result['put_call'].get('etf_hedge_zscore', result['put_call'].get('z_score'))
            if hz_val is not None:
                cache_updates['etf_hedge_zscore'] = hz_val
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
                raw_uni = json.loads(uni_file.read_text())
                uni_list = raw_uni if isinstance(raw_uni, list) else raw_uni.get('tickers', raw_uni.get('kr_stocks', []))
                tickers = uni_list[:20]
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
        from src.infra.safe_io import safe_json_update
        stale_files = []
        stale_threshold_h = pget('collector.stale_threshold_hours', 36)
        total_kr_parquets = 0
        if _DATA_DIR.exists():
            all_kr_pqs = sorted(_DATA_DIR.glob('kr_*.parquet'))
            total_kr_parquets = len(all_kr_pqs)
            for f in all_kr_pqs[:500]:
                age_h = (datetime.now() - datetime.fromtimestamp(f.stat().st_mtime)).total_seconds() / 3600
                if age_h > stale_threshold_h:
                    stale_files.append({'file': f.name, 'age_hours': round(age_h, 1)})
        fallback_snapshot = None
        if _GLOBAL_FALLBACK_EVENTS:
            fallback_snapshot = _GLOBAL_FALLBACK_EVENTS.copy()
            _GLOBAL_FALLBACK_EVENTS.clear()

        def _mutate(existing: dict) -> dict:
            if not isinstance(existing, dict):
                existing = {}
            phases = existing.get('phases', {})
            if not isinstance(phases, dict):
                phases = {}
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
            if fallback_snapshot:
                phase_entry['fallback_events'] = fallback_snapshot
            phases[phase_key] = phase_entry
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
            return {
                'last_updated': datetime.now().isoformat(),
                'phases': phases,
                'stale_files': stale_files[:50],
                'stale_count': len(stale_files),
                'overall': {
                    'total_kr_parquets': total_kr_parquets,
                    'fresh_count': total_kr_parquets - len(stale_files),
                    'stale_count': len(stale_files),
                    'success_rate': round(total_success / max(total_attempted, 1), 3),
                },
            }

        status_file.parent.mkdir(parents=True, exist_ok=True)
        safe_json_update(status_file, _mutate, strict=True)
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
        def _sanitize_val(v):
            import math
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return None
            if isinstance(v, dict):
                return {k: _sanitize_val(x) for k, x in v.items()}
            if isinstance(v, list):
                return [_sanitize_val(x) for x in v]
            return v

        sanitized = _sanitize_val(results)
        existing.append(sanitized)
        existing = [_sanitize_val(item) for item in existing[-30:]]
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
        max_stocks = pget('collector.max_stock_tickers', 350)
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