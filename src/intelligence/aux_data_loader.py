"""
Auxiliary Data Loader — 보조 데이터 통합 로더 (V6)
====================================================
학습 및 추론 시 ticker+date로 보조 피처를 조회합니다.

데이터 소스:
  1. Sentiment  — data/sentiment/{ticker}/daily_signal.csv
  2. DART       — data/dart/{ticker}/daily_signal.csv
  3. Flow       — data/investor_flow/{ticker}/daily_flow.csv + short_proxy.csv
  4. Earnings   — data/earnings/{ticker}/quarterly_earnings.csv
  5. Financials — data/financials_history/{ticker}.json

Usage:
    loader = AuxDataLoader()
    features = loader.get_features('005930', '2026-05-22')
    # → {'news_sentiment_mean': 0.55, 'foreign_net_buy_norm': 0.3, ...}
"""
import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional
import numpy as np
import pandas as pd
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / 'data'
AUX_FEATURE_NAMES = ['news_sentiment_mean', 'news_sentiment_std', 'news_count_norm', 'news_pos_ratio', 'dart_insider_signal', 'dart_buyback_signal', 'dart_composite', 'foreign_net_buy_norm', 'inst_net_buy_norm', 'foreign_ratio_feat', 'short_proxy_score', 'earnings_surprise_latest', 'revenue_yoy_latest', 'roe_2yr_avg', 'debt_ratio_latest']
_DEFAULTS = {f: 0.0 for f in AUX_FEATURE_NAMES}
_DEFAULTS['news_sentiment_mean'] = 0.5
_DEFAULTS['news_pos_ratio'] = 0.5
_DEFAULTS['foreign_ratio_feat'] = 0.0

