from __future__ import annotations
"""
SS-ETF Feature Engine — 단일종목 ETF 유동성 팩터 계산기
========================================================

'Wag-the-Dog Effect' (파생발 투기적 변동성) 감지를 위한
3가지 핵심 Feature를 계산합니다:

  1. ss_etf_vol_ratio      : 웩더독 강도
     = (레버리지 거래량 + 인버스 거래량) / 기초자산 거래량

  2. lp_delta_pressure     : LP 델타 헤징 압력
     = 레버리지 ETF 개인 순매수 - 인버스 ETF 개인 순매수
     (양수 → LP의 장막판 기초자산 매도 헤징 예상)

  3. intraday_vol_anomaly  : 일중 변동성 이상치
     = 오늘 일중 변동폭(High-Low) / 최근 N일 평균 변동폭

[ML 파이프라인 통합]
  - 상장일(2026-05-27) 이전 기간: 모든 Feature = 0.0 (NaN 없음)
  - StandardScaler 입력 전 안전하게 fillna(0.0) 처리
  - S1·S2 Feature DataFrame에 안전 병합 (pd.merge, how='left')

[Zero Hardcoding]
  - 모든 Feature 이름, 임계값은 DynamicConfig ss_etf.* 키로 관리

Usage:
    from src.data_collection.ss_etf_feature_engine import SSETFFeatureEngine
    engine = SSETFFeatureEngine()
    features = engine.compute(target_ticker='005930', target_date='20260623')
    df_merged = engine.merge_into_ml_df(df_ml, target_date='20260623')
"""
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _cfg = None

def _dcfg(key: str, default):
    return _cfg.get(key, default) if _cfg is not None else default
_LISTING_DATE_STR = _dcfg('ss_etf.listing_date', '20260527')
_LISTING_DATE = datetime.strptime(_LISTING_DATE_STR, '%Y%m%d').date()
_FEAT_VOL_RATIO = _dcfg('ss_etf.feature.vol_ratio_name', 'ss_etf_vol_ratio')
_FEAT_LP_PRESSURE = _dcfg('ss_etf.feature.lp_pressure_name', 'lp_delta_pressure')
_FEAT_VOL_ANOMALY = _dcfg('ss_etf.feature.vol_anomaly_name', 'intraday_vol_anomaly')
_VOL_ANOMALY_WINDOW = _dcfg('ss_etf.vol_anomaly_window_days', 14)
SS_ETF_FEATURE_NAMES: List[str] = [_FEAT_VOL_RATIO, _FEAT_LP_PRESSURE, _FEAT_VOL_ANOMALY]

def _is_before_listing(target_date_str: str) -> bool:
    """요청일이 상장일 이전이면 True."""
    try:
        req_date = datetime.strptime(str(target_date_str)[:8], '%Y%m%d').date()
        return req_date < _LISTING_DATE
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        return False

