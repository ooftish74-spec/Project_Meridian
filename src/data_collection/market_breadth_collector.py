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
from src.utils.file_ops import atomic_write_json

try:
    from src.params import pget
except Exception:
    def pget(name, fallback, ctx=None):
        return fallback

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
        """VKOSPI 수집 — signal_cache, 실시간 VIX, KRX 실현 변동성 통합"""
        try:
            vix_now = float(self._data.get('vix', 0.0) or 0.0)
            vkospi_direct = 0.0
            sc_path = _PROJECT_ROOT / 'results' / 'signal_cache.json'
            if sc_path.exists():
                try:
                    sc = json.loads(sc_path.read_text(encoding='utf-8'))
                    if float(sc.get('vix', 0) or 0) > 0:
                        vix_now = float(sc['vix'])
                    if float(sc.get('vkospi', 0) or 0) > 0:
                        vkospi_direct = float(sc['vkospi'])
                except Exception:
                    pass
            if vix_now <= 0:
                try:
                    from src.utils.google_finance_collector import GoogleFinanceCollector
                    live_vix = GoogleFinanceCollector.get_vix()
                    if live_vix > 0:
                        vix_now = live_vix
                except Exception:
                    pass
            if vix_now <= 0:
                vix_now = float(pget('data.vkospi_fallback_vix', 18.5))
            vix_prev = float(self._data.get('vix_prev', vix_now) or vix_now)
            vix_ma20 = float(self._data.get('vix_ma20', vix_now) or vix_now)
            realized_res = self._estimate_vkospi_from_realized(vix_hint=vix_now)
            realized_est = float(realized_res.get('vkospi', 0.0) or 0.0)
            is_crash_1d = bool(realized_res.get('is_crash_1d', False))
            max_vix_ratio = float(pget('data.vkospi_max_vix_ratio', 1.35))
            vix_cap = vix_now * max_vix_ratio
            vkospi_min = float(pget('data.vkospi_min_valid', 5.0))
            if (
                vkospi_direct
                and vkospi_min <= vkospi_direct <= 100.0
                and (not realized_est or abs(vkospi_direct - vix_now) <= abs(realized_est - vix_now))
            ):
                vkospi = round(min(vkospi_direct, vix_cap) if not is_crash_1d else vkospi_direct, 2)
                src = 'signal_cache'
            elif realized_est and vkospi_min <= realized_est <= 100.0:
                blended = 0.6 * float(realized_est) + 0.4 * (vix_now * 0.95)
                if not is_crash_1d:
                    blended = min(blended, vix_cap)
                vkospi = round(blended, 2)
                src = 'realized_and_vix_blend'
            elif vkospi_direct and vkospi_min <= vkospi_direct <= 100.0:
                vkospi = round(min(vkospi_direct, vix_cap) if not is_crash_1d else vkospi_direct, 2)
                src = 'signal_cache'
            else:
                vkospi = round(vix_now * 0.85, 2)
                src = 'local_vix'
            _eps_den = float(pget('data.vkospi_eps', 1e-6))
            result = {
                'vkospi': vkospi,
                'vix': round(vix_now, 2),
                'vkospi_prev': round(vix_prev * 0.85, 2),
                'vkospi_change': round((vix_now / max(vix_prev, _eps_den) - 1) * 100, 2),
                'vkospi_ma20': round(vix_ma20 * 0.85, 2),
                'timestamp': datetime.now().isoformat(),
                'source': src,
            }
            _th_ext_fear = float(pget('data.vkospi_level_extreme_fear', 30.0))
            _th_fear = float(pget('data.vkospi_level_fear', 25.0))
            _th_neutral = float(pget('data.vkospi_level_neutral', 18.0))
            _th_greed = float(pget('data.vkospi_level_greed', 12.0))
            if vkospi >= _th_ext_fear:
                result['level'] = 'extreme_fear'
            elif vkospi >= _th_fear:
                result['level'] = 'fear'
            elif vkospi >= _th_neutral:
                result['level'] = 'neutral'
            elif vkospi >= _th_greed:
                result['level'] = 'greed'
            else:
                result['level'] = 'extreme_greed'
            self._data['vkospi'] = result
            _lvl_r = result.get('level', '')
            logger.info(f"  ✅ VKOSPI: {vkospi:.1f} (VIX={vix_now:.1f}, {_lvl_r}, src={src})")
            return result
        except Exception as e:
            logger.warning(f"  VKOSPI 수집 실패: {e}", exc_info=True)
        return self._estimate_vkospi_from_realized()

    def _estimate_vkospi_from_realized(self, vix_hint: float = 0.0) -> Dict:
        """KOSPI 실현 변동성에서 VKOSPI 추정 (Huber/MAD + 5d RV + VIX Shrinkage)"""
        try:
            df = None
            for pq_path in [
                _PROJECT_ROOT / 'data' / 'kr_markets' / 'kr_069500.parquet',
                _PROJECT_ROOT / 'data' / 'korea_stocks' / 'prices' / '069500.parquet',
            ]:
                if pq_path.exists():
                    df = pd.read_parquet(pq_path)
                    break
            if df is None:
                kospi_csv = _PROJECT_ROOT / 'data' / 'raw' / 'indices' / 'KOSPI.csv'
                if kospi_csv.exists():
                    df = pd.read_csv(kospi_csv, index_col=0, parse_dates=True)
            if df is not None and not df.empty:
                close_col = 'Close' if 'Close' in df.columns else ('close' if 'close' in df.columns else None)
                if close_col:
                    close = pd.to_numeric(df[close_col], errors='coerce').dropna().tail(60)
                    _min_len = int(pget('data.vkospi_min_bars', 20))
                    if len(close) >= _min_len:
                        rets = close.pct_change().dropna().tail(20)
                        med_r = float(rets.median())
                        mad_sigma = float((rets - med_r).abs().median() * 1.4826 * np.sqrt(252) * 100.0)
                        ewma_sigma = float(rets.ewm(span=7).std().iloc[-1] * np.sqrt(252) * 100.0)
                        _n5 = int(pget('data.vkospi_short_rv_days', 5))
                        rv_5d = float(rets.tail(_n5).std() * np.sqrt(252) * 100.0) if len(rets) >= _n5 else ewma_sigma
                        ret_5d_sum = float(rets.tail(_n5).sum()) if len(rets) >= _n5 else 0.0
                        last_1d_ret = float(rets.iloc[-1]) if len(rets) >= 1 else 0.0
                        crash_1d_ret = float(pget('data.vkospi_crash_1d_ret', -0.015))
                        max_vix_ratio = float(pget('data.vkospi_max_vix_ratio', 1.35))
                        is_not_crash = (last_1d_ret >= crash_1d_ret)
                        _ewma_cap = mad_sigma * 1.5
                        robust_rv = 0.65 * mad_sigma + 0.35 * min(ewma_sigma, _ewma_cap)
                        if (ret_5d_sum >= 0.0 or is_not_crash) and rv_5d < mad_sigma:
                            _short_blend = 0.5 * rv_5d + 0.5 * ewma_sigma
                            robust_rv = min(robust_rv, _short_blend)
                        vix_now = float(vix_hint or 0.0)
                        if vix_now <= 0.0:
                            sc_path = _PROJECT_ROOT / 'results' / 'signal_cache.json'
                            if sc_path.exists():
                                try:
                                    sc = json.loads(sc_path.read_text(encoding='utf-8'))
                                    vix_now = float(sc.get('vix', 0.0) or 0.0)
                                except Exception:
                                    pass
                        _vix_min = float(pget('data.vkospi_min_valid', 5.0))
                        if _vix_min <= vix_now <= 100.0:
                            _vix_cap = vix_now * max_vix_ratio
                            if is_not_crash:
                                robust_rv = min(robust_rv, _vix_cap)
                            estimated = 0.55 * (vix_now * 1.05) + 0.45 * robust_rv
                            if is_not_crash:
                                estimated = min(estimated, _vix_cap)
                        else:
                            estimated = robust_rv * 1.05
                        estimated = round(float(estimated), 2)
                        result = {
                            'vkospi': estimated,
                            'vkospi_prev': estimated,
                            'vkospi_change': 0,
                            'vkospi_ma20': estimated,
                            'level': 'estimated',
                            'source': 'realized_vol',
                            'is_crash_1d': not is_not_crash,
                            'timestamp': datetime.now().isoformat(),
                        }
                        self._data['vkospi'] = result
                        logger.info(f"  ⚠️ VKOSPI 추정: {estimated:.1f} (실현변동성 기반, MAD={mad_sigma:.1f}, RV5d={rv_5d:.1f})")
                        return result
        except Exception as e:
            logger.warning(f"  VKOSPI 추정 실패: {e}", exc_info=True)
        return {'vkospi': float(pget('data.vkospi_default', 20.0)), 'level': 'unknown', 'source': 'default'}

    def collect_put_call_ratio(self) -> Dict:
        """KRX 200 옵션 Put/Call 비율 (1순위: KRX 옵션 지수, 2순위: 인버스/레버리지 ETF 거래대금 헤지 비율)"""
        end_date = datetime.now()
        end_str = end_date.strftime('%Y%m%d')
        try:
            inv1_pq = _PROJECT_ROOT / 'data' / 'kr_markets' / 'kr_114800.parquet'
            inv2_pq = _PROJECT_ROOT / 'data' / 'kr_markets' / 'kr_252670.parquet'
            bull1_pq = _PROJECT_ROOT / 'data' / 'kr_markets' / 'kr_069500.parquet'
            bull2_pq = _PROJECT_ROOT / 'data' / 'kr_markets' / 'kr_122630.parquet'
            if inv1_pq.exists() and inv2_pq.exists() and bull1_pq.exists() and bull2_pq.exists():
                def _load_val_series(p: Path) -> pd.Series:
                    d = pd.read_parquet(p)
                    d.columns = [str(c).lower() for c in d.columns]
                    if 'date' in d.columns:
                        d['date'] = pd.to_datetime(d['date'], errors='coerce')
                        d = d.dropna(subset=['date']).set_index('date').sort_index()
                    else:
                        d.index = pd.to_datetime(d.index, errors='coerce')
                        d = d.sort_index()
                    c = pd.to_numeric(d.get('close'), errors='coerce')
                    v = pd.to_numeric(d.get('volume'), errors='coerce')
                    return (c * v).dropna().tail(90)

                s_put = _load_val_series(inv1_pq).add(_load_val_series(inv2_pq), fill_value=0)
                s_call = _load_val_series(bull1_pq).add(_load_val_series(bull2_pq), fill_value=0)
                df_pc = pd.DataFrame({'put': s_put, 'call': s_call}).dropna()
                df_pc = df_pc[(df_pc['put'] > 0) & (df_pc['call'] > 0)]
                if len(df_pc) >= 20:
                    raw_ratio = df_pc['put'] / df_pc['call']
                    mean_r = float(raw_ratio.tail(60).mean())
                    std_r = max(float(raw_ratio.tail(60).std()), 1e-4)
                    latest_r = float(raw_ratio.iloc[-1])
                    z_score = float((latest_r - mean_r) / std_r)
                    _pcr_lo, _pcr_hi = 0.4, 2.0
                    raw_pcr = float(np.clip(latest_r, _pcr_lo, _pcr_hi))
                    if z_score > 2.0:
                        sent = 'extreme_fear'
                    elif z_score > 1.0:
                        sent = 'fear'
                    elif z_score > -1.0:
                        sent = 'neutral'
                    elif z_score > -2.0:
                        sent = 'greed'
                    else:
                        sent = 'extreme_greed'
                    pcr_rounded = round(raw_pcr, 3)
                    z_rounded = round(z_score, 2)
                    result = {
                        'put_call_ratio': pcr_rounded,
                        'options_pcr': pcr_rounded,
                        'raw_etf_hedge_ratio': round(latest_r, 3),
                        'etf_hedge_zscore': z_rounded,
                        'put_volume': int(df_pc['put'].iloc[-1]),
                        'call_volume': int(df_pc['call'].iloc[-1]),
                        'z_score': z_rounded,
                        'sentiment': sent,
                        'source': 'krx_etf_hedge_ratio',
                        'date': df_pc.index[-1].strftime('%Y%m%d') if hasattr(df_pc.index[-1], 'strftime') else end_str,
                        'timestamp': datetime.now().isoformat(),
                    }
                    self._data['put_call'] = result
                    logger.info(f"  ✅ P/C Ratio (ETF Hedge Proxy): {pcr_rounded:.3f} (raw={latest_r:.3f}, z={z_score:.2f}, {sent})")
                    return result
        except Exception as e:
            logger.warning(f"  P/C Ratio ETF Proxy 계산 실패: {e}", exc_info=True)
        return {'put_call_ratio': 1.0, 'sentiment': 'neutral', 'source': 'default'}

    def collect_global_correlations(self) -> Dict:
        """글로벌 자산 간 Rolling Correlation (20일) — 로컬 Parquet 시계열 기반"""
        try:
            def _load_close_series(path: Path) -> Optional[pd.Series]:
                if not path.exists():
                    return None
                df = pd.read_parquet(path)
                df.columns = [str(c).lower() for c in df.columns]
                if 'date' in df.columns:
                    df['date'] = pd.to_datetime(df['date'], errors='coerce')
                    df = df.dropna(subset=['date']).set_index('date')
                else:
                    df.index = pd.to_datetime(df.index, errors='coerce')
                df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
                df = df[~df.index.duplicated(keep='last')].sort_index()
                if 'close' not in df.columns:
                    return None
                s = pd.to_numeric(df['close'], errors='coerce').dropna().tail(120)
                return s if len(s) >= 25 else None

            us_dir = _PROJECT_ROOT / 'data' / 'us_stocks' / 'prices'
            kr_dir = _PROJECT_ROOT / 'data' / 'kr_markets'
            asset_paths = {
                'KOSPI': kr_dir / 'kr_069500.parquet',
                'SP500': us_dir / 'SPY.parquet',
                'DXY': us_dir / 'UUP.parquet',
                'WTI': us_dir / 'USO.parquet',
                'GOLD': us_dir / 'GLD.parquet',
                'US10Y_BOND': us_dir / 'TLT.parquet',
                'COPPER': us_dir / 'CPER.parquet',
            }
            prices = {}
            for name, p in asset_paths.items():
                s = _load_close_series(p)
                if s is not None:
                    prices[name] = s

            if 'KOSPI' not in prices or len(prices) < 3:
                return {'status': 'insufficient_data'}

            df = pd.DataFrame(prices).ffill(limit=3).dropna(how='all')
            returns = df.pct_change(fill_method=None).dropna(subset=['KOSPI'])
            if 'US10Y_BOND' in returns.columns:
                returns['US10Y'] = -returns['US10Y_BOND']

            _min_pair_len, _min_cg_len, _min_macro_len = 20, 21, 12
            _sp_on, _dxy_on, _sp_off, _dxy_off = 0.4, -0.1, 0.15, 0.25
            correlations = {}
            current_corr = {}
            for asset in ['SP500', 'DXY', 'WTI', 'GOLD', 'US10Y']:
                if asset in returns.columns:
                    pair = returns[['KOSPI', asset]].dropna()
                    if len(pair) >= _min_pair_len:
                        rolling_corr = pair['KOSPI'].rolling(20).corr(pair[asset]).dropna()
                        if len(rolling_corr) > 0:
                            curr = float(rolling_corr.iloc[-1])
                            avg = float(rolling_corr.tail(60).mean())
                            current_corr[f"KOSPI_{asset}"] = round(curr, 3)
                            correlations[f"KOSPI_{asset}"] = {
                                'current': round(curr, 3),
                                'avg_60d': round(avg, 3),
                                'deviation': round(curr - avg, 3),
                            }
            sp_corr = current_corr.get('KOSPI_SP500', 0)
            dxy_corr = current_corr.get('KOSPI_DXY', 0)
            if sp_corr > _sp_on and dxy_corr < _dxy_on:
                risk_mode = 'risk_on'
            elif sp_corr < _sp_off or dxy_corr > _dxy_off:
                risk_mode = 'risk_off'
            else:
                risk_mode = 'neutral'

            copper_gold = None
            if 'COPPER' in df.columns and 'GOLD' in df.columns:
                cg = (df['COPPER'] / df['GOLD']).dropna()
                if len(cg) >= _min_cg_len:
                    copper_gold = round(float(cg.iloc[-1] / cg.iloc[-21] - 1), 4)
            elif 'WTI' in df.columns and 'GOLD' in df.columns:
                cg = (df['WTI'] / df['GOLD']).dropna()
                if len(cg) >= _min_cg_len:
                    copper_gold = round(float(cg.iloc[-1] / cg.iloc[-21] - 1), 4)

            result = {
                'correlations': correlations,
                'current_corr': current_corr,
                'risk_mode': risk_mode,
                'copper_gold_momentum': copper_gold,
                'timestamp': datetime.now().isoformat(),
            }
            self._data['global_corr'] = result
            logger.info(f"  ✅ 글로벌 상관관계: {risk_mode}, SP500={sp_corr:.2f}, DXY={dxy_corr:.2f}, Cu/Au={copper_gold}")
            return result
        except Exception as e:
            logger.warning(f"  글로벌 상관관계 수집 실패: {e}", exc_info=True)
            return {'status': 'error', 'error': str(e)}

    def collect_economic_surprise(self) -> Dict:
        """경제 서프라이즈 인덱스 (실제값 vs 예상값)"""
        try:
            macro_files = {'CPI': _PROJECT_ROOT / 'data' / 'raw' / 'economic_indicators' / 'USA_CPI.csv', 'GDP': _PROJECT_ROOT / 'data' / 'raw' / 'economic_indicators' / 'USA_GDP.csv', 'KOR_CPI': _PROJECT_ROOT / 'data' / 'raw' / 'korea_economic' / 'KOR_CPI.csv', 'KOR_GDP': _PROJECT_ROOT / 'data' / 'raw' / 'economic_indicators' / 'KOR_GDP.csv'}
            surprises = {}
            surprise_scores = []
            _min_macro_rows, _min_std_floor, _esi_pos, _esi_neg = 12, 0.001, 0.5, -0.5
            for name, path in macro_files.items():
                if path.exists():
                    try:
                        df = pd.read_csv(path, index_col=0, parse_dates=True)
                        col = df.columns[0] if len(df.columns) > 0 else None
                        if col and len(df) >= _min_macro_rows:
                            values = df[col].dropna().tail(24)
                            if len(values) >= _min_macro_rows:
                                rolling_mean = values.rolling(6).mean()
                                rolling_std = values.rolling(12).std()
                                if len(rolling_mean.dropna()) > 0 and len(rolling_std.dropna()) > 0:
                                    latest = float(values.iloc[-1])
                                    expected = float(rolling_mean.iloc[-1])
                                    std = max(float(rolling_std.iloc[-1]), _min_std_floor)
                                    z = (latest - expected) / std
                                    surprises[name] = {'latest': round(latest, 3), 'expected': round(expected, 3), 'z_score': round(z, 2)}
                                    surprise_scores.append(z)
                    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
                        import logging
                        logging.getLogger(__name__).debug(f"Targeted fallback: {_e}")
                        continue
            if surprise_scores:
                esi = float(np.mean(surprise_scores))
                result = {'economic_surprise_index': round(esi, 3), 'indicators': surprises, 'interpretation': 'positive_surprise' if esi > _esi_pos else 'negative_surprise' if esi < _esi_neg else 'inline', 'timestamp': datetime.now().isoformat()}
                self._data['eco_surprise'] = result
                _interp = result.get('interpretation', '')
                logger.info(f"  ✅ ESI: {esi:+.3f} ({_interp}), {len(surprises)}개 지표")
                return result
        except Exception as e:
            logger.warning(f"  ESI 수집 실패: {e}", exc_info=True)
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
        _ss_score = sentiment_score.get('score', 0)
        _ss_label = sentiment_score.get('label', '')
        logger.info(f"  📊 종합 센티먼트: {_ss_score:.1f}/100 ({_ss_label})")
        return result

    def _calculate_composite_sentiment(self) -> Dict:
        """종합 시장 센티먼트 (0=극공포, 100=극탐욕)"""
        scores = []
        vkospi_data = self._data.get('vkospi', {})
        vkospi = vkospi_data.get('vkospi') or 20
        vkospi_score = max(0, min(100, 100 - (vkospi - 10) * 3.3))
        scores.append(('VKOSPI', vkospi_score, 0.35))
        pc_data = self._data.get('put_call', {})
        pc = pc_data.get('put_call_ratio', 1.0)
        pc_score = max(0, min(100, 100 - (pc - 0.6) * 100))
        scores.append(('P/C_Ratio', pc_score, 0.25))
        corr_data = self._data.get('global_corr', {})
        risk_mode = corr_data.get('risk_mode', 'neutral')
        risk_score = {'risk_on': 75, 'neutral': 50, 'risk_off': 25}.get(risk_mode) or 50
        scores.append(('Risk_Mode', risk_score, 0.2))
        esi_data = self._data.get('eco_surprise', {})
        esi = esi_data.get('economic_surprise_index', 0)
        esi_score = max(0, min(100, 50 + esi * 25))
        scores.append(('ESI', esi_score, 0.2))
        total = sum((s * w for _, s, w in scores))
        total_weight = sum((w for _, _, w in scores))
        composite = total / total_weight if total_weight > 0 else 50
        _t_eg, _t_g, _t_n, _t_f = 75, 60, 40, 25
        if composite >= _t_eg:
            label = 'extreme_greed'
        elif composite >= _t_g:
            label = 'greed'
        elif composite >= _t_n:
            label = 'neutral'
        elif composite >= _t_f:
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
            logger.warning(f"  suppressed: {_e}", exc_info=True)
        return {}

    def _save_cache(self):
        try:
            self._data['timestamp'] = datetime.now().isoformat()
            atomic_write_json(self.cache_file, self._data, indent=2, default=str, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"캐시 저장 실패: {e}", exc_info=True)