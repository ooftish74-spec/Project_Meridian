"""
KRX Investor Flow Collector — 외국인/기관 순매수 데이터
======================================================
임팩트: ★★★★★ — 한국시장 가격의 핵심 드라이버.

수집 소스: pykrx (KRX 공식 데이터)
  - 외국인 순매수 금액
  - 기관 순매수 금액
  - 개인 순매수 금액
  - 프로그램매매 순매수

피처 생성:
  - foreign_net_5d:    외국인 5일 누적 순매수
  - foreign_net_20d:   외국인 20일 누적 순매수
  - inst_net_5d:       기관 5일 누적 순매수
  - foreign_ratio:     외국인 순매수 / 거래대금 비율
  - flow_momentum:     외국인+기관 순매수 모멘텀 (5일 vs 20일)
  - supply_demand_score: 종합 수급 점수

Author: Project-A
Date: 2026-03-21
"""
import logging
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Dict, Optional, List
from datetime import datetime, timedelta
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

class InvestorFlowCollector:
    """KRX 투자자별 매매동향 수집 + 피처 생성."""

    def __init__(self):
        self.data_dir = _PROJECT_ROOT / 'data' / 'investor_flow'
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._pykrx = None

    @property
    def pykrx(self):
        if self._pykrx is None:
            try:
                from src.data_collection.pykrx_compat import stock as _pykrx_stock
                self._pykrx = _pykrx_stock
            except ImportError as e:
                logger.error('pykrx_compat not available', exc_info=True)
        return self._pykrx

    def collect_daily(self, ticker: str, date_str: Optional[str]=None, lookback_days: int=60) -> Optional[pd.DataFrame]:
        """
        특정 종목의 투자자별 매매동향 수집.

        Args:
            ticker: 종목코드 (e.g. '005930')
            date_str: 기준일 (YYYYMMDD), None=오늘
            lookback_days: 수집 기간

        Returns:
            DataFrame with columns: [foreign_net, inst_net, retail_net, volume]
        """
        if date_str is None:
            date_str = datetime.now().strftime('%Y%m%d')
        cache_path = self.data_dir / ticker / f'{date_str}.csv'
        if cache_path.exists():
            try:
                return pd.read_csv(cache_path, index_col=0, parse_dates=True)
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
        if self.pykrx is None:
            kis_result = self._load_from_kis_csv(ticker, lookback_days)
            if kis_result is not None:
                return kis_result
            return self._generate_proxy(ticker, lookback_days)
        try:
            end_date = datetime.strptime(date_str, '%Y%m%d')
            start_date = end_date - timedelta(days=lookback_days + 30)
            start_str = start_date.strftime('%Y%m%d')
            df = self.pykrx.client.get_investor_trading_range(start_str, date_str, ticker)
            if df is not None and (not df.empty):
                result = self._parse_investor_data(df)
                if result is not None and len(result) > 0:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    result.to_csv(cache_path)
                    return result
        except Exception as e:
            logger.error(f'pykrx 수급 수집 실패 ({ticker}): {e}', exc_info=True)
        return self._generate_proxy(ticker, lookback_days)

    def _parse_investor_data(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        """pykrx 결과 파싱."""
        try:
            col_map = {}
            for col in df.columns:
                col_lower = str(col).lower()
                if '외국인' in col_lower or 'foreign' in col_lower:
                    col_map[col] = 'foreign_net'
                elif '기관' in col_lower or 'instit' in col_lower:
                    col_map[col] = 'inst_net'
                elif '개인' in col_lower or 'retail' in col_lower:
                    col_map[col] = 'retail_net'
            if col_map:
                result = df[list(col_map.keys())].rename(columns=col_map)
                result.index = pd.to_datetime(result.index)
                return result
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
        return None

    def _load_from_kis_csv(self, ticker: str, lookback_days: int) -> Optional[pd.DataFrame]:
        """
        data/raw/stock_supply_demand/kis_investor_YYYYMMDD.csv 에서
        실제 외인/기관 수급 데이터를 로드합니다.

        kis_investor CSV 컬럼: date, close, prsn_ntby_qty, frgn_ntby_qty, orgn_ntby_qty

        Returns
        -------
        DataFrame with columns: [foreign_net, inst_net, retail_net]
        """
        supply_dir = _PROJECT_ROOT / 'data' / 'raw' / 'stock_supply_demand'
        ts_path = supply_dir / 'stock_sd_timeseries.csv'
        if ts_path.exists():
            try:
                ts = pd.read_csv(ts_path, index_col=0, parse_dates=True)
                if ticker in ts.columns or f'frgn_{ticker}' in ts.columns:
                    frgn_col = next((c for c in ts.columns if ticker in c and 'frgn' in c), None)
                    orgn_col = next((c for c in ts.columns if ticker in c and 'orgn' in c), None)
                    if frgn_col and orgn_col:
                        df = pd.DataFrame({'foreign_net': ts[frgn_col], 'inst_net': ts[orgn_col], 'retail_net': -(ts[frgn_col] + ts[orgn_col])}).tail(lookback_days + 30)
                        df = df.dropna()
                        if len(df) >= 20:
                            logger.debug(f'  ✅ KIS timeseries 수급 로드: {ticker} {len(df)}일')
                            return df
            except Exception as _e:
                logger.error(f'  timeseries 로드 실패: {_e}', exc_info=True)
        csv_files = sorted(supply_dir.glob('kis_investor_*.csv'), reverse=True)[:lookback_days + 30]
        if not csv_files:
            return None
        rows = []
        for f in csv_files:
            try:
                df_day = pd.read_csv(f)
                if 'ticker' in df_day.columns:
                    df_day = df_day[df_day['ticker'] == ticker]
                if df_day.empty:
                    continue
                row = df_day.iloc[0]
                date_str = f.stem.replace('kis_investor_', '')
                try:
                    idx = pd.to_datetime(date_str, format='%Y%m%d')
                except ValueError:
                    continue
                rows.append({'date': idx, 'foreign_net': float(row.get('frgn_ntby_qty', 0)), 'inst_net': float(row.get('orgn_ntby_qty', 0)), 'retail_net': float(row.get('prsn_ntby_qty', 0))})
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
                continue
        if not rows:
            return None
        result = pd.DataFrame(rows).set_index('date').sort_index()
        result = result.tail(lookback_days + 30)
        if len(result) < 5:
            return None
        logger.debug(f'  ✅ KIS CSV 수급 로드: {ticker} {len(result)}일 (실제 데이터)')
        return result

    def _generate_proxy(self, ticker: str, lookback_days: int) -> Optional[pd.DataFrame]:
        """
        pykrx 불가 시 가격 기반 프록시 생성.
        외국인 순매수 ≈ 가격 × 거래량 변화율 (근사치)

        ★ 근원 수정: historical_10y parquet를 1순위 소스로 활용
        """
        df = None
        try:
            parquet_path = _PROJECT_ROOT / 'data' / 'historical_10y' / f'kr_{ticker}.parquet'
            if parquet_path.exists():
                _df = pd.read_parquet(parquet_path)
                if len(_df) >= 20:
                    if 'close' in _df.columns and 'volume' in _df.columns:
                        _df = _df.rename(columns={'close': 'Close', 'volume': 'Volume', 'open': 'Open', 'high': 'High', 'low': 'Low'})
                        df = _df
            if df is None:
                csv_paths = sorted((_PROJECT_ROOT / 'data' / 'versions').glob(f'*/historical/korea_stocks/{ticker}.csv'), reverse=True)
                if not csv_paths:
                    csv_paths = [_PROJECT_ROOT / 'data' / 'raw' / 'korea_stocks' / f'{ticker}.csv']
                for p in csv_paths:
                    if p.exists():
                        df = pd.read_csv(p, index_col=0, parse_dates=True)
                        break
            if df is None:
                return None
            KR = {'종가': 'Close', '거래량': 'Volume', '시가': 'Open', '고가': 'High', '저가': 'Low'}
            df.rename(columns=KR, inplace=True)
            if 'Close' not in df.columns or 'Volume' not in df.columns:
                return None
            df = df.tail(lookback_days + 30)
            ret = df['Close'].pct_change()
            vol_chg = df['Volume'].pct_change()
            foreign_proxy = ret * vol_chg.abs() * df['Close'] * df['Volume'] / 1000000000.0
            inst_proxy = foreign_proxy * 0.7
            retail_proxy = -(foreign_proxy + inst_proxy)
            result = pd.DataFrame({'foreign_net': foreign_proxy, 'inst_net': inst_proxy, 'retail_net': retail_proxy}, index=df.index)
            return result.dropna()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
            return None

    def generate_features(self, ticker: str, lookback_days: int=60) -> Optional[pd.DataFrame]:
        """
        수급 기반 피처 생성 — ML 모델 입력용.

        피처:
          - foreign_net_5d:     외국인 5일 누적 순매수 (정규화)
          - foreign_net_20d:    외국인 20일 누적 순매수
          - inst_net_5d:        기관 5일 누적 순매수
          - inst_net_20d:       기관 20일 누적 순매수
          - flow_momentum:      (5일 순매수 - 20일 평균) / std
          - foreign_streak:     외국인 연속 순매수일 수
          - supply_demand_score: 종합 수급 점수 (-1 ~ +1)
        """
        flow = self.collect_daily(ticker, lookback_days=lookback_days)
        if flow is None or len(flow) < 20:
            return None
        feat = pd.DataFrame(index=flow.index)
        feat['foreign_net_5d'] = flow['foreign_net'].rolling(5).sum()
        feat['foreign_net_20d'] = flow['foreign_net'].rolling(20).sum()
        feat['inst_net_5d'] = flow['inst_net'].rolling(5).sum()
        feat['inst_net_20d'] = flow['inst_net'].rolling(20).sum()
        for col in ['foreign_net_5d', 'foreign_net_20d', 'inst_net_5d', 'inst_net_20d']:
            std = feat[col].rolling(20).std()
            mean = feat[col].rolling(20).mean()
            feat[col] = (feat[col] - mean) / (std + 1e-08)
        short_flow = (flow['foreign_net'] + flow['inst_net']).rolling(5).mean()
        long_flow = (flow['foreign_net'] + flow['inst_net']).rolling(20).mean()
        flow_std = (flow['foreign_net'] + flow['inst_net']).rolling(20).std()
        feat['flow_momentum'] = (short_flow - long_flow) / (flow_std + 1e-08)
        foreign_positive = (flow['foreign_net'] > 0).astype(int)
        streak = foreign_positive.copy()
        for i in range(1, len(streak)):
            if streak.iloc[i] == 1:
                streak.iloc[i] = streak.iloc[i - 1] + 1
            else:
                streak.iloc[i] = 0
        feat['foreign_streak'] = streak
        score = feat['foreign_net_5d'] * 0.3 + feat['inst_net_5d'] * 0.2 + feat['flow_momentum'] * 0.3 + (feat['foreign_streak'] / 10).clip(-1, 1) * 0.2
        feat['supply_demand_score'] = score.clip(-1, 1)
        return feat.dropna()

    def collect_batch_features(self, tickers: List[str]) -> Dict[str, pd.DataFrame]:
        """여러 종목 일괄 피처 생성."""
        results = {}
        for ticker in tickers:
            feat = self.generate_features(ticker)
            if feat is not None and len(feat) > 0:
                results[ticker] = feat
        return results