class SSETFFeatureEngine:
    """단일종목 ETF 유동성 팩터 계산 엔진.

    메서드:
        compute(ticker, date)            → 특정 종목·날짜의 Feature dict
        compute_batch(tickers, date)     → 복수 종목 Feature DataFrame
        merge_into_ml_df(df, date)       → ML 학습용 DF에 Feature 병합
        get_historical_features(ticker, start, end) → 히스토리 Feature DF
    """

    def __init__(self):
        from src.data_collection.ss_etf_liquidity_collector import SSETFLiquidityCollector
        self._collector = SSETFLiquidityCollector()
        self._universe = self._collector._universe

    def compute(self, target_ticker: str, target_date: str=None, raw_df: pd.DataFrame=None, intraday_data: dict=None) -> Dict[str, float]:
        """단일 종목·날짜의 SS-ETF Feature 계산.

        Args:
            target_ticker: 기초자산 코드 (e.g. '005930')
            target_date:   'YYYYMMDD'. None=오늘
            raw_df:        이미 수집된 DataFrame (None이면 자동 수집)
            intraday_data: 장중 실시간 데이터 딕셔너리 (제공될 경우 최우선으로 사용)

        Returns:
            {
              'ss_etf_vol_ratio':     float,
              'lp_delta_pressure':    float,   (백만원 단위로 스케일)
              'intraday_vol_anomaly': float,
            }
            상장 이전이면 모든 값 = 0.0
        """
        if target_date is None:
            target_date = date.today().strftime('%Y%m%d')
        if _is_before_listing(target_date):
            return self._zero_features()
        if target_ticker not in self._universe and intraday_data is None:
            logger.debug(f'  SS-ETF: {target_ticker}는 단일종목 ETF 대상 아님 → 0 반환')
            return self._zero_features()
        if intraday_data is not None:
            row = pd.Series(intraday_data)
            return self._calc_features(row, target_ticker, target_date)
        if raw_df is None:
            raw_df = self._collector.collect(target_date)
        if raw_df is None or len(raw_df) == 0:
            return self._zero_features()
        row = raw_df[raw_df['underlying'] == target_ticker]
        if len(row) == 0:
            return self._zero_features()
        row = row.iloc[0]
        return self._calc_features(row, target_ticker, target_date)

    def compute_batch(self, target_tickers: Optional[List[str]]=None, target_date: str=None) -> pd.DataFrame:
        """복수 종목의 SS-ETF Feature DataFrame 반환.

        Returns:
            DataFrame: 인덱스=ticker, 컬럼=Feature 이름들
            수집 대상 외 티커: 0.0 채움
        """
        if target_date is None:
            target_date = date.today().strftime('%Y%m%d')
        tickers = target_tickers or list(self._universe.keys())
        result_rows = []
        if _is_before_listing(target_date):
            for t in tickers:
                row = {'ticker': t, **self._zero_features()}
                result_rows.append(row)
            df = pd.DataFrame(result_rows).set_index('ticker')
            return df
        raw_df = self._collector.collect(target_date)
        for ticker in tickers:
            features = self.compute(target_ticker=ticker, target_date=target_date, raw_df=raw_df)
            result_rows.append({'ticker': ticker, **features})
        df = pd.DataFrame(result_rows).set_index('ticker')
        return df

    def merge_into_ml_df(self, df_ml: pd.DataFrame, target_date: str=None, ticker_col: str='ticker', date_col: str='date') -> pd.DataFrame:
        """ML 학습용 DataFrame에 SS-ETF Feature를 안전하게 병합.

        [설계 원칙]
          - pd.merge(how='left'): 기존 데이터 손실 없음
          - 결측치(NaN) → 0.0 자동 채움 (StandardScaler 안전)
          - 상장 이전 날짜: 모든 Feature = 0.0

        Args:
            df_ml:       기존 ML Feature DataFrame (ticker 컬럼 필요)
            target_date: 'YYYYMMDD'. None=df_ml의 date 컬럼 중 최신 날짜
            ticker_col:  ticker 컬럼명 (기본 'ticker')
            date_col:    date 컬럼명 (기본 'date')

        Returns:
            SS-ETF Feature가 추가된 DataFrame
        """
        if df_ml is None or len(df_ml) == 0:
            return df_ml
        if target_date is None:
            if date_col in df_ml.columns:
                latest = str(df_ml[date_col].max()).replace('-', '')[:8]
                target_date = latest
            else:
                target_date = date.today().strftime('%Y%m%d')
        tickers_in_df = df_ml[ticker_col].unique().tolist() if ticker_col in df_ml.columns else []
        all_tickers = list(set(tickers_in_df + list(self._universe.keys())))
        feat_df = self.compute_batch(target_tickers=all_tickers, target_date=target_date).reset_index().rename(columns={'index': ticker_col})
        if feat_df.index.name == 'ticker':
            feat_df = feat_df.reset_index()
        if 'ticker' in feat_df.columns and ticker_col != 'ticker':
            feat_df = feat_df.rename(columns={'ticker': ticker_col})
        df_out = pd.merge(df_ml, feat_df, on=ticker_col, how='left', suffixes=('', '_ss_etf'))
        for feat_name in SS_ETF_FEATURE_NAMES:
            if feat_name in df_out.columns:
                df_out[feat_name] = df_out[feat_name].fillna(0.0)
            else:
                df_out[feat_name] = 0.0
        logger.info(f'  SS-ETF merge: {len(df_ml)}행 → {len(df_out)}행, Features: {SS_ETF_FEATURE_NAMES}')
        return df_out

    def get_historical_features(self, target_ticker: str, start_date: str, end_date: str=None) -> pd.DataFrame:
        """지정 기간의 SS-ETF Feature 히스토리 DataFrame 반환.

        상장일 이전 날짜는 0.0으로 채워 ML 학습 데이터 연속성 보장.

        Args:
            target_ticker: 기초자산 코드
            start_date:    'YYYYMMDD'
            end_date:      'YYYYMMDD'. None=오늘

        Returns:
            DataFrame: 인덱스=date, 컬럼=Feature 이름들
        """
        from datetime import datetime
        if end_date is None:
            end_date = date.today().strftime('%Y%m%d')
        start_dt = datetime.strptime(start_date, '%Y%m%d').date()
        end_dt = datetime.strptime(end_date, '%Y%m%d').date()
        rows = []
        current = start_dt
        while current <= end_dt:
            date_str = current.strftime('%Y%m%d')
            if current.weekday() >= 5:
                current += timedelta(days=1)
                continue
            if _is_before_listing(date_str):
                rows.append({'date': date_str, **self._zero_features()})
            else:
                try:
                    feat = self.compute(target_ticker, date_str)
                    rows.append({'date': date_str, **feat})
                except Exception as e:
                    logger.error(f'  SS-ETF hist [{date_str}] 실패: {e}', exc_info=True)
                    rows.append({'date': date_str, **self._zero_features()})
            current += timedelta(days=1)
        df = pd.DataFrame(rows)
        if len(df) > 0:
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date').sort_index()
        return df

    def _calc_features(self, row: pd.Series, underlying: str, target_date: str) -> Dict[str, float]:
        """수집된 raw row에서 3개 Feature 계산.

        Feature 1: ss_etf_vol_ratio (웩더독 강도)
            = (lev_volume + inv_volume) / underlying_volume
            본주 대비 파생 ETF 거래 비중.

        Feature 2: lp_delta_pressure (LP 델타 헤징 압력)
            = (lev_retail_net - inv_retail_net) / 1_000_000  (백만원 스케일)
            양수 → LP 장막판 기초자산 매도 압력.

        Feature 3: intraday_vol_anomaly (일중 변동성 이상치)
            = 기초자산의 오늘 H-L 변동폭 / 최근 N일 평균 H-L
            파생 수급으로 변동성이 튀는 날 식별.
        """
        results = self._zero_features()
        try:
            if 'ss_etf_vol_ratio' in row:
                results[_FEAT_VOL_RATIO] = float(row['ss_etf_vol_ratio'])
            else:
                lev_vol = float(row.get('lev_volume', 0) or 0)
                inv_vol = float(row.get('inv_volume', 0) or 0)
                ul_vol = float(row.get('underlying_volume', 0) or 0)
                if ul_vol > 0:
                    vol_ratio = (lev_vol + inv_vol) / ul_vol
                    results[_FEAT_VOL_RATIO] = round(vol_ratio, 6)
        except Exception as e:
            logger.error(f'  SS-ETF vol_ratio 계산 실패: {e}', exc_info=True)
        try:
            if 'lp_delta_pressure' in row:
                results[_FEAT_LP_PRESSURE] = float(row['lp_delta_pressure'])
            else:
                lev_retail = float(row.get('lev_retail_net', 0) or 0)
                inv_retail = float(row.get('inv_retail_net', 0) or 0)
                scale = _dcfg('ss_etf.lp_pressure_scale', 1000000)
                lp_pressure = (lev_retail - inv_retail) / scale
                results[_FEAT_LP_PRESSURE] = round(lp_pressure, 4)
        except Exception as e:
            logger.error(f'  SS-ETF lp_delta_pressure 계산 실패: {e}', exc_info=True)
        try:
            if 'intraday_vol_anomaly' in row:
                results[_FEAT_VOL_ANOMALY] = float(row['intraday_vol_anomaly'])
            else:
                anomaly = self._compute_vol_anomaly(underlying, target_date)
                results[_FEAT_VOL_ANOMALY] = round(anomaly, 4)
        except Exception as e:
            logger.error(f'  SS-ETF vol_anomaly 계산 실패: {e}', exc_info=True)
        return results

    def _compute_vol_anomaly(self, ticker: str, target_date: str) -> float:
        """일중 변동성 이상치 = 오늘 H-L / 최근 N일 평균 H-L.

        pykrx에서 기초자산의 일봉을 조회하여 계산.
        실패 시 0.0 반환 (Graceful).
        """
        pykrx = self._collector._pykrx
        if pykrx is None:
            return 0.0
        try:
            window = _dcfg('ss_etf.vol_anomaly_window_days', 14)
            end_dt = datetime.strptime(target_date, '%Y%m%d').date()
            start_dt = end_dt - timedelta(days=window * 2)
            df = _pykrx_safe(pykrx.get_market_ohlcv_by_date, start_dt.strftime('%Y%m%d'), target_date, ticker)
            if df is None or len(df) < 2:
                return 0.0
            highs = df.get('고가', df.get('high', None))
            lows = df.get('저가', df.get('low', None))
            closes = df.get('종가', df.get('close', None))
            if highs is None or lows is None:
                return 0.0
            highs = highs.astype(float)
            lows = lows.astype(float)
            if closes is not None:
                closes = closes.astype(float)
                hl_pct = (highs - lows) / closes.where(closes > 0, other=np.nan)
            else:
                hl_pct = highs - lows
            hl_pct = hl_pct.dropna()
            if len(hl_pct) < 2:
                return 0.0
            today_hl = float(hl_pct.iloc[-1])
            hist_mean = float(hl_pct.iloc[:-1].tail(window).mean())
            if hist_mean <= 0:
                return 0.0
            return today_hl / hist_mean
        except Exception as e:
            logger.error(f'  SS-ETF vol_anomaly [{ticker}] 계산 실패: {e}', exc_info=True)
            return 0.0

    @staticmethod
    def _zero_features() -> Dict[str, float]:
        """상장 이전 / 수집 실패 기본값 — 모두 0.0."""
        return {_FEAT_VOL_RATIO: 0.0, _FEAT_LP_PRESSURE: 0.0, _FEAT_VOL_ANOMALY: 0.0}

def _pykrx_safe(func, *args, max_retries: int=2, **kwargs) -> Optional[pd.DataFrame]:
    import time
    for attempt in range(max_retries):
        try:
            time.sleep(0.3)
            result = func(*args, **kwargs)
            if result is not None and len(result) > 0:
                return result
        except Exception as e:
            logger.error(f'  pykrx safe call attempt {attempt + 1}: {e}', exc_info=True)
            time.sleep(1.5 * (attempt + 1))
    return None

def get_ss_etf_features(target_ticker: str, target_date: str=None) -> Dict[str, float]:
    """단일 Feature 계산 편의 함수."""
    return SSETFFeatureEngine().compute(target_ticker, target_date)

def merge_ss_etf_into_df(df_ml: pd.DataFrame, target_date: str=None, ticker_col: str='ticker') -> pd.DataFrame:
    """ML DataFrame에 SS-ETF Feature 병합 편의 함수."""
    return SSETFFeatureEngine().merge_into_ml_df(df_ml, target_date, ticker_col)