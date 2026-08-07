"""
Resilient Market Data Fetcher v1.0
===================================
yfinance 단일 실패점(SPOF) 제거를 위한 이중화 데이터 페처.

Fallback 체인:
  1. yfinance (무료, 빠름)
  2. FMP (Financial Modeling Prep, API 키 필요)
  3. Alpha Vantage (API 키 필요, 분당 5건 제한)
  4. 로컬 Parquet 캐시 (stale 허용)

추가 기능:
  - Rate Limit 큐잉 (초당/분당 제한 자동 준수)
  - Data Freshness 모니터링 (stale 데이터 감지 + 로깅)
  - 자동 캐시 저장 (성공 시 parquet 갱신)

Author: Project-A | Date: 2026-05-11
"""
import json
import logging
import os
import time
import threading
from collections import deque
from src.utils.file_ops import atomic_write_json

from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Dict, Optional, Tuple
import pandas as pd
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CACHE_DIR = _PROJECT_ROOT / 'data' / 'market_data_cache'
_FRESHNESS_LOG = _PROJECT_ROOT / 'results' / 'data_freshness_log.json'

class RateLimiter:
    """Thread-safe Token Bucket Rate Limiter.

    사용법:
        limiter = RateLimiter(max_calls=5, period=1.0)  # 초당 5건
        limiter.acquire()  # 호출 전 대기
    """

    def __init__(self, max_calls: int=5, period: float=1.0, name: str='default'):
        self.max_calls = max_calls
        self.period = period
        self.name = name
        self._lock = threading.Lock()
        self._calls: deque = deque()

    def acquire(self) -> None:
        """Rate Limit 준수를 위해 필요 시 대기."""
        with self._lock:
            now = time.monotonic()
            while self._calls and self._calls[0] <= now - self.period:
                self._calls.popleft()
            if len(self._calls) >= self.max_calls:
                sleep_time = self._calls[0] + self.period - now
                if sleep_time > 0:
                    logger.debug(f'  ⏳ RateLimit[{self.name}]: {sleep_time:.2f}s 대기 ({len(self._calls)}/{self.max_calls})')
                    time.sleep(sleep_time)
            self._calls.append(time.monotonic())

    @property
    def usage_pct(self) -> float:
        """현재 사용률 (0~1)."""
        with self._lock:
            now = time.monotonic()
            while self._calls and self._calls[0] <= now - self.period:
                self._calls.popleft()
            return len(self._calls) / max(self.max_calls, 1)
