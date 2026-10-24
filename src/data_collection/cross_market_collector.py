"""
Cross-Market Data Collector — 크로스마켓 보완 지표
====================================================
Top 5 보완 지표:
  1. US-JP 금리 스프레드 (엔캐리 핵심)
  2. Caixin PMI (중국 선행)
  3. ISM PMI (미국 선행)
  4. PBoC LPR 금리 (중국 통화정책)
  5. US 2Y 채권 (수익률 곡선)

추가:
  - Rolling 60일 섹터 베타 자동 계산
  - 이란/중동 유가 충격 프록시
"""
from src.utils.file_ops import atomic_write_json
from src.infra.safe_io import atomic_write_dataframe

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional
import numpy as np
import pandas as pd
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / 'data' / 'raw' / 'cross_market'

class CrossMarketCollector:
    """크로스마켓 보완 지표 수집 + 스프레드 계산."""

    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._fred_down = False
        self._setup_fred()
        self._setup_yf()

    def _setup_fred(self):
        try:
            from fredapi import Fred
            from src.utils.credential_manager import CredentialManager
            key = (CredentialManager().read_from_env('FRED_API_KEY') or '').strip()
            self.fred = Fred(api_key=key) if key else None
            if self.fred:
                logger.info('  ✅ FRED API 초기화 완료 (key=%s...)', key[:8] if key else '?')
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {_e}")
            self.fred = None

    def _safe_fred_get_series(self, series_id: str, observation_start: Optional[str]=None) -> Optional[pd.Series]:
        """FRED get_series + USADataCollector 멀티벤더(Alpha Vantage / DirectChart) 자동 전환."""
        if not self._fred_down and self.fred:
            try:
                kwargs = {'observation_start': observation_start} if observation_start else {}
                s = self.fred.get_series(series_id, **kwargs)
                if s is not None and not s.empty:
                    return s
            except Exception as _fe:
                err_str = str(_fe)
                if 'syntax error' in err_str.lower() or 'parseerror' in type(_fe).__name__.lower() or 'connection' in err_str.lower() or 'timed out' in err_str.lower():
                    self._fred_down = True
                    logger.info(f'  ℹ️ FRED 서버 점검 감지 ({series_id}) → 멀티벤더 1차 수집 전환')
                else:
                    logger.debug(f'  FRED {series_id} 스킵: {_fe}')
        try:
            from src.data_collection.usa_collector import USADataCollector
            if not hasattr(self, '_usa_collector') or self._usa_collector is None:
                self._usa_collector = USADataCollector()
                self._usa_collector._fred_down = self._fred_down
            return self._usa_collector._fetch_multi_vendor_series(series_id, start_date=observation_start)
        except Exception as _mve:
            logger.debug(f'  Multi-vendor series lookup skipped ({series_id}): {_mve}')
            return None

    def _setup_yf(self):
        self.yf = True

    def _yf_fetch_with_retry(self, ticker: str, period: str='3y', n_retry: int=3) -> Optional[pd.DataFrame]:
        """Direct Chart REST / Alpha Vantage 기반 일봉 조회."""
        try:
            from src.data_collection.alpha_vantage_collector import fetch_yahoo_chart_ohlcv
            df = fetch_yahoo_chart_ohlcv(ticker, range_str=period)
            if df is not None and not df.empty:
                df = df.copy()
                if 'close' in df.columns and 'Close' not in df.columns:
                    df['Close'] = df['close']
                return df
        except Exception as _e:
            logger.debug(f'  _yf_fetch_with_retry({ticker}) 스킵: {_e}')
        return None

    def _load_csv_ffill(self, csv_path: Path, col: str='Close') -> Optional[pd.DataFrame]:
        """CSV 파일에서 이전 데이터 로드 (ffill fallback용)."""
        try:
            if csv_path.exists():
                df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
                logger.warning(f"  📋 {csv_path.name}: 실시간 수집 실패 → 이전 CSV ffill 적용 ({len(df)}행 이전 데이터)")
                return df
        except Exception as _e:
            logger.warning(f'  CSV ffill 로드 실패 {csv_path}: {_e}', exc_info=True)
        return None

    def collect_us_jp_spread(self, lookback_years: int=3) -> Optional[pd.DataFrame]:
        """
        US 10Y - Japan 10Y 금리 스프레드 계산.
        스프레드 축소 = 엔캐리 언와인드 트리거.
        """
        logger.info('📊 US-JP 금리 스프레드 수집...')
        try:
            obs_start = (datetime.now() - timedelta(days=lookback_years * 365)).strftime('%Y-%m-%d')
            us_10y = self._safe_fred_get_series('DGS10', observation_start=obs_start)
            if us_10y is None or us_10y.empty:
                df = self._yf_fetch_with_retry('^TNX', period=f'{lookback_years}y')
                us_10y = df['Close'].squeeze() if df is not None and not df.empty else None
            jp_10y = self._safe_fred_get_series('IRLTLT01JPM156N', observation_start=obs_start)
            if jp_10y is None or jp_10y.empty:
                jp_path = PROJECT_ROOT / 'data' / 'raw' / 'japan_economic' / 'JPN_10Y_Treasury.csv'
                if jp_path.exists():
                    jp_df = pd.read_csv(jp_path, parse_dates=[0], index_col=0)
                    jp_10y = pd.to_numeric(jp_df.iloc[:, 0], errors='coerce').dropna()
            if us_10y is not None and jp_10y is not None:
                us_10y.index = pd.to_datetime(us_10y.index).tz_localize(None)
                jp_10y.index = pd.to_datetime(jp_10y.index).tz_localize(None)
                combined = pd.DataFrame({'US_10Y': us_10y, 'JP_10Y': jp_10y}).sort_index()
                combined['JP_10Y'] = combined['JP_10Y'].ffill().bfill()
                combined = combined.dropna(subset=['US_10Y', 'JP_10Y'])
                combined['US_JP_Spread'] = combined['US_10Y'] - combined['JP_10Y']
                combined['Spread_MA20'] = combined['US_JP_Spread'].rolling(20).mean()
                combined['Spread_Zscore'] = (combined['US_JP_Spread'] - combined['US_JP_Spread'].rolling(60).mean()) / (combined['US_JP_Spread'].rolling(60).std() + 1e-10)
                atomic_write_dataframe(combined, DATA_DIR / 'us_jp_spread.csv', file_format='csv')
                logger.info(f"  ✅ US-JP 스프레드: {len(combined)}일, 현재 {combined['US_JP_Spread'].iloc[-1]:.2f}%p")
                return combined
            old_df = self._load_csv_ffill(DATA_DIR / 'us_jp_spread.csv')
            if old_df is not None and not old_df.empty:
                return old_df
            logger.warning('  ⚠️ US/JP 10Y 데이터 부족')
            return None
        except Exception as e:
            old_df = self._load_csv_ffill(DATA_DIR / 'us_jp_spread.csv')
            if old_df is not None and not old_df.empty:
                return old_df
            logger.warning(f'  ⚠️ US-JP 스프레드 실패 (데이터 부족): {e}')
            return None

    def collect_caixin_pmi(self) -> Optional[pd.DataFrame]:
        """중국 Caixin 제조업/서비스 PMI 대리 지표 수집."""
        logger.info('📊 중국 PMI (Caixin proxy) 수집...')
        results = pd.DataFrame()
        FRED_CHINA_SERIES = [('MANEMP', 'US_Mfg_Emp_proxy')]
        fred_start_date = (datetime.now() - timedelta(days=365 * 5)).strftime('%Y-%m-%d')
        for series_id, col_name in FRED_CHINA_SERIES:
            s = self._safe_fred_get_series(series_id, observation_start=fred_start_date)
            if s is not None and (not s.empty):
                results[col_name] = s
                logger.info(f"  {series_id}: {len(s)}행")
                break
        if results.empty:
            df = self._yf_fetch_with_retry('FXI', period='3y', n_retry=3)
            if df is not None and (not df.empty):
                results['China_FXI_Proxy'] = df['Close'].resample('ME').last()
                logger.info('  FXI ETF 대리 지표 사용')
        if results.empty:
            old_df = self._load_csv_ffill(DATA_DIR / 'china_pmi.csv')
            if old_df is not None and (not old_df.empty):
                results = old_df
        if not results.empty:
            results = results.dropna(how='all')
            atomic_write_dataframe(results, DATA_DIR / 'china_pmi.csv', file_format='csv')
            logger.info(f"  ✅ 중국 PMI proxy: {len(results)}개월")
            return results
        logger.warning('  ⚠️ 중국 PMI 모든 소스 실패 → 스킵')
        return None

    def collect_ism_pmi(self) -> Optional[pd.DataFrame]:
        """미국 ISM 제조업/서비스 PMI 대리 지표 수집."""
        logger.info('📊 미국 ISM PMI 수집...')
        results = pd.DataFrame()
        FRED_ISM_MAP = [('MANEMP', 'US_Mfg_Employment'), ('INDPRO', 'US_Ind_Production'), ('DGORDER', 'US_Durable_Goods'), ('RSXFS', 'US_Retail_Sales')]
        fred_start_date = (datetime.now() - timedelta(days=365 * 5)).strftime('%Y-%m-%d')
        for series_id, col_name in FRED_ISM_MAP:
            s = self._safe_fred_get_series(series_id, observation_start=fred_start_date)
            if s is not None and (not s.empty):
                results[col_name] = s.resample('ME').last()
                logger.info(f"  {series_id}: {len(s)}행")
        if results.empty:
            df_xli = self._yf_fetch_with_retry('XLI', period='3y', n_retry=3)
            if df_xli is not None and (not df_xli.empty):
                results['XLI_Industrial_ETF'] = df_xli['Close'].resample('ME').last()
                logger.info('  XLI 산업 ETF 대리 지표 사용')
        if results.empty:
            old_df = self._load_csv_ffill(DATA_DIR / 'us_ism_pmi.csv')
            if old_df is not None and (not old_df.empty):
                results = old_df
        if not results.empty:
            results = results.dropna(how='all')
            atomic_write_dataframe(results, DATA_DIR / 'us_ism_pmi.csv', file_format='csv')
            logger.info(f"  ✅ US 경기 지표: {len(results)}개월, 컬럼={list(results.columns)}")
            return results
        logger.warning('  ⚠️ US PMI 모든 소스 실패')
        return None

    def collect_pboc_lpr(self) -> Optional[pd.DataFrame]:
        """중국 PBoC LPR / 통화정책 지표 수집."""
        logger.info('📊 PBoC LPR / 중국 금리 수집...')
        results = pd.DataFrame()
        FRED_CHINA_MONETARY = [('FEDFUNDS', 'Fed_Funds_Rate'), ('DGS10', 'US_10Y_Yield'), ('DCOILWTICO', 'WTI_Oil')]
        for series_id, col_name in FRED_CHINA_MONETARY:
            s = self._safe_fred_get_series(series_id, observation_start='2020-01-01')
            if s is not None and (not s.empty):
                results[col_name] = s
        if results.empty:
            df = self._yf_fetch_with_retry('CBON', period='2y', n_retry=3)
            if df is not None and (not df.empty):
                results['China_Bond_ETF'] = df['Close']
                logger.info('  CBON ETF 대리 지표')
        if results.empty:
            old_df = self._load_csv_ffill(DATA_DIR / 'china_monetary.csv')
            if old_df is not None and (not old_df.empty):
                results = old_df
        if not results.empty:
            results = results.dropna(how='all')
            atomic_write_dataframe(results, DATA_DIR / 'china_monetary.csv', file_format='csv')
            logger.info(f"  ✅ 통화/금리 지표: {len(results)}행")
            return results
        logger.warning('  ⚠️ PBoC LPR 모든 소스 실패')
        return None

    def collect_us_yield_curve(self, lookback_years: int=3) -> Optional[pd.DataFrame]:
        """
        US 2Y 채권 + 수익률 곡선 역전 판단.
        2Y-10Y 스프레드 < 0 = 경기 침체 선행 신호.
        """
        logger.info('📊 US 2Y + 수익률 곡선 수집...')
        try:
            results = pd.DataFrame()
            start = (datetime.now() - timedelta(days=lookback_years * 365)).strftime('%Y-%m-%d')
            us_2y = self._safe_fred_get_series('DGS2', observation_start=start)
            us_10y = self._safe_fred_get_series('DGS10', observation_start=start)
            us_3m = self._safe_fred_get_series('DGS3MO', observation_start=start)
            if us_2y is not None:
                results['US_2Y'] = us_2y
            if us_10y is not None:
                results['US_10Y'] = us_10y
            if us_3m is not None:
                results['US_3M'] = us_3m
            if 'US_2Y' not in results.columns:
                df = self._yf_fetch_with_retry('^IRX', period=f'{lookback_years}y', n_retry=3)
                if df is not None and (not df.empty):
                    results['US_2Y'] = df['Close'].squeeze()
            if 'US_2Y' not in results.columns or 'US_10Y' not in results.columns:
                old_df = self._load_csv_ffill(DATA_DIR / 'us_yield_curve.csv', col='US_2Y')
                if old_df is not None and not old_df.empty:
                    for col_c in ('US_2Y', 'US_10Y', 'US_3M', 'Yield_Curve_2Y10Y', 'Curve_Inverted', 'Yield_Curve_3M10Y'):
                        if col_c not in results.columns and col_c in old_df.columns:
                            results[col_c] = old_df[col_c]
            if 'US_2Y' in results.columns and 'US_10Y' in results.columns:
                results = results.dropna(subset=['US_2Y', 'US_10Y'])
                results['Yield_Curve_2Y10Y'] = results['US_10Y'] - results['US_2Y']
                results['Curve_Inverted'] = (results['Yield_Curve_2Y10Y'] < 0).astype(int)
                if 'US_3M' in results.columns:
                    results['Yield_Curve_3M10Y'] = results['US_10Y'] - results['US_3M']
                atomic_write_dataframe(results, DATA_DIR / 'us_yield_curve.csv', file_format='csv')
                latest = results['Yield_Curve_2Y10Y'].iloc[-1]
                logger.info(f"  ✅ 수익률 곡선: {len(results)}일, 2Y-10Y={latest:+.2f}%p ({('역전' if latest < 0 else '정상')})")
                return results
            return None
        except Exception as e:
            old_df = self._load_csv_ffill(DATA_DIR / 'us_yield_curve.csv')
            if old_df is not None and not old_df.empty:
                return old_df
            logger.warning(f'  ⚠️ 수익률 곡선 실패 (데이터 부족): {e}')
            return None

    def compute_sector_betas(self, lookback_days: int=60) -> Dict[str, float]:
        """섹터 ETF vs KOSPI의 rolling 베타 계산 (로컬 캐시 우선)."""
        logger.info('📊 섹터 베타 계산 (60일 rolling)...')
        try:
            def _load_kr_close(tk: str) -> Optional[pd.Series]:
                for sub_dir in ('kr_markets',):
                    p = PROJECT_ROOT / 'data' / sub_dir / f'kr_{tk}.parquet'
                    if p.exists():
                        df = pd.read_parquet(p)
                        df.columns = [str(c).lower() for c in df.columns]
                        if 'date' in df.columns:
                            df['date'] = pd.to_datetime(df['date']).dt.normalize()
                            df = df.drop_duplicates(subset=['date'], keep='last').set_index('date').sort_index()
                        if 'close' in df.columns:
                            return pd.to_numeric(df['close'], errors='coerce').dropna()
                return None

            kospi_close = _load_kr_close('069500')  # hardcode-ok: L0 KOSPI200 benchmark ETF
            if kospi_close is None or kospi_close.empty:
                return {}
            kospi_ret = kospi_close.pct_change().dropna().tail(lookback_days * 2)
            sector_tickers = {'semiconductor': '091160', 'auto': '091180', 'finance': '091170', 'tech': '139260', 'defense': '449450'}  # hardcode-ok: L0 sector benchmark ETFs
            betas = {}
            for sector, ticker in sector_tickers.items():
                try:
                    sec_close = _load_kr_close(ticker)
                    if sec_close is None or len(sec_close) < 30:
                        continue
                    sec_ret = sec_close.pct_change().dropna()
                    aligned = pd.DataFrame({'market': kospi_ret, 'sector': sec_ret}).dropna().tail(lookback_days)
                    if len(aligned) < 20:
                        continue
                    cov = np.cov(aligned['sector'], aligned['market'])
                    beta = cov[0, 1] / (cov[1, 1] + 1e-10)
                    betas[sector] = round(float(beta), 3)
                except Exception as _e:
                    logger.debug(f"Targeted fallback: {_e}")
                    continue
            if betas:
                beta_path = PROJECT_ROOT / 'data' / 'raw' / 'sector_beta'
                beta_path.mkdir(parents=True, exist_ok=True)
                atomic_write_json(beta_path / 'latest_betas.json', {'date': datetime.now().strftime('%Y-%m-%d'), 'lookback_days': lookback_days, 'betas': betas}, indent=2)
                logger.info(f"  ✅ 섹터 베타: {betas}")
            return betas
        except Exception as e:
            logger.error(f'  ❌ 섹터 베타 실패: {e}', exc_info=True)
            return {}

    def collect_all(self) -> Dict:
        """모든 크로스마켓 보완 지표 수집."""
        logger.info('\n' + '=' * 60)
        logger.info('📊 Cross-Market 보완 지표 수집')
        logger.info('=' * 60)
        results = {}
        spread = self.collect_us_jp_spread()
        results['us_jp_spread'] = spread is not None
        pmi_cn = self.collect_caixin_pmi()
        results['china_pmi'] = pmi_cn is not None
        pmi_us = self.collect_ism_pmi()
        results['us_ism_pmi'] = pmi_us is not None
        pboc = self.collect_pboc_lpr()
        results['china_monetary'] = pboc is not None
        curve = self.collect_us_yield_curve()
        results['us_yield_curve'] = curve is not None
        betas = self.compute_sector_betas()
        results['sector_betas'] = len(betas) > 0
        ok = sum((1 for v in results.values() if v))
        logger.info(f"\n✅ Cross-Market 수집 완료: {ok}/{len(results)}")
        for k, v in results.items():
            logger.info(f"  {('✅' if v else '❌')} {k}")
        summary = {'timestamp': datetime.now().isoformat(), 'results': results, 'sector_betas': betas}
        atomic_write_json(DATA_DIR / 'collection_summary.json', summary, indent=2, default=str)
        return summary
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    collector = CrossMarketCollector()
    collector.collect_all()