class AuxDataLoader:
    """보조 데이터 통합 로더.

    메모리에 한 번 로드 후 O(1) 조회.
    데이터가 없는 ticker/date는 중립 기본값 반환.
    """

    def __init__(self, lazy: bool=False):
        """
        Args:
            lazy: True면 get_features 호출 시 로딩 (메모리 절약).
        """
        self._sentiment: Dict[str, pd.DataFrame] = {}
        self._dart: Dict[str, pd.DataFrame] = {}
        self._flow: Dict[str, pd.DataFrame] = {}
        self._short: Dict[str, pd.DataFrame] = {}
        self._earnings: Dict[str, pd.DataFrame] = {}
        self._financials: Dict[str, Dict] = {}
        self._loaded = False
        if not lazy:
            self._load_all()

    def _load_all(self):
        """모든 보조 데이터를 메모리에 로드."""
        if self._loaded:
            return
        logger.info('  📦 AuxDataLoader: 보조 데이터 로딩...')
        self._load_sentiment()
        self._load_dart()
        self._load_flow()
        self._load_earnings()
        self._load_financials()
        self._loaded = True
        logger.info(f'  ✅ AuxDataLoader: sentiment={len(self._sentiment)}, dart={len(self._dart)}, flow={len(self._flow)}, earnings={len(self._earnings)}, financials={len(self._financials)}')

    def get_features(self, ticker: str, date_str: str) -> Dict[str, float]:
        """ticker+date에 해당하는 15개 보조 피처 반환.

        Args:
            ticker: KRX 종목코드 (6자리)
            date_str: 날짜 ('YYYY-MM-DD')

        Returns:
            15개 피처 dict. 데이터 없으면 기본값.
        """
        if not self._loaded:
            self._load_all()
        feat = dict(_DEFAULTS)
        self._fill_sentiment(feat, ticker, date_str)
        self._fill_dart(feat, ticker, date_str)
        self._fill_flow(feat, ticker, date_str)
        self._fill_fundamentals(feat, ticker, date_str)
        return feat

    def _load_sentiment(self):
        """data/sentiment/{ticker}/daily_signal.csv 로드.

        Forward-fills sentiment values from the last day with actual news
        (news_count > 0) so that days without news carry the most recent
        real sentiment instead of returning zeros/defaults.
        Also precomputes a rolling 20-day average of news_count for dynamic
        normalization.
        """
        sent_dir = _DATA_DIR / 'sentiment'
        if not sent_dir.exists():
            return
        for ticker_dir in sent_dir.iterdir():
            if not ticker_dir.is_dir() or ticker_dir.name in ('macro', 'sectors'):
                continue
            signal_file = ticker_dir / 'daily_signal.csv'
            if not signal_file.exists():
                continue
            try:
                df = pd.read_csv(signal_file)
                if 'date' not in df.columns or len(df) == 0:
                    continue
                df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
                df = df.set_index('date')
                nc_col = 'news_count'
                if nc_col in df.columns:
                    has_news = df[nc_col].fillna(0).astype(float) > 0
                else:
                    has_news = pd.Series(False, index=df.index)
                ffill_cols = ['news_sentiment_mean', 'news_sentiment_std', 'news_pos_ratio']
                existing_ffill = [c for c in ffill_cols if c in df.columns]
                if has_news.any() and existing_ffill:
                    for col in existing_ffill:
                        df[col] = df[col].where(has_news).ffill()
                    defaults_map = {'news_sentiment_mean': 0.5, 'news_sentiment_std': 0.0, 'news_pos_ratio': 0.5}
                    for col in existing_ffill:
                        df[col] = df[col].fillna(defaults_map.get(col, 0.0))
                if nc_col in df.columns:
                    nc_series = df[nc_col].fillna(0).astype(float)
                    rolling_avg = nc_series.rolling(window=20, min_periods=1).mean()
                    news_only = nc_series[nc_series > 0]
                    if len(news_only) > 0:
                        ticker_avg = float(news_only.mean())
                    else:
                        ticker_avg = 1.0
                    df['_news_count_rolling_avg'] = rolling_avg
                    df['_news_count_ticker_avg'] = ticker_avg
                self._sentiment[ticker_dir.name] = df
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at aux_data_loader.py:208', exc_info=True)

    def _fill_sentiment(self, feat: Dict, ticker: str, date_str: str):
        """센티먼트 피처 채우기.

        Uses forward-filled sentiment values (from _load_sentiment) so
        days without news carry the last known real sentiment.
        Uses dynamic normalization for news_count_norm based on the
        ticker's rolling 20-day average news count.
        """
        df = self._sentiment.get(ticker)
        if df is None:
            return
        if date_str in df.index:
            row = df.loc[date_str]
        else:
            try:
                dt = pd.Timestamp(date_str)
                for delta in range(1, 8):
                    prev = (dt - pd.Timedelta(days=delta)).strftime('%Y-%m-%d')
                    if prev in df.index:
                        row = df.loc[prev]
                        break
                else:
                    return
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                return
        if isinstance(row, pd.DataFrame):
            row = row.iloc[-1]
        feat['news_sentiment_mean'] = float(row.get('news_sentiment_mean', 0.5))
        feat['news_sentiment_std'] = float(row.get('news_sentiment_std', 0.0))
        nc = float(row.get('news_count', 0))
        ticker_avg = float(row.get('_news_count_ticker_avg', 0))
        rolling_avg = float(row.get('_news_count_rolling_avg', 0))
        divisor = max(ticker_avg, rolling_avg, 1.0)
        feat['news_count_norm'] = float(np.clip(nc / divisor, 0.0, 1.0))
        pr = row.get('news_pos_ratio')
        if pr is not None and (not (isinstance(pr, float) and np.isnan(pr))):
            feat['news_pos_ratio'] = float(pr)

    def _load_dart(self):
        """data/dart/{ticker}/daily_signal.csv 로드.

        DART 공시는 이벤트형이므로 대부분의 날에 0입니다.
        Rolling 30일 최대값을 적용하여 공시 효과를 지속시키고,
        DD-11 auto-zero를 방지합니다.
        """
        dart_dir = _DATA_DIR / 'dart'
        if not dart_dir.exists():
            return
        signal_cols = ['dart_insider', 'dart_buyback', 'dart_composite']
        for ticker_dir in dart_dir.iterdir():
            if not ticker_dir.is_dir():
                continue
            signal_file = ticker_dir / 'daily_signal.csv'
            if not signal_file.exists():
                continue
            try:
                df = pd.read_csv(signal_file)
                if len(df) == 0:
                    continue
                date_col = df.columns[0]
                df = df.rename(columns={date_col: 'date'})
                df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
                df = df.set_index('date')
                for col in signal_cols:
                    if col in df.columns:
                        df[col] = df[col].fillna(0).rolling(window=30, min_periods=1).max()
                self._dart[ticker_dir.name] = df
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at aux_data_loader.py:304', exc_info=True)

    def _fill_dart(self, feat: Dict, ticker: str, date_str: str):
        """DART 피처 채우기 (확장 lookback 14일)."""
        df = self._dart.get(ticker)
        if df is None:
            return
        row = self._find_row(df, date_str, max_lookback=14)
        if row is None:
            return
        feat['dart_insider_signal'] = float(row.get('dart_insider', 0.0))
        feat['dart_buyback_signal'] = float(row.get('dart_buyback', 0.0))
        feat['dart_composite'] = float(row.get('dart_composite', 0.0))

    def _load_flow(self):
        """data/investor_flow/{ticker}/daily_flow.csv + short_proxy.csv 로드.

        Forward-fills normalized flow columns so days without raw data
        carry the last known values, preventing auto-zero in training.
        """
        flow_dir = _DATA_DIR / 'investor_flow'
        if not flow_dir.exists():
            return
        ffill_cols = ['foreign_net_buy_norm', 'inst_net_buy_norm', 'foreign_ratio_feat', 'short_proxy_score']
        for ticker_dir in flow_dir.iterdir():
            if not ticker_dir.is_dir():
                continue
            flow_file = ticker_dir / 'daily_flow.csv'
            if flow_file.exists():
                try:
                    df = pd.read_csv(flow_file)
                    if len(df) > 0 and 'date' in df.columns:
                        df['date'] = pd.to_datetime(df['date'].astype(str), format='mixed').dt.strftime('%Y-%m-%d')
                        df = df.set_index('date')
                        for col in ffill_cols:
                            if col in df.columns:
                                valid = df[col].replace(0.0, np.nan)
                                filled = valid.ffill()
                                df[col] = filled.fillna(0.0)
                        self._flow[ticker_dir.name] = df
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.warning('[SILENT_BYPASS] Suppressed exception at aux_data_loader.py:364', exc_info=True)
            short_file = ticker_dir / 'short_proxy.csv'
            if short_file.exists():
                try:
                    df = pd.read_csv(short_file)
                    if len(df) > 0 and 'date' in df.columns:
                        df['date'] = pd.to_datetime(df['date'].astype(str), format='mixed').dt.strftime('%Y-%m-%d')
                        df = df.set_index('date')
                        if 'short_proxy_score' in df.columns:
                            valid = df['short_proxy_score'].replace(0.0, np.nan)
                            df['short_proxy_score'] = valid.ffill().fillna(0.0)
                        self._short[ticker_dir.name] = df
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.warning('[SILENT_BYPASS] Suppressed exception at aux_data_loader.py:384', exc_info=True)

    def _fill_flow(self, feat: Dict, ticker: str, date_str: str):
        """수급 피처 채우기. Forward-fill + NaN 방어."""
        df = self._flow.get(ticker)
        if df is not None:
            row = self._find_row(df, date_str, max_lookback=14)
            if row is not None:
                for col, key in [('foreign_net_buy_norm', 'foreign_net_buy_norm'), ('inst_net_buy_norm', 'inst_net_buy_norm'), ('foreign_ratio_feat', 'foreign_ratio_feat')]:
                    val = row.get(col)
                    if val is not None and (not (isinstance(val, float) and np.isnan(val))):
                        feat[key] = float(val)
                    else:
                        vol = row.get('volume', 1)
                        vol = float(vol) if not (isinstance(vol, float) and np.isnan(vol)) else 1.0
                        vol = max(vol, 1.0)
                        if col == 'foreign_net_buy_norm':
                            raw = row.get('foreign_net_buy', 0)
                            raw = float(raw) if not (isinstance(raw, float) and np.isnan(raw)) else 0.0
                            feat[key] = float(np.clip(raw / vol, -1.0, 1.0))
                        elif col == 'inst_net_buy_norm':
                            raw = row.get('inst_net_buy', 0)
                            raw = float(raw) if not (isinstance(raw, float) and np.isnan(raw)) else 0.0
                            feat[key] = float(np.clip(raw / vol, -1.0, 1.0))
                        elif col == 'foreign_ratio_feat':
                            fr = row.get('foreign_ratio')
                            if fr is not None and (not (isinstance(fr, float) and np.isnan(fr))):
                                feat[key] = (float(fr) - 50.0) / 100.0
                sp = row.get('short_proxy_score')
                if sp is not None and (not (isinstance(sp, float) and np.isnan(sp))):
                    feat['short_proxy_score'] = float(sp)
        sdf = self._short.get(ticker)
        if sdf is not None:
            row = self._find_row(sdf, date_str, max_lookback=14)
            if row is not None:
                sp = row.get('short_proxy_score', 0.0)
                sp = float(sp) if not (isinstance(sp, float) and np.isnan(sp)) else 0.0
                feat['short_proxy_score'] = sp

    def _load_earnings(self):
        """data/earnings/{ticker}/quarterly_earnings.csv 로드."""
        earn_dir = _DATA_DIR / 'earnings'
        if not earn_dir.exists():
            return
        for ticker_dir in earn_dir.iterdir():
            if not ticker_dir.is_dir():
                continue
            qe_file = ticker_dir / 'quarterly_earnings.csv'
            if not qe_file.exists():
                continue
            try:
                df = pd.read_csv(qe_file)
                if len(df) > 0:
                    self._earnings[ticker_dir.name] = df
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at aux_data_loader.py:454', exc_info=True)

    def _load_financials(self):
        """data/financials_history/{ticker}.json 로드."""
        fh_dir = _DATA_DIR / 'financials_history'
        if not fh_dir.exists():
            return
        for fh_file in fh_dir.glob('*.json'):
            ticker = fh_file.stem
            try:
                data = json.loads(fh_file.read_text())
                self._financials[ticker] = data
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at aux_data_loader.py:470', exc_info=True)

    def _fill_fundamentals(self, feat: Dict, ticker: str, date_str: str):
        """재무/실적 피처 채우기.

        Dynamically computes:
        - earnings_surprise = (latest_q_income - prev_q_income) / |prev_q_income|
        - revenue_yoy = (latest_q_revenue - same_q_prev_year_revenue) / |same_q_prev_year_revenue|
        - roe_2yr_avg = average ROE over last 2 years from annual data
        - debt_ratio_latest from annual data

        Falls back to pre-computed values when raw data is insufficient.
        """
        edf = self._earnings.get(ticker)
        if edf is not None and len(edf) > 0:
            try:
                dt_year = int(date_str[:4])
                dt_month = int(date_str[5:7])
                quarter_months = {'Q1': 5, 'Q2': 8, 'Q3': 11, 'Q4': 2}
                valid = []
                for _, row in edf.iterrows():
                    yr = int(row.get('year', 0))
                    q = str(row.get('quarter', ''))
                    pub_month = quarter_months.get(q, 0)
                    pub_year = yr if q != 'Q4' else yr + 1
                    if pub_year < dt_year or (pub_year == dt_year and pub_month <= dt_month):
                        valid.append(row)
                if valid:
                    latest = valid[-1]
                    income_col = 'single_q_income' if 'single_q_income' in latest.index else 'operating_income'
                    latest_income = float(latest.get(income_col, 0) or 0)
                    if len(valid) >= 2:
                        prev_q = valid[-2]
                        prev_income = float(prev_q.get(income_col, 0) or 0)
                        if abs(prev_income) > 0:
                            es = (latest_income - prev_income) / abs(prev_income)
                        else:
                            es = 0.0
                    else:
                        es = float(latest.get('earnings_surprise', 0) or 0)
                        logger.debug(f'earnings_surprise: insufficient quarters for {ticker}, using pre-computed={es:.4f}')
                    if not (isinstance(es, float) and np.isnan(es)):
                        feat['earnings_surprise_latest'] = float(np.clip(es, -3.0, 3.0))
                    rev_col = 'single_q_revenue' if 'single_q_revenue' in latest.index else 'revenue'
                    latest_rev = float(latest.get(rev_col, 0) or 0)
                    latest_q = str(latest.get('quarter', ''))
                    latest_yr = int(latest.get('year', 0))
                    prev_year_row = None
                    for row in valid:
                        if int(row.get('year', 0)) == latest_yr - 1 and str(row.get('quarter', '')) == latest_q:
                            prev_year_row = row
                            break
                    if prev_year_row is not None:
                        prev_yr_rev = float(prev_year_row.get(rev_col, 0) or 0)
                        if abs(prev_yr_rev) > 0:
                            ry = (latest_rev - prev_yr_rev) / abs(prev_yr_rev)
                        else:
                            ry = 0.0
                    else:
                        ry = float(latest.get('revenue_yoy', latest.get('earnings_yoy', 0)) or 0)
                        if abs(ry) > 2.0:
                            ry = ry / 100.0
                        logger.debug(f'revenue_yoy: no same-quarter prev year for {ticker}, using fallback={ry:.4f}')
                    if not (isinstance(ry, float) and np.isnan(ry)):
                        feat['revenue_yoy_latest'] = float(np.clip(ry, -2.0, 2.0))
            except Exception as e:
                logger.error(f'earnings feature computation error ({ticker}): {e}', exc_info=True)
        fh = self._financials.get(ticker)
        if fh is not None:
            try:
                annual = fh.get('annual', [])
                computed = fh.get('computed', {})
                if annual and len(annual) >= 1:
                    roe_values = []
                    for entry in annual[-2:]:
                        roe_val = entry.get('roe')
                        if roe_val is not None and (not (isinstance(roe_val, float) and np.isnan(roe_val))):
                            roe_values.append(float(roe_val))
                    if roe_values:
                        roe_avg = sum(roe_values) / len(roe_values)
                        feat['roe_2yr_avg'] = float(np.clip(roe_avg / 100.0, -1.0, 1.0))
                    else:
                        roe = computed.get('roe_2yr_avg')
                        if roe is not None and roe != 0.0 and (not (isinstance(roe, float) and np.isnan(roe))):
                            feat['roe_2yr_avg'] = float(np.clip(float(roe) / 100.0, -1.0, 1.0))
                else:
                    roe = computed.get('roe_2yr_avg')
                    if roe is not None and roe != 0.0 and (not (isinstance(roe, float) and np.isnan(roe))):
                        feat['roe_2yr_avg'] = float(np.clip(float(roe) / 100.0, -1.0, 1.0))
                if annual and len(annual) >= 1:
                    dr_val = annual[-1].get('debt_ratio')
                    if dr_val is not None and (not (isinstance(dr_val, float) and np.isnan(dr_val))):
                        feat['debt_ratio_latest'] = float(np.clip(float(dr_val) / 500.0, 0.0, 1.0))
                    else:
                        dr = computed.get('debt_ratio_latest')
                        if dr is not None and (not (isinstance(dr, float) and np.isnan(dr))):
                            feat['debt_ratio_latest'] = float(np.clip(float(dr) / 500.0, 0.0, 1.0))
                else:
                    dr = computed.get('debt_ratio_latest')
                    if dr is not None and (not (isinstance(dr, float) and np.isnan(dr))):
                        feat['debt_ratio_latest'] = float(np.clip(float(dr) / 500.0, 0.0, 1.0))
            except Exception as e:
                logger.error(f'financials feature computation error ({ticker}): {e}', exc_info=True)

    @staticmethod
    def _find_row(df: pd.DataFrame, date_str: str, max_lookback: int=3):
        """DataFrame에서 date_str에 해당하는 행 탐색.

        정확 매칭 → 없으면 최대 max_lookback일 전 탐색.
        """
        if date_str in df.index:
            row = df.loc[date_str]
            return row.iloc[-1] if isinstance(row, pd.DataFrame) else row
        try:
            dt = pd.Timestamp(date_str)
            for delta in range(1, max_lookback + 1):
                prev = (dt - pd.Timedelta(days=delta)).strftime('%Y-%m-%d')
                if prev in df.index:
                    row = df.loc[prev]
                    return row.iloc[-1] if isinstance(row, pd.DataFrame) else row
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            logger.warning('[SILENT_BYPASS] Suppressed exception at aux_data_loader.py:685', exc_info=True)
        return None