_LIMITERS: Dict[str, RateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()

def get_limiter(name: str, max_calls: int=5, period: float=1.0) -> RateLimiter:
    """Named Rate Limiter 싱글턴 조회."""
    with _LIMITERS_LOCK:
        if name not in _LIMITERS:
            _LIMITERS[name] = RateLimiter(max_calls, period, name)
        return _LIMITERS[name]
YFINANCE_LIMITER = get_limiter('yfinance', max_calls=5, period=1.0)
FMP_LIMITER = get_limiter('fmp', max_calls=5, period=1.0)
AV_LIMITER = get_limiter('alpha_vantage', max_calls=5, period=60.0)
KIS_LIMITER = get_limiter('kis', max_calls=18, period=1.0)

class FreshnessMonitor:
    """데이터 소스별 freshness 추적 및 stale 감지."""

    def __init__(self):
        self._records: Dict[str, dict] = {}
        self._load()

    def _load(self):
        try:
            if _FRESHNESS_LOG.exists():
                self._records = json.loads(_FRESHNESS_LOG.read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            self._records = {}

    def record(self, source: str, ticker: str, success: bool, data_date: str='', latency_ms: float=0):
        """데이터 수집 결과 기록."""
        key = f'{source}:{ticker}'
        now = datetime.now().isoformat()
        self._records[key] = {'source': source, 'ticker': ticker, 'success': success, 'data_date': data_date or date.today().isoformat(), 'fetched_at': now, 'latency_ms': round(latency_ms, 1)}
        if len(self._records) % 100 == 0:
            self.save()

    def check_staleness(self, source: str, ticker: str, max_age_hours: int=24) -> Tuple[bool, str]:
        """데이터 staleness 확인.

        Returns:
            (is_stale, reason)
        """
        key = f'{source}:{ticker}'
        rec = self._records.get(key)
        if not rec:
            return (True, 'no_record')
        try:
            fetched = datetime.fromisoformat(rec['fetched_at'])
            age = datetime.now() - fetched
            if age > timedelta(hours=max_age_hours):
                return (True, f'stale_{age.total_seconds() / 3600:.0f}h')
        except (ValueError, KeyError):
            return (True, 'parse_error')
        if not rec.get('success', False):
            return (True, 'last_fetch_failed')
        return (False, 'fresh')

    def get_summary(self) -> Dict:
        """소스별 freshness 요약."""
        summary: Dict[str, dict] = {}
        for key, rec in self._records.items():
            src = rec.get('source', 'unknown')
            if src not in summary:
                summary[src] = {'total': 0, 'success': 0, 'stale': 0}
            summary[src]['total'] += 1
            if rec.get('success'):
                summary[src]['success'] += 1
            is_stale, _ = self.check_staleness(rec.get('source', ''), rec.get('ticker', ''))
            if is_stale:
                summary[src]['stale'] += 1
        return summary

    def save(self):
        """디스크에 저장."""
        try:
            _FRESHNESS_LOG.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(_FRESHNESS_LOG, self._records, ensure_ascii=False, indent=2)
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  Freshness log save: {e}')
_FRESHNESS = FreshnessMonitor()

class ResilientFetcher:
    """yfinance + FMP + Alpha Vantage 이중화 데이터 페처.

    사용법:
        fetcher = ResilientFetcher()
        df = fetcher.get_price_history('AAPL', period='1y')
        price = fetcher.get_current_price('^VIX')
    """

    def __init__(self):
        from src.utils.credential_manager import CredentialManager
        cm = CredentialManager()
        self._fmp_key = cm.read_from_env('FMP_API_KEY') or ''
        self._av_key = cm.read_from_env('ALPHA_VANTAGE_API_KEY') or ''
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def get_price_history(self, ticker: str, period: str='1y', interval: str='1d') -> Optional[pd.DataFrame]:
        """이중화 가격 히스토리 조회.

        Fallback: yfinance → FMP → Alpha Vantage → parquet 캐시
        """
        df = self._yf_history(ticker, period, interval)
        if df is not None and (not df.empty):
            self._save_cache(ticker, df)
            return df
        if self._fmp_key:
            df = self._fmp_history(ticker, period)
            if df is not None and (not df.empty):
                self._save_cache(ticker, df)
                return df
        if self._av_key:
            df = self._av_history(ticker)
            if df is not None and (not df.empty):
                self._save_cache(ticker, df)
                return df
        df = self._load_cache(ticker)
        if df is not None and (not df.empty):
            is_stale, reason = _FRESHNESS.check_staleness('cache', ticker)
            if is_stale:
                logger.warning(f'  ⚠️ {ticker}: stale 캐시 사용 ({reason})')
            return df
        logger.error(f'  ❌ {ticker}: 모든 소스 실패')
        return None

    def get_current_price(self, ticker: str) -> Optional[float]:
        """이중화 현재가 조회.

        Fallback: KIS API(국내) → yfinance → FMP → 캐시 최신 close
        """
        is_kr = False
        kis_ticker = ticker
        if ticker.endswith('.KS') or ticker.endswith('.KQ'):
            is_kr = True
            kis_ticker = ticker.split('.')[0]
        elif len(ticker) == 6 and ticker.isdigit():
            is_kr = True
        if is_kr:
            try:
                from src.data_collection.kis_data_collector import KISDataCollector
                kis = KISDataCollector()
                price_data = kis.get_current_price(kis_ticker)
                if price_data and price_data.get('price', 0) > 0:
                    val = float(price_data['price'])
                    logger.debug(f'  📊 KIS API 실시간 호가 성공: {kis_ticker} ({val:,.0f}원)')
                    return val
            except Exception as e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                logger.debug(f'  KIS API 현재가 조회 실패 ({ticker}): {e}')
        price = self._yf_current(ticker)
        if price and price > 0:
            return price
        if self._fmp_key:
            price = self._fmp_current(ticker)
            if price and price > 0:
                return price
        df = self._load_cache(ticker)
        if df is not None and (not df.empty):
            price = float(df['close'].iloc[-1])
            logger.warning(f'  ⚠️ {ticker}: 캐시 가격 사용 ({price:.2f})')
            return price
        return None

    def _yf_history(self, ticker: str, period: str, interval: str) -> Optional[pd.DataFrame]:
        t0 = time.time()
        try:
            YFINANCE_LIMITER.acquire()
            import yfinance as yf
            df = yf.download(ticker, period=period, interval=interval, progress=False, timeout=10)
            latency = (time.time() - t0) * 1000
            if df is not None and (not df.empty):
                df.columns = [c.lower() if isinstance(c, str) else c[0].lower() for c in df.columns]
                _FRESHNESS.record('yfinance', ticker, True, str(df.index[-1].date()), latency)
                return df
            _FRESHNESS.record('yfinance', ticker, False, '', latency)
        except Exception as e:
            latency = (time.time() - t0) * 1000
            _FRESHNESS.record('yfinance', ticker, False, '', latency)
            logger.debug(f'  yfinance {ticker}: {e}')
        return None

    def _yf_current(self, ticker: str) -> Optional[float]:
        try:
            YFINANCE_LIMITER.acquire()
            import yfinance as yf
            t = yf.Ticker(ticker)
            hist = t.history(period='1d')
            if not hist.empty:
                return float(hist['Close'].iloc[-1])
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  yfinance current {ticker}: {e}')
        return None

    def _fmp_history(self, ticker: str, period: str) -> Optional[pd.DataFrame]:
        t0 = time.time()
        try:
            import requests
            FMP_LIMITER.acquire()
            days_map = {'1mo': 30, '3mo': 90, '6mo': 180, '1y': 365, '2y': 730, '5y': 1825, '10y': 3650}
            days = days_map.get(period, 365)
            url = f'https://financialmodelingprep.com/api/v3/historical-price-full/{ticker}'
            params = {'apikey': self._fmp_key, 'timeseries': days}
            resp = requests.get(url, params=params, timeout=15)
            data = resp.json()
            if 'historical' not in data:
                _FRESHNESS.record('fmp', ticker, False)
                return None
            df = pd.DataFrame(data['historical'])
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date').sort_index()
            df = df.rename(columns={'open': 'open', 'high': 'high', 'low': 'low', 'close': 'close', 'volume': 'volume'})
            latency = (time.time() - t0) * 1000
            _FRESHNESS.record('fmp', ticker, True, str(df.index[-1].date()), latency)
            logger.info(f'  📊 FMP fallback 성공: {ticker} ({len(df)}일)')
            return df[['open', 'high', 'low', 'close', 'volume']]
        except Exception as e:
            _FRESHNESS.record('fmp', ticker, False)
            logger.debug(f'  FMP {ticker}: {e}')
        return None

    def _fmp_current(self, ticker: str) -> Optional[float]:
        try:
            import requests
            FMP_LIMITER.acquire()
            url = f'https://financialmodelingprep.com/api/v3/quote-short/{ticker}'
            resp = requests.get(url, params={'apikey': self._fmp_key}, timeout=10)
            data = resp.json()
            if data and isinstance(data, list):
                return float(data[0].get('price', 0))
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  FMP current {ticker}: {e}')
        return None

    def _av_history(self, ticker: str) -> Optional[pd.DataFrame]:
        t0 = time.time()
        try:
            import requests
            AV_LIMITER.acquire()
            url = 'https://www.alphavantage.co/query'
            params = {'function': 'TIME_SERIES_DAILY', 'symbol': ticker, 'apikey': self._av_key, 'outputsize': 'full'}
            resp = requests.get(url, params=params, timeout=20)
            data = resp.json()
            ts = data.get('Time Series (Daily)', {})
            if not ts:
                _FRESHNESS.record('alpha_vantage', ticker, False)
                return None
            rows = []
            for dt_str, vals in ts.items():
                rows.append({'date': pd.Timestamp(dt_str), 'open': float(vals['1. open']), 'high': float(vals['2. high']), 'low': float(vals['3. low']), 'close': float(vals['4. close']), 'volume': int(vals['5. volume'])})
            df = pd.DataFrame(rows).set_index('date').sort_index()
            latency = (time.time() - t0) * 1000
            _FRESHNESS.record('alpha_vantage', ticker, True, str(df.index[-1].date()), latency)
            logger.info(f'  📊 Alpha Vantage fallback 성공: {ticker} ({len(df)}일)')
            return df
        except Exception as e:
            _FRESHNESS.record('alpha_vantage', ticker, False)
            logger.debug(f'  Alpha Vantage {ticker}: {e}')
        return None

    def _cache_path(self, ticker: str) -> Path:
        safe = ticker.replace('^', '_').replace('/', '_')
        return _CACHE_DIR / f'{safe}.parquet'

    def _save_cache(self, ticker: str, df: pd.DataFrame):
        try:
            path = self._cache_path(ticker)
            df.to_parquet(path, engine='pyarrow')
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  Cache save {ticker}: {e}')

    def _load_cache(self, ticker: str) -> Optional[pd.DataFrame]:
        path = self._cache_path(ticker)
        if path.exists():
            try:
                return pd.read_parquet(path)
            except Exception as e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                logger.debug(f'  Cache load {ticker}: {e}')
        return None
_FETCHER: Optional[ResilientFetcher] = None

def get_fetcher() -> ResilientFetcher:
    """싱글턴 ResilientFetcher 조회."""
    global _FETCHER
    if _FETCHER is None:
        _FETCHER = ResilientFetcher()
    return _FETCHER

def resilient_download(ticker: str, period: str='1y', interval: str='1d') -> Optional[pd.DataFrame]:
    """yf.download() 대체 — 이중화 + Rate Limit + Freshness."""
    return get_fetcher().get_price_history(ticker, period, interval)

def resilient_price(ticker: str) -> Optional[float]:
    """현재가 조회 — 이중화."""
    return get_fetcher().get_current_price(ticker)

def get_freshness_summary() -> Dict:
    """데이터 소스별 freshness 요약."""
    return _FRESHNESS.get_summary()