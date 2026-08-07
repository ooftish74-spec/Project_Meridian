"""
US Stock Data Collector — yfinance 기반 미국 주식 일봉 + 재무 수집
=================================================================

수집 대상: S&P500 + 주요 레버리지 ETF
수집 항목:
  - 일봉 OHLCV (6개월)
  - 기본 재무 지표 (PE, PB, Market Cap, Revenue Growth, EPS)
  - 기술적 지표 (RSI, SMA20/50/200, ATR, MACD, BB)

출력:
  data/us_stocks/prices/{ticker}.csv     — 일봉 데이터
  data/us_stocks/features/{date}.json    — 일별 피처 벡터 (ML용)
  results/prefetch_atr_cache.json        — ATR 캐시 (L1 TP/SL용)

Author: Project-A
Date: 2026-05-14
"""
from src.utils.file_ops import atomic_write_json

import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
import numpy as np
import pandas as pd
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
logger = logging.getLogger(__name__)
US_L2_UNIVERSE = {'AAPL': {'sector': 'Technology', 'exchange': 'NASD'}, 'MSFT': {'sector': 'Technology', 'exchange': 'NASD'}, 'GOOGL': {'sector': 'Technology', 'exchange': 'NASD'}, 'AMZN': {'sector': 'Technology', 'exchange': 'NASD'}, 'NVDA': {'sector': 'Technology', 'exchange': 'NASD'}, 'META': {'sector': 'Technology', 'exchange': 'NASD'}, 'TSLA': {'sector': 'Consumer Cyclical', 'exchange': 'NASD'}, 'AVGO': {'sector': 'Technology', 'exchange': 'NASD'}, 'AMD': {'sector': 'Technology', 'exchange': 'NASD'}, 'CRM': {'sector': 'Technology', 'exchange': 'NASD'}, 'ORCL': {'sector': 'Technology', 'exchange': 'NASD'}, 'ADBE': {'sector': 'Technology', 'exchange': 'NASD'}, 'NFLX': {'sector': 'Communication', 'exchange': 'NASD'}, 'INTC': {'sector': 'Technology', 'exchange': 'NASD'}, 'QCOM': {'sector': 'Technology', 'exchange': 'NASD'}, 'AMAT': {'sector': 'Technology', 'exchange': 'NASD'}, 'MU': {'sector': 'Technology', 'exchange': 'NASD'}, 'LRCX': {'sector': 'Technology', 'exchange': 'NASD'}, 'KLAC': {'sector': 'Technology', 'exchange': 'NASD'}, 'MRVL': {'sector': 'Technology', 'exchange': 'NASD'}, 'JPM': {'sector': 'Financial', 'exchange': 'NYSE'}, 'V': {'sector': 'Financial', 'exchange': 'NYSE'}, 'MA': {'sector': 'Financial', 'exchange': 'NYSE'}, 'BAC': {'sector': 'Financial', 'exchange': 'NYSE'}, 'GS': {'sector': 'Financial', 'exchange': 'NYSE'}, 'UNH': {'sector': 'Healthcare', 'exchange': 'NYSE'}, 'JNJ': {'sector': 'Healthcare', 'exchange': 'NYSE'}, 'LLY': {'sector': 'Healthcare', 'exchange': 'NYSE'}, 'ABBV': {'sector': 'Healthcare', 'exchange': 'NYSE'}, 'MRK': {'sector': 'Healthcare', 'exchange': 'NYSE'}, 'PG': {'sector': 'Consumer Staples', 'exchange': 'NYSE'}, 'KO': {'sector': 'Consumer Staples', 'exchange': 'NYSE'}, 'COST': {'sector': 'Consumer Staples', 'exchange': 'NASD'}, 'WMT': {'sector': 'Consumer Staples', 'exchange': 'NYSE'}, 'HD': {'sector': 'Consumer Cyclical', 'exchange': 'NYSE'}, 'MCD': {'sector': 'Consumer Cyclical', 'exchange': 'NYSE'}, 'CAT': {'sector': 'Industrials', 'exchange': 'NYSE'}, 'DE': {'sector': 'Industrials', 'exchange': 'NYSE'}, 'BA': {'sector': 'Industrials', 'exchange': 'NYSE'}, 'XOM': {'sector': 'Energy', 'exchange': 'NYSE'}, 'CVX': {'sector': 'Energy', 'exchange': 'NYSE'}, 'SPY': {'sector': 'ETF', 'exchange': 'AMEX'}, 'QQQ': {'sector': 'ETF', 'exchange': 'NASD'}, 'TQQQ': {'sector': 'ETF', 'exchange': 'NASD'}, 'SQQQ': {'sector': 'ETF', 'exchange': 'NASD'}, 'SOXL': {'sector': 'ETF', 'exchange': 'NASD'}, 'SOXS': {'sector': 'ETF', 'exchange': 'NASD'}, 'TLT': {'sector': 'ETF', 'exchange': 'NASD'}, 'GLD': {'sector': 'ETF', 'exchange': 'AMEX'}, '^VIX': {'sector': 'Macro', 'exchange': 'INDEX'}, 'KRW=X': {'sector': 'Macro', 'exchange': 'FOREX'}, '^TNX': {'sector': 'Macro', 'exchange': 'INDEX'}, 'CL=F': {'sector': 'Macro', 'exchange': 'COMMODITY'}, 'GC=F': {'sector': 'Macro', 'exchange': 'COMMODITY'}, 'DX-Y.NYB': {'sector': 'Macro', 'exchange': 'INDEX'}}

