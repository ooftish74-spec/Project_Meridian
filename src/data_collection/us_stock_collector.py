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
US_L2_UNIVERSE = {'AAPL': {'sector': 'Technology', 'exchange': 'NASD'}, 'MSFT': {'sector': 'Technology', 'exchange': 'NASD'}, 'GOOGL': {'sector': 'Technology', 'exchange': 'NASD'}, 'AMZN': {'sector': 'Technology', 'exchange': 'NASD'}, 'NVDA': {'sector': 'Technology', 'exchange': 'NASD'}, 'META': {'sector': 'Technology', 'exchange': 'NASD'}, 'TSLA': {'sector': 'Consumer Cyclical', 'exchange': 'NASD'}, 'AVGO': {'sector': 'Technology', 'exchange': 'NASD'}, 'AMD': {'sector': 'Technology', 'exchange': 'NASD'}, 'CRM': {'sector': 'Technology', 'exchange': 'NASD'}, 'ORCL': {'sector': 'Technology', 'exchange': 'NASD'}, 'ADBE': {'sector': 'Technology', 'exchange': 'NASD'}, 'NFLX': {'sector': 'Communication', 'exchange': 'NASD'}, 'INTC': {'sector': 'Technology', 'exchange': 'NASD'}, 'QCOM': {'sector': 'Technology', 'exchange': 'NASD'}, 'AMAT': {'sector': 'Technology', 'exchange': 'NASD'}, 'MU': {'sector': 'Technology', 'exchange': 'NASD'}, 'LRCX': {'sector': 'Technology', 'exchange': 'NASD'}, 'KLAC': {'sector': 'Technology', 'exchange': 'NASD'}, 'MRVL': {'sector': 'Technology', 'exchange': 'NASD'}, 'JPM': {'sector': 'Financial', 'exchange': 'NYSE'}, 'V': {'sector': 'Financial', 'exchange': 'NYSE'}, 'MA': {'sector': 'Financial', 'exchange': 'NYSE'}, 'BAC': {'sector': 'Financial', 'exchange': 'NYSE'}, 'GS': {'sector': 'Financial', 'exchange': 'NYSE'}, 'UNH': {'sector': 'Healthcare', 'exchange': 'NYSE'}, 'JNJ': {'sector': 'Healthcare', 'exchange': 'NYSE'}, 'LLY': {'sector': 'Healthcare', 'exchange': 'NYSE'}, 'ABBV': {'sector': 'Healthcare', 'exchange': 'NYSE'}, 'MRK': {'sector': 'Healthcare', 'exchange': 'NYSE'}, 'PG': {'sector': 'Consumer Staples', 'exchange': 'NYSE'}, 'KO': {'sector': 'Consumer Staples', 'exchange': 'NYSE'}, 'COST': {'sector': 'Consumer Staples', 'exchange': 'NASD'}, 'WMT': {'sector': 'Consumer Staples', 'exchange': 'NYSE'}, 'HD': {'sector': 'Consumer Cyclical', 'exchange': 'NYSE'}, 'MCD': {'sector': 'Consumer Cyclical', 'exchange': 'NYSE'}, 'CAT': {'sector': 'Industrials', 'exchange': 'NYSE'}, 'DE': {'sector': 'Industrials', 'exchange': 'NYSE'}, 'BA': {'sector': 'Industrials', 'exchange': 'NYSE'}, 'XOM': {'sector': 'Energy', 'exchange': 'NYSE'}, 'CVX': {'sector': 'Energy', 'exchange': 'NYSE'}, 'SPY': {'sector': 'ETF', 'exchange': 'AMEX'}, 'QQQ': {'sector': 'ETF', 'exchange': 'NASD'}, 'TQQQ': {'sector': 'ETF', 'exchange': 'NASD'}, 'SQQQ': {'sector': 'ETF', 'exchange': 'NASD'}, 'SOXL': {'sector': 'ETF', 'exchange': 'NASD'}, 'SOXS': {'sector': 'ETF', 'exchange': 'NASD'}, 'TLT': {'sector': 'ETF', 'exchange': 'NASD'}, 'GLD': {'sector': 'ETF', 'exchange': 'AMEX'}}

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
        """전체 US 유니버스 수집."""
        universe = universe or US_L2_UNIVERSE
        import yfinance as yf
        tickers = list(universe.keys())
        logger.info(f'📡 US 데이터 수집: {len(tickers)}종목')
        end = datetime.now()
        start = end - timedelta(days=self.lookback_days)
        try:
            data = yf.download(tickers, start=start.strftime('%Y-%m-%d'), end=end.strftime('%Y-%m-%d'), group_by='ticker', auto_adjust=True, progress=False, threads=True)
        except Exception as e:
            logger.error(f'  ❌ yfinance 배치 다운로드 실패: {e}', exc_info=True)
            return {'success': 0, 'failed': len(tickers)}
        success = 0
        failed = 0
        skipped_tickers = []
        for ticker in tickers:
            try:
                if data is None:
                    logger.warning(f'  ⚠️ {ticker}: yfinance data is None (전체 다운로드 실패)')
                    failed += 1
                    continue
                if len(tickers) == 1:
                    df = data.copy()
                else:
                    try:
                        raw = data[ticker]
                    except (TypeError, KeyError) as _ke:
                        logger.warning(f'  ⚠️ {ticker}: data subscript 불가 ({type(_ke).__name__}) — 건너뜀', exc_info=True)
                        skipped_tickers.append(ticker)
                        failed += 1
                        continue
                    if raw is None:
                        logger.warning(f'  ⚠️ {ticker}: yfinance 반환값 None — 건너뜀 (야후 API 미지원 티커)')
                        skipped_tickers.append(ticker)
                        failed += 1
                        continue
                    df = raw.copy()
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = [c[0] for c in df.columns]
                df = df.dropna(subset=['Close'])
                if len(df) < 20:
                    logger.warning(f'  ⚠️ {ticker}: 데이터 부족 ({len(df)}일)')
                    failed += 1
                    continue
                df.columns = [c.lower() for c in df.columns]
                df.index.name = 'date'
                csv_path = self.prices_dir / f'{ticker}.csv'
                df.to_csv(csv_path)
                success += 1
            except Exception as e:
                logger.warning(f'  ⚠️ {ticker}: 처리 중 오류 ({type(e).__name__}: {e})', exc_info=True)
                failed += 1
            if (success + failed) % 50 == 0:
                time.sleep(1)
        if skipped_tickers:
            logger.info(f'  🔄 누락 티커 {len(skipped_tickers)}개 개별 재수집 시도 (yfinance bulk 누락 방어): {skipped_tickers}')
            for ticker in skipped_tickers:
                try:
                    logger.debug(f'    - {ticker} 단독 수집 중...')
                    single_data = yf.download(ticker, start=start.strftime('%Y-%m-%d'), end=end.strftime('%Y-%m-%d'), auto_adjust=True, progress=False)
                    if single_data is not None and (not single_data.empty):
                        df = single_data.copy()
                        if isinstance(df.columns, pd.MultiIndex):
                            df.columns = [c[0] for c in df.columns]
                        df = df.dropna(subset=['Close'])
                        if len(df) >= 20:
                            df.columns = [c.lower() for c in df.columns]
                            df.index.name = 'date'
                            csv_path = self.prices_dir / f'{ticker}.csv'
                            df.to_csv(csv_path)
                            success += 1
                            failed -= 1
                            logger.info(f'      ✅ {ticker} 개별 재수집 성공')
                        else:
                            logger.warning(f'      ⚠️ {ticker} 개별 수집 실패 (데이터 {len(df)}일 부족)')
                    else:
                        logger.warning(f'      ⚠️ {ticker} 개별 수집 실패 (여전히 None/Empty 반환)')
                except Exception as retry_e:
                    logger.warning(f'      ⚠️ {ticker} 개별 수집 중 예상치 못한 오류: {retry_e}', exc_info=True)
                time.sleep(1)
            logger.warning(f'  🚨 yfinance 개별 재수집 후 최종 누락 티커: {failed}건')
        logger.info(f'  ✅ US 일봉 수집: {success}/{len(tickers)} 완료')
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
        with open(out_path, 'w') as f:
            json.dump({'date': date_str, 'n_stocks': len(features), 'features': features}, f, indent=2, default=str)
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
        with open(cache_path, 'w') as f:
            json.dump(cache, f, indent=2)
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