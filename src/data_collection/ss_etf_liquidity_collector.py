"""
SS-ETF Liquidity Collector — 단일종목 ETF 유동성 데이터 수집기
==============================================================

2026년 5월 27일 단일종목 레버리지/인버스 ETF 상장 이후 발생하는
'파생발 투기적 변동성(Wag-the-Dog Effect)'을 감지·방어하기 위해
관련 ETF의 거래량·투자자별 수급 데이터를 수집하는 전용 모듈.

[수집 대상]
  삼성전자(005930): 레버리지 ETF + 인버스 ETF
  SK하이닉스(000660): 레버리지 ETF + 인버스 ETF

[Graceful Fallback]
  - 상장일(2026-05-27) 이전 요청: 에러 없이 0/NaN 반환
  - pykrx 실패: 빈 DataFrame 반환 (파이프라인 중단 없음)
  - 개별 ETF 실패: 해당 ETF만 0 처리, 나머지는 정상 수집 계속

[Zero Hardcoding]
  - 모든 ETF 티커, 상장일, 임계값은 DynamicConfig에서 로드
  - config/dynamic_config.py의 'ss_etf.*' 키로 관리

Usage:
    from src.data_collection.ss_etf_liquidity_collector import SSETFLiquidityCollector
    collector = SSETFLiquidityCollector()
    df = collector.collect(target_date='20260623')
"""
from __future__ import annotations
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CACHE_DIR = _PROJECT_ROOT / 'data' / 'ss_etf_cache'
try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _cfg = None

def _dcfg(key: str, default):
    """DynamicConfig 조회 (없으면 default)."""
    if _cfg is not None:
        return _cfg.get(key, default)
    return default
_SS_ETF_LISTING_DATE_STR = _dcfg('ss_etf.listing_date', '20260527')
_SS_ETF_LISTING_DATE = datetime.strptime(_SS_ETF_LISTING_DATE_STR, '%Y%m%d').date()

def _get_etf_universe() -> Dict[str, Dict]:
    """DynamicConfig에서 단일종목 ETF 유니버스 로드.

    반환 형식:
        {
          '005930': {
            'name': '삼성전자',
            'lev_ticker': '...',
            'inv_ticker': '...',
          },
          '000660': { ... },
        }
    """
    default_universe = {'005930': {'name': '삼성전자', 'lev_ticker': _dcfg('ss_etf.samsung.lev_ticker', '470450'), 'inv_ticker': _dcfg('ss_etf.samsung.inv_ticker', '470460')}, '000660': {'name': 'SK하이닉스', 'lev_ticker': _dcfg('ss_etf.hynix.lev_ticker', '470480'), 'inv_ticker': _dcfg('ss_etf.hynix.inv_ticker', '470490')}}
    cfg_universe = _dcfg('ss_etf.universe', None)
    if cfg_universe and isinstance(cfg_universe, dict):
        default_universe.update(cfg_universe)
    return default_universe

def _is_before_listing(target_date_str: str) -> bool:
    """요청일이 상장일 이전이면 True."""
    try:
        req_date = datetime.strptime(target_date_str[:8], '%Y%m%d').date()
        return req_date < _SS_ETF_LISTING_DATE
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        return False

def _pykrx_safe(func, *args, max_retries: int=2, **kwargs) -> Optional[pd.DataFrame]:
    """pykrx 호출을 retry + rate-limit으로 감싼 wrapper."""
    for attempt in range(max_retries):
        try:
            time.sleep(0.3)
            result = func(*args, **kwargs)
            if result is not None and len(result) > 0:
                return result
        except Exception as e:
            logger.error(f'  pykrx call attempt {attempt + 1} 실패: {e}', exc_info=True)
            time.sleep(1.5 * (attempt + 1))
    return None