class USStockCollector:
    """미국 주식 일봉 + 피처 수집기."""

    def __init__(self, lookback_days: int=180):
        self.lookback_days = lookback_days
        self.prices_dir = PROJECT_ROOT / 'data' / 'us_stocks' / 'prices'
        self.features_dir = PROJECT_ROOT / 'data' / 'us_stocks' / 'features'
        self.prices_dir.mkdir(parents=True, exist_ok=True)
        self.features_dir.mkdir(parents=True, exist_ok=True)

    def collect_daily(self, universe: Dict=None) -> Dict:
        """일일 수집 편의 메서드 (collect_all + build_features)."""
        result = self.collect_all(universe)
        if result.get('success', 0) > 0:
            try:
                self.build_features()
            except Exception as e:
                logger.warning(f'  피처 생성 실패: {e}', exc_info=True)
        return result

    def collect_all(self, universe: Dict=None) -> Dict:
        """전체 US 유니버스 수집 (KIS -> Alpha Vantage -> yfinance 3중 Fallback 구조)."""
        universe = universe or US_L2_UNIVERSE
        tickers = list(universe.keys())
        logger.info(f'📡 US 데이터 수집 (Live/3중 폴백): {len(tickers)}종목')
        
        from src.data_collection.kis_data_collector import KISDataCollector
        from src.data_collection.alpha_vantage_collector import (
            collect_us_daily_ohlcv as av_collect_raw,
            collect_fx_daily_ohlcv as fx_collect_raw,
            collect_econ_indicator_daily_ohlcv as econ_collect_raw
        )
        from src.utils.file_ops import atomic_write_parquet
        from src.utils.retry_utils import with_retry
        import yfinance as yf
        
        # 적용: Exponential Backoff 재시도 래퍼
        av_collect = with_retry(max_retries=3, initial_delay=2.0)(av_collect_raw)
        collect_fx_daily_ohlcv = with_retry(max_retries=3, initial_delay=2.0)(fx_collect_raw)
        collect_econ_indicator_daily_ohlcv = with_retry(max_retries=3, initial_delay=2.0)(econ_collect_raw)
        
        kis = KISDataCollector()
        kis_ready = kis._ensure_auth()
        
        success = 0
        failed = 0
        end = datetime.now()
        start = end - timedelta(days=self.lookback_days)
        start_str = start.strftime('%Y-%m-%d')
        end_str = end.strftime('%Y-%m-%d')
        
        def _process_and_save(df, t):
            if df is None or df.empty:
                return False
            # Ensure index is date and columns are correct
            if 'date' in df.columns:
                df = df.set_index('date')
            elif df.index.name != 'date':
                df.index.name = 'date'
                
            df.columns = [str(c).lower() for c in df.columns]
            if 'close' not in df.columns:
                return False
                
            df = df.dropna(subset=['close'])
            if len(df) < 20:
                logger.warning(f'  ⚠️ {t}: 데이터 부족 ({len(df)}일)')
                return False
                
            pq_path = self.prices_dir / f'{t}.parquet'
            atomic_write_parquet(df, pq_path)
            return True

        def _is_sane(df) -> tuple[bool, float]:
            """Z-Score 기반 이상치 검증. (정상여부, 수익률) 반환"""
            if df is None or df.empty or 'close' not in df.columns or len(df) < 20:
                return False, 0.0
            
            # Ensure proper index
            if df.index.name != 'date' and 'date' in df.columns:
                df = df.set_index('date')
            
            close = df['close'].dropna()
            if len(close) < 20:
                return False, 0.0
                
            rets = close.pct_change().dropna()
            if len(rets) < 19:
                return False, 0.0
                
            latest_ret = float(rets.iloc[-1])
            hist_std = float(rets.iloc[-20:-1].std())
            
            # 동적 변동성 임계치: 5-Sigma 초과 및 절대수익률 7% 초과시 이상치로 간주
            if hist_std > 0:
                z_score = abs(latest_ret) / hist_std
                if z_score > 5.0 and abs(latest_ret) > 0.07:
                    # [NEW] 액면분할 방어 로직 (Corporate Action Check)
                    # Alpha Vantage 데이터의 경우 split_coefficient가 제공되므로 이를 확인
                    if 'split_coefficient' in df.columns:
                        recent_splits = df['split_coefficient'].tail(3)
                        if (recent_splits != 1.0).any():
                            logger.info(f"  ℹ️ [Corporate Action] 액면분할 감지 (Split != 1.0). 폭락(Crash) 판정 면제.")
                            return True, latest_ret
                    return False, latest_ret
            return True, latest_ret

        for ticker in tickers:
            df_kis = df_av = df_yf = df_fred = None
            source = ""
            
            # [Macro & Commodity Branching]
            if ticker == '^VIX':
                # VIX: yfinance (Primary) -> FRED (Fallback)
                try:
                    single_data = yf.download('^VIX', start=start_str, end=end_str, auto_adjust=True, progress=False)
                    if single_data is not None and not single_data.empty:
                        df_yf = single_data.copy()
                        if isinstance(df_yf.columns, pd.MultiIndex):
                            df_yf.columns = [c[0] for c in df_yf.columns]
                        df_yf.columns = [str(c).lower() for c in df_yf.columns]
                        if _process_and_save(df_yf, ticker):
                            source = "yfinance"
                except Exception as e:
                    logger.debug(f"yfinance fetch failed for ^VIX: {e}")
                    
                if not source:
                    try:
                        # Fallback to FRED (VIXCLS)
                        from src.utils.credential_manager import CredentialManager
                        import requests
                        fred_key = CredentialManager().read_from_env('FRED_API_KEY')
                        if fred_key:
                            url = f"https://api.stlouisfed.org/fred/series/observations?series_id=VIXCLS&api_key={fred_key}&file_type=json"
                            resp = requests.get(url, timeout=10).json()
                            if 'observations' in resp:
                                records = []
                                for obs in resp['observations']:
                                    if obs['value'] == '.': continue
                                    v = float(obs['value'])
                                    records.append({'date': obs['date'], 'open': v, 'high': v, 'low': v, 'close': v, 'volume': 0})
                                df_fred = pd.DataFrame(records)
                                df_fred['date'] = pd.to_datetime(df_fred['date'])
                                df_fred = df_fred.set_index('date').sort_index()
                                if _process_and_save(df_fred, ticker):
                                    source = "FRED (Fallback)"
                    except Exception as e:
                        logger.debug(f"FRED fetch failed for ^VIX: {e}")
                        
            elif ticker == 'KRW=X':
                # USD/KRW: AV FX_DAILY (Primary) -> FDR (Fallback)
                df_av = collect_fx_daily_ohlcv('USD', 'KRW')
                if df_av is not None and _process_and_save(df_av, ticker):
                    source = "AlphaVantage (FX_DAILY)"
                else:
                    try:
                        import FinanceDataReader as fdr
                        df_fdr = fdr.DataReader('USD/KRW', start_str)
                        if df_fdr is not None and not df_fdr.empty:
                            df_fdr = df_fdr.reset_index()
                            df_fdr = df_fdr.rename(columns={'Date': 'date', 'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close'})
                            if _process_and_save(df_fdr, ticker):
                                source = "FDR (Fallback)"
                    except Exception as e:
                        logger.debug(f"FDR fetch failed for KRW=X: {e}")
                        
            elif ticker == '^TNX':
                # US 10Y Treasury: AV TREASURY_YIELD (Primary) -> FRED (Fallback)
                df_av = collect_econ_indicator_daily_ohlcv('TREASURY_YIELD', maturity='10year')
                if df_av is not None and _process_and_save(df_av, ticker):
                    source = "AlphaVantage (TREASURY_YIELD)"
                else:
                    try:
                        from src.utils.credential_manager import CredentialManager
                        import requests
                        fred_key = CredentialManager().read_from_env('FRED_API_KEY')
                        if fred_key:
                            url = f"https://api.stlouisfed.org/fred/series/observations?series_id=DGS10&api_key={fred_key}&file_type=json"
                            resp = requests.get(url, timeout=10).json()
                            if 'observations' in resp:
                                records = []
                                for obs in resp['observations']:
                                    if obs['value'] == '.': continue
                                    v = float(obs['value'])
                                    records.append({'date': obs['date'], 'open': v, 'high': v, 'low': v, 'close': v, 'volume': 0})
                                df_fred = pd.DataFrame(records)
                                df_fred['date'] = pd.to_datetime(df_fred['date'])
                                df_fred = df_fred.set_index('date').sort_index()
                                if _process_and_save(df_fred, ticker):
                                    source = "FRED (Fallback)"
                    except Exception as e:
                        logger.debug(f"FRED fetch failed for ^TNX: {e}")
                        
            elif ticker == 'CL=F':
                # WTI Crude: AV WTI (Primary)
                df_av = collect_econ_indicator_daily_ohlcv('WTI')
                if df_av is not None and _process_and_save(df_av, ticker):
                    source = "AlphaVantage (WTI)"
                    
            elif ticker in ['GC=F', 'DX-Y.NYB', '^GSPC', '^IXIC']:
                # Mapped to ETFs for KIS / AV
                mapping = {'GC=F': 'GLD', 'DX-Y.NYB': 'UUP', '^GSPC': 'SPY', '^IXIC': 'QQQ'}
                proxy_ticker = mapping[ticker]
                logger.info(f"  🔄 {ticker} 우회 수집: {proxy_ticker} ETF 데이터로 대체 수집")
                
                # 1. KIS (Primary)
                if kis_ready:
                    try: df_kis = kis.get_us_daily_ohlcv(proxy_ticker)
                    except: pass
                is_sane_kis, ret_kis = _is_sane(df_kis)
                if is_sane_kis and _process_and_save(df_kis, ticker):
                    source = f"KIS (Proxy: {proxy_ticker})"
                else:
                    # 2. AV (Fallback)
                    try: df_av = av_collect(proxy_ticker)
                    except: pass
                    is_sane_av, ret_av = _is_sane(df_av)
                    if is_sane_av and _process_and_save(df_av, ticker):
                        source = f"AlphaVantage (Proxy: {proxy_ticker})"
                        
            else:
                # [Equity Branching: KIS -> AV ONLY]
                # 1. KIS API (Primary)
                if kis_ready:
                    # KIS API 자체에 백오프 래퍼 적용
                    kis_get = with_retry(max_retries=3, initial_delay=1.0)(kis.get_us_daily_ohlcv)
                    try:
                        df_kis = kis_get(ticker)
                    except Exception as e:
                        logger.debug(f"KIS fetch failed for {ticker}: {e}")
                        
                is_sane_kis, ret_kis = _is_sane(df_kis)
                
                if is_sane_kis and _process_and_save(df_kis, ticker):
                    source = "KIS"
                else:
                    if df_kis is not None and not is_sane_kis:
                        logger.warning(f"  ⚠️ [Sanity Check] {ticker} KIS 데이터 이상치 감지 (Z-Score 기각). AlphaVantage로 교차 검증 시도...")
                    
                    # 2. Alpha Vantage (Fallback 1 / Cross-Validation)
                    try:
                        df_av = av_collect(ticker)
                    except Exception as e:
                        logger.debug(f"AlphaVantage fetch failed for {ticker}: {e}")
                        
                    is_sane_av, ret_av = _is_sane(df_av)
                    
                    # 교차 검증 (Cross Validation)
                    if not is_sane_kis and df_kis is not None and df_av is not None:
                        # 두 소스가 동일하게 미친 수익률을 뿜는다면(오차 1% 이내), 그것은 현실(Real Crash/Split)이다.
                        if abs(ret_kis - ret_av) < 0.01:
                            logger.critical(f"  🚨 [Cross Validated] {ticker} 폭락/폭등 교차 검증 일치! 실제 시장 상황으로 수용.")
                            if _process_and_save(df_kis, ticker):
                                source = "KIS (Cross-Verified Anomaly)"
                    
                    if not source and is_sane_av and _process_and_save(df_av, ticker):
                        source = "AlphaVantage"
            
            if source:
                success += 1
                logger.debug(f'      ✅ {ticker} 수집 성공 ({source})')
            else:
                failed += 1
                logger.error(f'  🚨 {ticker} 수집 완전 실패 (모든 소스 무응답)')
                
            time.sleep(0.1)
            
        logger.info(f'  ✅ US 일봉 수집 완료: {success} 성공 / {failed} 실패')
        return {'success': success, 'failed': failed}

    def build_features(self, date_str: str=None) -> Dict[str, Dict]:
        """수집된 일봉에서 ML 피처 벡터 생성."""
        date_str = date_str or datetime.now().strftime('%Y-%m-%d')
        features = {}
        for csv_file in sorted(self.prices_dir.glob('*.csv')):
            ticker = csv_file.stem
            try:
                df = pd.read_csv(csv_file, index_col='date', parse_dates=True)
                if len(df) < 50:
                    continue
                feat = self._compute_features(ticker, df)
                if feat:
                    features[ticker] = feat
            except Exception as e:
                logger.error(f'  {ticker} 피처 실패: {e}', exc_info=True)
        out_path = self.features_dir / f'{date_str}.json'
        from src.utils.file_ops import atomic_write_json

        atomic_write_json(out_path, {'date': date_str, 'n_stocks': len(features), 'features': features}, indent=2, default=str)
        logger.info(f'  📊 US 피처 생성: {len(features)}종목 → {out_path.name}')
        self._update_atr_cache(features)
        return features

    def _compute_features(self, ticker: str, df: pd.DataFrame) -> Optional[Dict]:
        """단일 종목 피처 계산."""
        if len(df) < 50:
            return None
        close = df['close']
        high = df['high']
        low = df['low']
        volume = df['volume'] if 'volume' in df.columns else pd.Series(0, index=df.index)
        latest = close.iloc[-1]
        prev = close.iloc[-2] if len(close) > 1 else latest
        ret_1d = latest / prev - 1 if prev > 0 else 0
        ret_5d = latest / close.iloc[-6] - 1 if len(close) > 5 else 0
        ret_20d = latest / close.iloc[-21] - 1 if len(close) > 20 else 0
        ret_60d = latest / close.iloc[-61] - 1 if len(close) > 60 else 0
        sma_20 = close.rolling(20).mean().iloc[-1]
        sma_50 = close.rolling(50).mean().iloc[-1]
        sma_200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else sma_50
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain.iloc[-1] / loss.iloc[-1] if loss.iloc[-1] > 0 else 100
        rsi_14 = 100 - 100 / (1 + rs)
        tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
        atr_14 = tr.rolling(14).mean().iloc[-1]
        atr_pct = atr_14 / latest if latest > 0 else 0.02
        ema_12 = close.ewm(span=12).mean()
        ema_26 = close.ewm(span=26).mean()
        macd = ema_12.iloc[-1] - ema_26.iloc[-1]
        macd_signal = (ema_12 - ema_26).ewm(span=9).mean().iloc[-1]
        macd_hist = macd - macd_signal
        bb_mid = sma_20
        bb_std = close.rolling(20).std().iloc[-1]
        bb_upper = bb_mid + 2 * bb_std
        bb_lower = bb_mid - 2 * bb_std
        bb_pct = (latest - bb_lower) / (bb_upper - bb_lower) if bb_upper > bb_lower else 0.5
        vol_ratio = volume.iloc[-1] / volume.rolling(20).mean().iloc[-1] if volume.rolling(20).mean().iloc[-1] > 0 else 1.0
        high_52w = high.iloc[-252:].max() if len(high) >= 252 else high.max()
        low_52w = low.iloc[-252:].min() if len(low) >= 252 else low.min()
        pct_from_high = latest / high_52w - 1 if high_52w > 0 else 0
        pct_from_low = latest / low_52w - 1 if low_52w > 0 else 0
        vol_20d = close.pct_change().rolling(20).std().iloc[-1] * np.sqrt(252)
        financials = self._load_financials(ticker)
        feat = {'ticker': ticker, 'close': round(latest, 2), 'date': str(df.index[-1].date()), 'ret_1d': round(ret_1d, 6), 'ret_5d': round(ret_5d, 6), 'ret_20d': round(ret_20d, 6), 'ret_60d': round(ret_60d, 6), 'rsi_14': round(rsi_14, 2), 'sma_20_ratio': round(latest / sma_20, 4) if sma_20 > 0 else 1.0, 'sma_50_ratio': round(latest / sma_50, 4) if sma_50 > 0 else 1.0, 'sma_200_ratio': round(latest / sma_200, 4) if sma_200 > 0 else 1.0, 'atr_pct': round(atr_pct, 6), 'macd_hist': round(macd_hist, 4), 'bb_pct': round(bb_pct, 4), 'vol_ratio': round(vol_ratio, 4), 'vol_20d': round(vol_20d, 4), 'pct_from_52w_high': round(pct_from_high, 4), 'pct_from_52w_low': round(pct_from_low, 4), **financials, 'sector': US_L2_UNIVERSE.get(ticker, {}).get('sector', 'Unknown'), 'exchange': US_L2_UNIVERSE.get(ticker, {}).get('exchange', 'NASD')}
        return feat

    def _load_financials(self, ticker: str) -> Dict:
        """기존 수집된 재무 데이터 로드."""
        fin_path = PROJECT_ROOT / 'data' / 'us_financials' / f'{ticker}.json'
        defaults = {'pe_ratio': 0, 'pb_ratio': 0, 'market_cap': 0, 'revenue_growth': 0, 'eps': 0, 'dividend_yield': 0}
        if not fin_path.exists():
            return defaults
        try:
            d = json.load(open(fin_path))
            return {'pe_ratio': d.get('pe_ratio', d.get('PE', 0)) or 0, 'pb_ratio': d.get('pb_ratio', d.get('PB', 0)) or 0, 'market_cap': d.get('market_cap', 0) or 0, 'revenue_growth': d.get('revenue_growth', 0) or 0, 'eps': d.get('eps', 0) or 0, 'dividend_yield': d.get('dividend_yield', 0) or 0}
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return defaults

    def _update_atr_cache(self, features: Dict):
        """ATR 캐시에 US 종목 추가 (L1 TP/SL용)."""
        cache_path = PROJECT_ROOT / 'results' / 'prefetch_atr_cache.json'
        cache = {}
        if cache_path.exists():
            try:
                cache = json.load(open(cache_path))
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                cache = {}
        updated = 0
        for ticker, feat in features.items():
            atr_pct = feat.get('atr_pct', 0)
            if atr_pct > 0:
                cache[ticker] = {'atr_pct': atr_pct, 'updated': datetime.now().isoformat()}
                updated += 1
        atomic_write_json(cache_path, cache, indent=2)
        if updated:
            logger.info(f'  💾 ATR 캐시 갱신: {updated}종목 (US)')
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    collector = USStockCollector()
    logger.info('=' * 60)
    logger.info('📡 US Stock Data Collector')
    logger.info('=' * 60)
    result = collector.collect_all()
    logger.info(f'일봉: {result['success']}종목 수집, {result['failed']}실패')
    features = collector.build_features()
    logger.info(f'피처: {len(features)}종목 생성')
    logger.info('=' * 60)
    logger.info('✅ 완료')