"""
Market Breadth & Options Data Collector
=======================================
1. VKOSPI (한국판 VIX) — 장중 공포 지수
2. Put/Call Ratio — 옵션 시장 센티먼트
3. 글로벌 상관관계 — Risk-On/Off 감지
4. Economic Surprise Index — 매크로 서프라이즈

Author: Project-A
Date: 2026-03-15
"""
import json
import logging
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CACHE_DIR = _PROJECT_ROOT / 'data' / 'cache' / 'market_breadth'

class MarketBreadthCollector:
    """
    확장 시장 데이터 수집기.
    
    수집 항목:
      - VKOSPI (변동성 지수)
      - Put/Call Ratio (KRX 200 옵션)
      - 글로벌 자산 상관관계 (S&P500/DXY/WTI/Gold/US10Y)
      - Economic Surprise Index (FRED 기반)
    """

    def __init__(self):
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.cache_file = _CACHE_DIR / 'market_breadth_latest.json'
        self._data = self._load_cache()

    def collect_vkospi(self) -> Dict:
        """VKOSPI 수집 — yfinance VIX → KOSPI 변환"""
        try:
            import yfinance as yf
            vix_data = yf.download('^VIX', period='60d', progress=False, timeout=10)
            if vix_data is not None and len(vix_data) > 0:
                close = vix_data['Close']
                if hasattr(close, 'columns'):
                    close = close.iloc[:, 0]
                vix_now = float(close.iloc[-1])
                vix_prev = float(close.iloc[-2]) if len(close) > 1 else vix_now
                vix_ma20 = float(close.tail(20).mean())
                vkospi = round(vix_now * 0.85, 2)
                result = {'vkospi': vkospi, 'vix': round(vix_now, 2), 'vkospi_prev': round(vix_prev * 0.85, 2), 'vkospi_change': round((vix_now / vix_prev - 1) * 100, 2), 'vkospi_ma20': round(vix_ma20 * 0.85, 2), 'timestamp': datetime.now().isoformat(), 'source': 'yfinance_vix'}
                if vkospi >= 30:
                    result['level'] = 'extreme_fear'
                elif vkospi >= 25:
                    result['level'] = 'fear'
                elif vkospi >= 18:
                    result['level'] = 'neutral'
                elif vkospi >= 12:
                    result['level'] = 'greed'
                else:
                    result['level'] = 'extreme_greed'
                self._data['vkospi'] = result
                logger.info(f'  ✅ VKOSPI: {vkospi:.1f} (VIX={vix_now:.1f}, {result['level']})')
                return result
        except Exception as e:
            logger.warning(f'  VKOSPI 수집 실패: {e}', exc_info=True)
        return self._estimate_vkospi_from_realized()

    def _estimate_vkospi_from_realized(self) -> Dict:
        """KOSPI 실현 변동성에서 VKOSPI 추정"""
        try:
            kospi_csv = _PROJECT_ROOT / 'data' / 'raw' / 'indices' / 'KOSPI.csv'
            if kospi_csv.exists():
                df = pd.read_csv(kospi_csv, index_col=0, parse_dates=True)
                close = df['Close'].dropna().tail(60)
                if len(close) >= 20:
                    realized = float(close.pct_change().tail(20).std() * np.sqrt(252) * 100)
                    estimated = realized * 1.15
                    result = {'vkospi': round(estimated, 2), 'vkospi_prev': round(estimated, 2), 'vkospi_change': 0, 'vkospi_ma20': round(estimated, 2), 'level': 'estimated', 'source': 'realized_vol', 'timestamp': datetime.now().isoformat()}
                    self._data['vkospi'] = result
                    logger.info(f'  ⚠️ VKOSPI 추정: {estimated:.1f} (실현변동성 기반)')
                    return result
        except Exception as e:
            logger.warning(f'  VKOSPI 추정 실패: {e}', exc_info=True)
        return {'vkospi': 20.0, 'level': 'unknown', 'source': 'default'}

    def collect_put_call_ratio(self) -> Dict:
        """KRX 200 옵션 Put/Call 비율"""
        try:
            from pykrx import stock as pykrx_stock
            end_date = datetime.now()
            start_date = end_date - timedelta(days=90)
            end_str = end_date.strftime('%Y%m%d')
            start_str = start_date.strftime('%Y%m%d')
            try:
                put_data = pykrx_stock.get_index_ohlcv(start_str, end_str, '1006')
                call_data = pykrx_stock.get_index_ohlcv(start_str, end_str, '1005')
                if put_data is not None and call_data is not None and (not put_data.empty) and (not call_data.empty):
                    df = pd.DataFrame({'put': put_data['거래량'], 'call': call_data['거래량']}).dropna()
                    df = df[df['call'] > 0]
                    df['pc_ratio'] = df['put'] / df['call']
                    if not df.empty:
                        pc_ratio = float(df['pc_ratio'].iloc[-1])
                        put_vol = float(df['put'].iloc[-1])
                        call_vol = float(df['call'].iloc[-1])
                        mean = df['pc_ratio'].mean()
                        std = df['pc_ratio'].std()
                        if pd.isna(std) or std == 0:
                            std = 0.001
                        z_score = float((pc_ratio - mean) / std)
                        result = {'put_call_ratio': round(pc_ratio, 3), 'put_volume': int(put_vol), 'call_volume': int(call_vol), 'z_score': round(z_score, 2), 'date': end_str, 'timestamp': datetime.now().isoformat()}
                        if z_score > 2.0:
                            result['sentiment'] = 'extreme_fear'
                        elif z_score > 1.0:
                            result['sentiment'] = 'fear'
                        elif z_score > -1.0:
                            result['sentiment'] = 'neutral'
                        elif z_score > -2.0:
                            result['sentiment'] = 'greed'
                        else:
                            result['sentiment'] = 'extreme_greed'
                        self._data['put_call'] = result
                        logger.info(f'  ✅ P/C Ratio: {pc_ratio:.3f} (z={z_score:.2f}, {result['sentiment']})')
                        return result
            except Exception as _e:
                logger.error(f'  P/C Ratio 데이터 로드 중 예외: {_e}', exc_info=True)
        except Exception as e:
            logger.warning(f'  P/C Ratio 수집 실패: {e}', exc_info=True)
        return {'put_call_ratio': 1.0, 'sentiment': 'unknown', 'source': 'default'}

    def collect_global_correlations(self) -> Dict:
        """글로벌 자산 간 Rolling Correlation (20일)"""
        try:
            import yfinance as yf
            tickers = {'KOSPI': '^KS11', 'SP500': '^GSPC', 'WTI': 'CL=F', 'GOLD': 'GC=F', 'US10Y': '^TNX'}
            end = datetime.now()
            start = end - timedelta(days=120)
            prices = {}
            for name, ticker in tickers.items():
                try:
                    data = yf.download(ticker, start=start, end=end, progress=False, timeout=10)
                    if data is not None and len(data) > 0:
                        close = data['Close']
                        if hasattr(close, 'columns'):
                            close = close.iloc[:, 0]
                        prices[name] = close
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
                    continue
            try:
                from fredapi import Fred
                from src.utils.credential_manager import CredentialManager
                _fred_key = CredentialManager().read_from_env('FRED_API_KEY') or ''
                if _fred_key:
                    _fred = Fred(api_key=_fred_key)
                    _dxy = _fred.get_series('DTWEXBGS', observation_start=start.strftime('%Y-%m-%d'))
                    if _dxy is not None and len(_dxy) >= 20:
                        prices['DXY'] = _dxy
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
            if 'DXY' not in prices:
                try:
                    _dxy_df = yf.download('DX=F', start=start, end=end, progress=False, timeout=10)
                    if _dxy_df is not None and len(_dxy_df) > 0:
                        close = _dxy_df['Close']
                        if hasattr(close, 'columns'):
                            close = close.iloc[:, 0]
                        prices['DXY'] = close
                except Exception as _e:
                    logger.warning(f'  suppressed: {_e}', exc_info=True)
            if 'KOSPI' not in prices or len(prices) < 3:
                return {'status': 'insufficient_data'}
            df = pd.DataFrame(prices)
            returns = df.pct_change(fill_method=None).dropna()
            correlations = {}
            current_corr = {}
            for asset in ['SP500', 'DXY', 'WTI', 'GOLD', 'US10Y']:
                if asset in returns.columns:
                    rolling_corr = returns['KOSPI'].rolling(20).corr(returns[asset])
                    if len(rolling_corr.dropna()) > 0:
                        curr = float(rolling_corr.iloc[-1])
                        avg = float(rolling_corr.tail(60).mean())
                        current_corr[f'KOSPI_{asset}'] = round(curr, 3)
                        correlations[f'KOSPI_{asset}'] = {'current': round(curr, 3), 'avg_60d': round(avg, 3), 'deviation': round(curr - avg, 3)}
            sp_corr = current_corr.get('KOSPI_SP500', 0)
            dxy_corr = current_corr.get('KOSPI_DXY', 0)
            gold_corr = current_corr.get('KOSPI_GOLD', 0)
            if sp_corr > 0.5 and dxy_corr < -0.2:
                risk_mode = 'risk_on'
            elif sp_corr < 0.2 or dxy_corr > 0.3:
                risk_mode = 'risk_off'
            else:
                risk_mode = 'neutral'
            copper_gold = None
            if 'WTI' in prices and 'GOLD' in prices:
                try:
                    cg = prices['WTI'] / prices['GOLD']
                    cg_ret = float(cg.iloc[-1] / cg.iloc[-20] - 1) if len(cg) > 20 else 0
                    copper_gold = round(cg_ret, 4)
                except Exception as _e:
                    logger.warning(f'  suppressed: {_e}', exc_info=True)
            result = {'correlations': correlations, 'current_corr': current_corr, 'risk_mode': risk_mode, 'copper_gold_momentum': copper_gold, 'timestamp': datetime.now().isoformat()}
            self._data['global_corr'] = result
            logger.info(f'  ✅ 글로벌 상관관계: {risk_mode}, SP500={sp_corr:.2f}, DXY={dxy_corr:.2f}')
            return result
        except Exception as e:
            logger.warning(f'  글로벌 상관관계 수집 실패: {e}', exc_info=True)
            return {'status': 'error', 'error': str(e)}

    def collect_economic_surprise(self) -> Dict:
        """경제 서프라이즈 인덱스 (실제값 vs 예상값)"""
        try:
            macro_files = {'CPI': _PROJECT_ROOT / 'data' / 'raw' / 'economic_indicators' / 'USA_CPI.csv', 'GDP': _PROJECT_ROOT / 'data' / 'raw' / 'economic_indicators' / 'USA_GDP.csv', 'KOR_CPI': _PROJECT_ROOT / 'data' / 'raw' / 'korea_economic' / 'KOR_CPI.csv', 'KOR_GDP': _PROJECT_ROOT / 'data' / 'raw' / 'economic_indicators' / 'KOR_GDP.csv'}
            surprises = {}
            surprise_scores = []
            for name, path in macro_files.items():
                if path.exists():
                    try:
                        df = pd.read_csv(path, index_col=0, parse_dates=True)
                        col = df.columns[0] if len(df.columns) > 0 else None
                        if col and len(df) >= 12:
                            values = df[col].dropna().tail(24)
                            if len(values) >= 12:
                                rolling_mean = values.rolling(6).mean()
                                rolling_std = values.rolling(12).std()
                                if len(rolling_mean.dropna()) > 0 and len(rolling_std.dropna()) > 0:
                                    latest = float(values.iloc[-1])
                                    expected = float(rolling_mean.iloc[-1])
                                    std = max(float(rolling_std.iloc[-1]), 0.001)
                                    z = (latest - expected) / std
                                    surprises[name] = {'latest': round(latest, 3), 'expected': round(expected, 3), 'z_score': round(z, 2)}
                                    surprise_scores.append(z)
                    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
                        import logging
                        logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
                        continue
            if surprise_scores:
                esi = float(np.mean(surprise_scores))
                result = {'economic_surprise_index': round(esi, 3), 'indicators': surprises, 'interpretation': 'positive_surprise' if esi > 0.5 else 'negative_surprise' if esi < -0.5 else 'inline', 'timestamp': datetime.now().isoformat()}
                self._data['eco_surprise'] = result
                logger.info(f'  ✅ ESI: {esi:+.3f} ({result['interpretation']}), {len(surprises)}개 지표')
                return result
        except Exception as e:
            logger.warning(f'  ESI 수집 실패: {e}', exc_info=True)
        return {'economic_surprise_index': 0, 'interpretation': 'unknown'}

    def collect_all(self) -> Dict:
        """전체 데이터 수집 실행"""
        logger.info('📊 Market Breadth 데이터 수집 시작')
        vkospi = self.collect_vkospi()
        pc_ratio = self.collect_put_call_ratio()
        global_corr = self.collect_global_correlations()
        eco_surprise = self.collect_economic_surprise()
        sentiment_score = self._calculate_composite_sentiment()
        result = {'vkospi': vkospi, 'put_call': pc_ratio, 'global_correlations': global_corr, 'economic_surprise': eco_surprise, 'composite_sentiment': sentiment_score, 'timestamp': datetime.now().isoformat()}
        self._save_cache()
        logger.info(f'  📊 종합 센티먼트: {sentiment_score['score']:.1f}/100 ({sentiment_score['label']})')
        return result

    def _calculate_composite_sentiment(self) -> Dict:
        """종합 시장 센티먼트 (0=극공포, 100=극탐욕)"""
        scores = []
        vkospi_data = self._data.get('vkospi', {})
        vkospi = vkospi_data.get('vkospi', 20)
        vkospi_score = max(0, min(100, 100 - (vkospi - 10) * 3.3))
        scores.append(('VKOSPI', vkospi_score, 0.35))
        pc_data = self._data.get('put_call', {})
        pc = pc_data.get('put_call_ratio', 1.0)
        pc_score = max(0, min(100, 100 - (pc - 0.6) * 100))
        scores.append(('P/C_Ratio', pc_score, 0.25))
        corr_data = self._data.get('global_corr', {})
        risk_mode = corr_data.get('risk_mode', 'neutral')
        risk_score = {'risk_on': 75, 'neutral': 50, 'risk_off': 25}.get(risk_mode, 50)
        scores.append(('Risk_Mode', risk_score, 0.2))
        esi_data = self._data.get('eco_surprise', {})
        esi = esi_data.get('economic_surprise_index', 0)
        esi_score = max(0, min(100, 50 + esi * 25))
        scores.append(('ESI', esi_score, 0.2))
        total = sum((s * w for _, s, w in scores))
        total_weight = sum((w for _, _, w in scores))
        composite = total / total_weight if total_weight > 0 else 50
        if composite >= 75:
            label = 'extreme_greed'
        elif composite >= 60:
            label = 'greed'
        elif composite >= 40:
            label = 'neutral'
        elif composite >= 25:
            label = 'fear'
        else:
            label = 'extreme_fear'
        return {'score': round(composite, 1), 'label': label, 'components': {name: round(s, 1) for name, s, _ in scores}}

    def get_regime_features(self) -> Dict:
        """
        레짐 엔진에서 사용할 피처 반환.
        
        Returns:
            {
                'vkospi': float,
                'vkospi_level': str,
                'put_call_ratio': float,
                'risk_mode': str,
                'eco_surprise': float,
                'composite_sentiment': float,
            }
        """
        vk = self._data.get('vkospi', {})
        pc = self._data.get('put_call', {})
        gc = self._data.get('global_corr', {})
        es = self._data.get('eco_surprise', {})
        return {'vkospi': vk.get('vkospi', 20.0), 'vkospi_level': vk.get('level', 'unknown'), 'vkospi_change': vk.get('vkospi_change', 0), 'put_call_ratio': pc.get('put_call_ratio', 1.0), 'pc_sentiment': pc.get('sentiment', 'unknown'), 'risk_mode': gc.get('risk_mode', 'neutral'), 'sp500_corr': gc.get('current_corr', {}).get('KOSPI_SP500', 0), 'dxy_corr': gc.get('current_corr', {}).get('KOSPI_DXY', 0), 'eco_surprise': es.get('economic_surprise_index', 0), 'composite_sentiment': self._calculate_composite_sentiment().get('score', 50)}

    def _load_cache(self) -> Dict:
        try:
            if self.cache_file.exists():
                with open(self.cache_file) as f:
                    data = json.load(f)
                ts = data.get('timestamp', '')
                if ts:
                    cached_time = datetime.fromisoformat(ts)
                    if (datetime.now() - cached_time).total_seconds() < 86400:
                        return data
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
        return {}

    def _save_cache(self):
        try:
            self._data['timestamp'] = datetime.now().isoformat()
            with open(self.cache_file, 'w') as f:
                json.dump(self._data, f, indent=2, default=str, ensure_ascii=False)
        except Exception as e:
            logger.warning(f'캐시 저장 실패: {e}', exc_info=True)