class SSETFLiquidityCollector:
    """단일종목 ETF 유동성 수집기.

    메서드:
        collect(target_date)         → 당일 전체 수집 (수집→파싱→캐시)
        collect_ohlcv(ticker, date)  → 개별 ETF OHLCV
        collect_investor(ticker, date) → 개별 ETF 투자자별 순매수
        get_cached(target_date)      → 캐시에서 로드
    """

    def __init__(self):
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._universe = _get_etf_universe()
        try:
            from pykrx import stock as _pykrx
            self._pykrx = _pykrx
        except ImportError as e:
            self._pykrx = None
            logger.error('  SS-ETF Collector: pykrx 미설치 → 수집 불가', exc_info=True)

    def collect(self, target_date: str=None) -> pd.DataFrame:
        """메인 수집 엔트리포인트.

        Args:
            target_date: 'YYYYMMDD' 형식. None이면 오늘.

        Returns:
            DataFrame: 수집된 유동성 데이터 (기초자산 기준 1행/종목)
            컬럼: [date, underlying, underlying_name,
                   lev_volume, lev_amount, lev_retail_net,
                   inv_volume, inv_amount, inv_retail_net,
                   underlying_volume]
        """
        if target_date is None:
            target_date = date.today().strftime('%Y%m%d')
        if _is_before_listing(target_date):
            logger.info(f'  SS-ETF: {target_date} < 상장일 {_SS_ETF_LISTING_DATE_STR} → 빈 DataFrame 반환 (사전 상장)')
            return self._empty_df(target_date)
        cached = self.get_cached(target_date)
        if cached is not None and len(cached) > 0:
            logger.info(f'  SS-ETF: {target_date} 캐시 로드 ({len(cached)}행)')
            return cached
        rows = []
        for underlying, info in self._universe.items():
            row = self._collect_single_underlying(underlying=underlying, name=info['name'], lev_ticker=info['lev_ticker'], inv_ticker=info['inv_ticker'], target_date=target_date)
            rows.append(row)
        df = pd.DataFrame(rows)
        if len(df) > 0:
            self._save_cache(df, target_date)
            logger.info(f'  SS-ETF: {target_date} 수집 완료 ({len(df)}종목, 총 레버리지 거래량={df['lev_volume'].sum():,.0f})')
        return df

    def collect_ohlcv(self, ticker: str, target_date: str) -> Optional[pd.Series]:
        """개별 ETF 당일 OHLCV 조회.

        Returns:
            Series: [open, high, low, close, volume, amount] or None
        """
        if self._pykrx is None:
            return None
        try:
            df = _pykrx_safe(self._pykrx.get_market_ohlcv_by_date, target_date, target_date, ticker)
            if df is None or len(df) == 0:
                return None
            row = df.iloc[-1]
            result = pd.Series({'open': float(row.get('시가', row.get('open', 0))), 'high': float(row.get('고가', row.get('high', 0))), 'low': float(row.get('저가', row.get('low', 0))), 'close': float(row.get('종가', row.get('close', 0))), 'volume': float(row.get('거래량', row.get('volume', 0))), 'amount': float(row.get('거래대금', row.get('amount', 0)))})
            return result
        except Exception as e:
            logger.error(f'  SS-ETF OHLCV [{ticker}] 실패: {e}', exc_info=True)
            return None

    def collect_investor(self, ticker: str, target_date: str) -> Optional[pd.Series]:
        """개별 ETF 당일 투자자별 순매수 조회.

        Returns:
            Series: [retail_net_buy, foreign_net_buy, inst_net_buy] (금액, 원) or None
        """
        if self._pykrx is None:
            return None
        try:
            df = _pykrx_safe(self._pykrx.get_market_net_purchases_of_equities_by_ticker, target_date, target_date, ticker)
            if df is None or len(df) == 0:
                df2 = _pykrx_safe(self._pykrx.get_market_trading_value_by_date, target_date, target_date, ticker)
                if df2 is not None and len(df2) > 0:
                    row2 = df2.iloc[-1]
                    retail_net = float(row2.get('개인', row2.get('retail', 0)) or 0)
                    return pd.Series({'retail_net_buy': retail_net, 'foreign_net_buy': float(row2.get('외국인', row2.get('foreign', 0)) or 0), 'inst_net_buy': float(row2.get('기관', row2.get('institution', 0)) or 0)})
                return None
            row = df.iloc[-1] if len(df) == 1 else df[df.index.astype(str).str[:6] == ticker[:6]].iloc[-1] if ticker in df.index.astype(str).values else df.iloc[0]
            retail_net = float(row.get('개인', row.get('retail', 0)) or 0)
            foreign_net = float(row.get('외국인', row.get('foreign', 0)) or 0)
            inst_net = float(row.get('기관합계', row.get('institution', 0)) or 0)
            return pd.Series({'retail_net_buy': retail_net, 'foreign_net_buy': foreign_net, 'inst_net_buy': inst_net})
        except Exception as e:
            logger.error(f'  SS-ETF Investor [{ticker}] 실패: {e}', exc_info=True)
            return None

    def get_cached(self, target_date: str) -> Optional[pd.DataFrame]:
        """캐시 파일에서 로드."""
        cache_path = _CACHE_DIR / f'ss_etf_{target_date}.parquet'
        if cache_path.exists():
            try:
                return pd.read_parquet(cache_path)
            except Exception as e:
                logger.error(f'  SS-ETF 캐시 읽기 실패: {e}', exc_info=True)
        return None

    def _collect_single_underlying(self, underlying: str, name: str, lev_ticker: str, inv_ticker: str, target_date: str) -> Dict:
        """단일 기초자산에 대한 레버리지 + 인버스 ETF 수집."""
        row: Dict = {'date': target_date, 'underlying': underlying, 'underlying_name': name, 'lev_volume': 0.0, 'lev_amount': 0.0, 'lev_retail_net': 0.0, 'inv_volume': 0.0, 'inv_amount': 0.0, 'inv_retail_net': 0.0, 'underlying_volume': 0.0}
        try:
            lev_ohlcv = self.collect_ohlcv(lev_ticker, target_date)
            if lev_ohlcv is not None:
                row['lev_volume'] = lev_ohlcv.get('volume', 0.0)
                row['lev_amount'] = lev_ohlcv.get('amount', 0.0)
        except Exception as e:
            logger.warning(f'  SS-ETF [{name}] 레버리지 OHLCV 실패: {e}', exc_info=True)
        try:
            lev_inv_data = self.collect_investor(lev_ticker, target_date)
            if lev_inv_data is not None:
                row['lev_retail_net'] = lev_inv_data.get('retail_net_buy', 0.0)
        except Exception as e:
            logger.warning(f'  SS-ETF [{name}] 레버리지 수급 실패: {e}', exc_info=True)
        try:
            inv_ohlcv = self.collect_ohlcv(inv_ticker, target_date)
            if inv_ohlcv is not None:
                row['inv_volume'] = inv_ohlcv.get('volume', 0.0)
                row['inv_amount'] = inv_ohlcv.get('amount', 0.0)
        except Exception as e:
            logger.warning(f'  SS-ETF [{name}] 인버스 OHLCV 실패: {e}', exc_info=True)
        try:
            inv_inv_data = self.collect_investor(inv_ticker, target_date)
            if inv_inv_data is not None:
                row['inv_retail_net'] = inv_inv_data.get('retail_net_buy', 0.0)
        except Exception as e:
            logger.warning(f'  SS-ETF [{name}] 인버스 수급 실패: {e}', exc_info=True)
        try:
            if self._pykrx is not None:
                ul_df = _pykrx_safe(self._pykrx.get_market_ohlcv_by_date, target_date, target_date, underlying)
                if ul_df is not None and len(ul_df) > 0:
                    ul_row = ul_df.iloc[-1]
                    row['underlying_volume'] = float(ul_row.get('거래량', ul_row.get('volume', 0)) or 0)
        except Exception as e:
            logger.warning(f'  SS-ETF [{name}] 기초자산 거래량 실패: {e}', exc_info=True)
        return row

    def _empty_df(self, target_date: str) -> pd.DataFrame:
        """상장 이전 / 수집 실패 시 빈 기본 DataFrame 반환."""
        rows = []
        for underlying, info in self._universe.items():
            rows.append({'date': target_date, 'underlying': underlying, 'underlying_name': info['name'], 'lev_volume': 0.0, 'lev_amount': 0.0, 'lev_retail_net': 0.0, 'inv_volume': 0.0, 'inv_amount': 0.0, 'inv_retail_net': 0.0, 'underlying_volume': 0.0})
        return pd.DataFrame(rows)

    def _save_cache(self, df: pd.DataFrame, target_date: str):
        """수집 결과 parquet 캐시 저장."""
        try:
            path = _CACHE_DIR / f'ss_etf_{target_date}.parquet'
            df.to_parquet(path, index=False)
        except Exception as e:
            logger.error(f'  SS-ETF 캐시 저장 실패: {e}', exc_info=True)

def collect_ss_etf_data(target_date: str=None) -> pd.DataFrame:
    """단일종목 ETF 데이터 수집 편의 함수."""
    return SSETFLiquidityCollector().collect(target_date)