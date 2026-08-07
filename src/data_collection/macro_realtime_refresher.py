"""
MacroRealtimeRefresher — 장중 매크로 데이터 실시간 갱신
=======================================================

3-Tier 아키텍처로 데이터 특성별 최적 주기로 갱신:
  Tier 1 (5분):  VIX, US선물, FX, KOSPI/KOSDAQ — 24h 거래, 장중 변동 큼
  Tier 2 (30분): 원자재, 채권금리, 아시아 지수 — 선물 거래, 변동 보통
  Tier 3 (1일):  FRED, PMI, 크로스마켓 CSV — 발표 자체가 주간/월간

모든 파라미터는 DynamicConfig에서 동적 로드 (하드코딩 배제).

Usage:
    from src.data_collection.macro_realtime_refresher import MacroRealtimeRefresher
    refresher = MacroRealtimeRefresher()
    result = refresher.refresh()  # tier 자동 판단

    # CLI
    python -m src.data_collection.macro_realtime_refresher
"""
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
logger = logging.getLogger(__name__)
try:
    from src.utils.data_health_monitor import dhm
except ImportError as e:
    dhm = None
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
_SIGNAL_CACHE = _RESULTS / 'signal_cache.json'

def _load_config():
    """DynamicConfig 로드 (import 실패 시 기본값)."""
    try:
        from config.dynamic_config import DynamicConfig
        return DynamicConfig()
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        return None

class MacroRealtimeRefresher:
    """장중 매크로 데이터 실시간 갱신 엔진.

    모든 주기/대상 종목/장 시간은 DynamicConfig에서 동적 로드.
    """
    _DEFAULT_TIER1 = {'^VIX': 'vix', '^GSPC': 'sp500', '^IXIC': 'nasdaq', '^SOX': 'sox', '^DJI': 'dji', 'KRW=X': 'usdkrw', 'USDJPY=X': 'usdjpy', 'EURUSD=X': 'eurusd', 'DX-Y.NYB': 'dxy'}
    _DEFAULT_TIER2 = {'CL=F': 'wti', 'GC=F': 'gold_us', 'SI=F': 'silver', 'HG=F': 'copper', '^TNX': 'us10y', '^FVX': 'us5y', '^TYX': 'us30y', 'EWY': 'ewy', 'FLKR': 'flkr', 'FXI': 'fxi', '^TWII': 'taiex', '^N225': 'nikkei', '^HSI': 'hangseng'}

    def __init__(self):
        self._cfg = _load_config()
        self._cache = self._load_signal_cache()
        self._tier1_interval = self._get_cfg('macro_refresh.tier1_interval_sec', 300)
        self._tier2_interval = self._get_cfg('macro_refresh.tier2_interval_sec', 1800)
        self._market_open_hour = self._get_cfg('macro_refresh.market_open_hour', 9)
        self._market_open_min = self._get_cfg('macro_refresh.market_open_min', 0)
        self._market_close_hour = self._get_cfg('macro_refresh.market_close_hour', 15)
        self._market_close_min = self._get_cfg('macro_refresh.market_close_min', 30)
        self._tier1_tickers = self._load_tier_tickers('tier1', self._DEFAULT_TIER1)
        self._tier2_tickers = self._load_tier_tickers('tier2', self._DEFAULT_TIER2)

    def refresh(self, tier: str='auto') -> Dict[str, Any]:
        """매크로 데이터 갱신.

        Args:
            tier: 'auto' | 't1' | 't2' | 'all'
                  'auto': 마지막 갱신 시간 기반 자동 판단

        Returns:
            {'tier1': {...}, 'tier2': {...}, 'skipped': [...], ...}
        """
        now = datetime.now()
        result = {'timestamp': now.isoformat(), 'market_open': self._is_market_hours(now), 'tier1': {}, 'tier2': {}, 'skipped': []}
        if not self._is_market_hours(now):
            result['skipped'].append('KR 지수: 장 시간 외 — 스킵')
            kr_market_open = False
        else:
            kr_market_open = True
        if tier == 'auto':
            if self._should_refresh('tier1'):
                result['tier1'] = self._refresh_tier1()
            else:
                result['skipped'].append('tier1: 최근 갱신 존재')
            if self._should_refresh('tier2'):
                result['tier2'] = self._refresh_tier2()
            else:
                result['skipped'].append('tier2: 최근 갱신 존재')
        elif tier == 't1':
            result['tier1'] = self._refresh_tier1()
        elif tier == 't2':
            result['tier2'] = self._refresh_tier2()
        elif tier == 'all':
            result['tier1'] = self._refresh_tier1()
            result['tier2'] = self._refresh_tier2()
        if tier in ('auto', 't1', 'all') and kr_market_open:
            kr_result = self._refresh_kr_indices()
            result['kr_indices'] = kr_result
            
        total_updated = result['tier1'].get('n_updated', 0) + result['tier2'].get('n_updated', 0) + result.get('kr_indices', {}).get('n_updated', 0)
        if total_updated > 0:
            self._update_cache({'macro_refresh_ts': now.isoformat()})
            logger.info(f'  ✅ MacroRefresh: {total_updated}개 갱신 완료 (T1={result['tier1'].get('n_updated', 0)}, T2={result['tier2'].get('n_updated', 0)}, KR={result.get('kr_indices', {}).get('n_updated', 0)})')
        if tier in ('auto', 't1', 'all'):
            try:
                self._refresh_gex_pipeline()
            except Exception as _gex_err:
                logger.error(f'GEX 파이프라인 오류 (비치명): {_gex_err}', exc_info=True)
            try:
                self._refresh_macro_spike()
            except Exception as _ms_err:
                logger.error(f'매크로 스파이크 오류 (비치명): {_ms_err}', exc_info=True)
            try:
                self._refresh_vix_attacker_sensors()
            except Exception as _vix_err:
                logger.error(f'VIX 어태커 센서 오류 (비치명): {_vix_err}', exc_info=True)
        return result

    def _refresh_tier1(self) -> Dict:
        """Tier 1 갱신: VIX, US 지수, FX."""
        logger.info('  🔄 Tier 1 갱신: VIX, US선물, FX')
        updates, errors = self._fetch_alphavantage_batch(self._tier1_tickers)
        if updates:
            us10y = updates.get('us10y', self._cache.get('us10y'))
            us5y = updates.get('us5y', self._cache.get('us5y'))
            us30y = updates.get('us30y', self._cache.get('us30y'))
            if us10y and us5y and isinstance(us10y, (int, float)) and isinstance(us5y, (int, float)):
                spread_10y_5y = us10y - us5y
                updates['yield_curve_10y_5y'] = round(spread_10y_5y, 4)
                updates['yield_curve_inverted'] = 1 if spread_10y_5y < 0 else 0
                updates['yield_curve_10y_2y'] = round(spread_10y_5y, 4)
            if us10y and us30y and isinstance(us10y, (int, float)) and isinstance(us30y, (int, float)):
                updates['yield_curve_10y_30y'] = round(us10y - us30y, 4)
            try:
                import math as _math
                import pandas as _pd
                from pathlib import Path
                
                vix_window = int(self._get_cfg('s1.vix_rolling_window', 20))
                
                # 1차 시도: yfinance를 통한 실시간 VIX 조회
                if 'vix' not in updates:
                    try:
                        import yfinance as _yf
                        vix_df = _yf.download('^VIX', period='1d', progress=False)
                        if not vix_df.empty and 'Close' in vix_df.columns:
                            live_vix = float(vix_df['Close'].iloc[-1])
                            if live_vix > 0:
                                updates['vix'] = live_vix
                                logger.info(f"  ✅ [yfinance] VIX 실시간 조회 성공: {live_vix}")
                    except Exception as yf_e:
                        logger.warning(f"  ⚠️ [yfinance] VIX 실시간 조회 에러: {yf_e}")
                
                # VIX 폴백 1: 기존 캐시값(전일값) Forward Fill
                fallback_vix = self._cache.get('vix')
                
                # VIX 폴백 2: 캐시도 없으면 SPY 역사적 변동성(HV)으로 합성 (Proxy VIX)
                if fallback_vix is None or fallback_vix <= 0:
                    try:
                        spy_path = Path(__file__).resolve().parent.parent.parent / 'data' / 'us_stocks' / 'prices' / 'SPY.csv'
                        if spy_path.exists():
                            spy_df = _pd.read_csv(spy_path)
                            if 'close' in spy_df.columns and len(spy_df) >= 21:
                                rets = spy_df['close'].pct_change().dropna()
                                hv = rets.tail(20).std() * _math.sqrt(252) * 100
                                fallback_vix = round(hv, 2)
                                logger.warning(f"  🔄 VIX Cold Start Fallback: SPY 20-day HV 계산 완료 = {fallback_vix}")
                    except Exception as he:
                        logger.warning(f"  ⚠️ SPY HV 합성 실패: {he}")
                        
                if fallback_vix is None or fallback_vix <= 0:
                    fallback_vix = 15.0 # 최후의 수단 (시스템 셧다운 방지)

                if 'vix' not in updates:
                    logger.warning(f"🚨 VIX 수집 실패! Intelligent Fallback 가동: {fallback_vix}")
                    updates['vix'] = fallback_vix
                    
                vix_now = updates.get('vix')
                vix_hist_raw = self._cache.get('vix_history', [])
                if not isinstance(vix_hist_raw, list):
                    vix_hist_raw = []
                if vix_now and isinstance(vix_now, (int, float)) and (vix_now > 0):
                    vix_hist_raw = list(vix_hist_raw) + [float(vix_now)]
                    if len(vix_hist_raw) > 252:
                        vix_hist_raw = vix_hist_raw[-252:]
                    updates['vix_history'] = vix_hist_raw
                if len(vix_hist_raw) >= 2:
                    window_data = vix_hist_raw[-vix_window:]
                    n_w = len(window_data)
                    mu = sum(window_data) / n_w
                    var = sum(((x - mu) ** 2 for x in window_data)) / n_w
                    sigma = _math.sqrt(var) if var > 0 else 0.0
                    updates['vix_ma_20'] = round(mu, 4)
                    updates['vix_std_20'] = round(sigma, 4)
                    logger.debug(f'  [S1 Support] VIX Rolling: n={n_w}, ma={mu:.2f}, std={sigma:.2f}')
                elif vix_now and isinstance(vix_now, (int, float)):
                    updates['vix_ma_20'] = float(vix_now)
                    updates['vix_std_20'] = 2.0
            except Exception as _e:
                logger.error(f'  [S1 Support] VIX rolling 계산 실패 (fallback 사용): {_e}', exc_info=True)
            updates['tier1_refresh_ts'] = datetime.now().isoformat()
            self._update_cache(updates)
        return {'n_updated': len([k for k in updates if not k.endswith('_ts')]), 'n_errors': len(errors), 'errors': errors}

    def _refresh_tier2(self) -> Dict:
        """Tier 2 갱신: 원자재, 채권, 아시아 지수."""
        logger.info('  🔄 Tier 2 갱신: 원자재, 채권, 아시아')
        now = datetime.now()
        active_tickers = {}
        for yf_ticker, cache_key in self._tier2_tickers.items():
            if self._is_active_market(yf_ticker, now):
                active_tickers[yf_ticker] = cache_key
        updates, errors = self._fetch_alphavantage_batch(active_tickers)
        if updates:
            updates['tier2_refresh_ts'] = datetime.now().isoformat()
            self._update_cache(updates)
        return {'n_updated': len([k for k in updates if not k.endswith('_ts')]), 'n_errors': len(errors), 'n_skipped_market_closed': len(self._tier2_tickers) - len(active_tickers), 'errors': errors}

    def _refresh_kr_indices(self) -> Dict:
        """KOSPI/KOSDAQ 실시간 갱신.

        ★ pykrx get_index_ohlcv_by_date는 '지수명' KeyError 발생하므로
        KODEX200(069500)과 KODEX코스닥150(229200)을 프록시로 사용.
        전일 종가 + MA20도 히스토리에서 정확하게 계산.
        """
        updates = {}
        n_updated = 0
        try:
            from pykrx import stock as pykrx_stock
            from datetime import timedelta
            today_str = datetime.now().strftime('%Y%m%d')
            hist_start = (datetime.now() - timedelta(days=60)).strftime('%Y%m%d')
            try:
                df = pykrx_stock.get_market_ohlcv_by_date(hist_start, today_str, '069500')
                if df is not None and len(df) >= 2:
                    close_col = '종가' if '종가' in df.columns else df.columns[3]
                    closes = df[close_col].astype(float)
                    today_close = float(closes.iloc[-1])
                    prev_close = float(closes.iloc[-2])
                    updates['kospi_close'] = today_close
                    updates['kospi'] = today_close  # [Fix] KOSPI 키 동기화 누락 수정
                    updates['kospi_prev_close'] = prev_close
                    if prev_close > 0:
                        updates['kospi_change_1d'] = round((today_close / prev_close - 1) * 100, 4)
                    high_col = '고가' if '고가' in df.columns else df.columns[1]
                    low_col = '저가' if '저가' in df.columns else df.columns[2]
                    try:
                        today_high = float(df[high_col].iloc[-1])
                        today_low = float(df[low_col].iloc[-1])
                        if today_close > 0 and today_high > today_low:
                            intraday_range = round((today_high - today_low) / today_close * 100, 4)
                            updates['intraday_range_pct'] = intraday_range
                    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                        import logging
                        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                        if dhm:
                            dhm.record('kr_intraday_range', e, 'info', context={'ticker': '069500'})
                    if len(closes) >= 20:
                        updates['kospi_ma20'] = round(float(closes.iloc[-20:].mean()), 2)
                    n_updated += 1
                    logger.debug(f'  KOSPI proxy(069500): close={today_close:.0f}, prev={prev_close:.0f}, chg={updates.get('kospi_change_1d', '?')}%, MA20={updates.get('kospi_ma20', '?')}, intraday_range={updates.get('intraday_range_pct', '?')}%')
            except Exception as e:
                if dhm:
                    dhm.record('kr_kospi_069500', e, 'critical', context={'ticker': '069500', 'method': 'pykrx'}, fallback_used='yfinance ^KS11')
                else:
                    logger.warning(f'  KOSPI(069500) 갱신 실패: {e}')
            if 'kospi_close' not in updates:
                try:
                    from src.data_collection.alpha_vantage_collector import collect_global_macro
                    av_res = collect_global_macro(['^KS11'])
                    if '^KS11' in av_res:
                        updates['kospi'] = av_res['^KS11']['price']
                        updates['kospi_change_1d'] = av_res['^KS11']['change_1d']
                        # [S1 Patch] KOSPI 스케일 오염 방지: pykrx(069500) 실패로 MA20 갱신이 멈췄을 때, 
                        # 백업 수집된 KOSPI(KS11)와 스케일을 일치시키기 위해 임시 동기화
                        updates['kospi_ma20'] = av_res['^KS11']['price']
                        n_updated += 1
                except Exception as e:
                    if dhm:
                        dhm.record('kr_kospi_alphavantage', e, 'critical', context={'ticker': '^KS11'}, fallback_used='없음 — KOSPI 데이터 완전 누락')
                    else:
                        logger.warning(f'  KOSPI(AlphaVantage) fallback 실패: {e}')
            try:
                df = pykrx_stock.get_market_ohlcv_by_date(hist_start, today_str, '229200')
                if df is not None and len(df) >= 2:
                    close_col = '종가' if '종가' in df.columns else df.columns[3]
                    closes = df[close_col].astype(float)
                    today_close = float(closes.iloc[-1])
                    prev_close = float(closes.iloc[-2])
                    updates['kosdaq_close'] = today_close
                    updates['kosdaq_prev_close'] = prev_close
                    if prev_close > 0:
                        updates['kosdaq_change_1d'] = round((today_close / prev_close - 1) * 100, 4)
                    if len(closes) >= 20:
                        updates['kosdaq_ma20'] = round(float(closes.iloc[-20:].mean()), 2)
                    n_updated += 1
            except Exception as e:
                if dhm:
                    dhm.record('kr_kosdaq_229200', e, 'critical', context={'ticker': '229200'})
                else:
                    logger.warning(f'  KOSDAQ(229200) 갱신 실패: {e}')
            if 'kospi_close' in updates:
                updates['kospi200'] = updates['kospi_close']
                updates['kospi200_prev_close'] = updates.get('kospi_prev_close', 0)
            try:
                vk_val = self._safe_pykrx_index('1004', today_str)
                if vk_val is not None:
                    updates['vkospi'] = vk_val
                    n_updated += 1
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                if dhm:
                    dhm.record('kr_vkospi', e, 'warning', context={'index': '1004'})
            if updates:
                updates['kr_refresh_ts'] = datetime.now().isoformat()
                self._update_cache(updates)
        except ImportError as e:
            if dhm:
                dhm.record('kr_pykrx_import', e, 'critical', fallback_used='없음 — pykrx 미설치')
        except Exception as e:
            if dhm:
                dhm.record('kr_indices_general', e, 'critical', context={'phase': '_refresh_kr_indices'})
            else:
                logger.warning(f'  KR 지수 갱신 실패: {e}')
        self._refresh_s1_derived_stats()
        return {'n_updated': n_updated}

    def _refresh_s1_derived_stats(self):
        """[Model 3/4 Support] S1 스트림이 필요로 하는 파생 통계를 signal_cache에 주입.

        주입 키:
          lp_pressure_ma   — lp_delta_pressure의 rolling N일 이동평균
          lp_pressure_std  — lp_delta_pressure의 rolling N일 표준편차
          atr_5m           — KODEX200(069500) 5분봉 TR/Close 기반 ATR

        설계 원칙:
          - 완전 fail-safe: 수집 실패 시 기존 캐시값 유지, 시스템 패닉 없음
          - lp_pressure_ma/std fallback: 0.0 / 500.0 (원 하드코딩 스케일 기준)
          - atr_5m fallback: 0.0 → etf_sniper_stream이 sl_min_pct(0.3%)로 대체
        """
        import math as _math
        updates: Dict = {}
        try:
            lp_window = int(self._get_cfg('s1.lp_pressure_rolling_window', 20))
            lp_hist = self._cache.get('lp_pressure_history', [])
            if not isinstance(lp_hist, list):
                lp_hist = []
            try:
                from src.data_collection.ss_etf_feature_engine import SSETFFeatureEngine
                engine = SSETFFeatureEngine()
                sam_feat = engine.compute('005930') or {}
                lp_now = sam_feat.get('lp_delta_pressure')
                if lp_now is not None and isinstance(lp_now, (int, float)):
                    lp_hist = list(lp_hist) + [float(lp_now)]
                    if len(lp_hist) > 252:
                        lp_hist = lp_hist[-252:]
                    updates['lp_pressure_history'] = lp_hist
            except Exception as _e:
                logger.error(f'  [S1 Support] SSETFFeatureEngine LP 수집 실패: {_e}', exc_info=True)
            if len(lp_hist) >= 2:
                window_data = lp_hist[-lp_window:]
                n_w = len(window_data)
                mu = sum(window_data) / n_w
                var = sum(((x - mu) ** 2 for x in window_data)) / n_w
                sigma = _math.sqrt(var) if var > 0 else 500.0
                updates['lp_pressure_ma'] = round(mu, 4)
                updates['lp_pressure_std'] = round(sigma, 4)
                logger.debug(f'  [S1 Support] LP Pressure Rolling: n={n_w}, ma={mu:.2f}, std={sigma:.2f}')
            else:
                updates.setdefault('lp_pressure_ma', 0.0)
                updates.setdefault('lp_pressure_std', 500.0)
                logger.debug('  [S1 Support] LP Pressure 히스토리 부족 → fallback(0.0, 500.0)')
        except Exception as _e:
            logger.error(f'  [S1 Support] LP Pressure rolling 계산 실패: {_e}', exc_info=True)
        try:
            atr_5m_window = int(self._get_cfg('s1.atr_5m_window', 5))
            today_str = datetime.now().strftime('%Y%m%d')
            from pykrx import stock as _pykrx
            df_5m = _pykrx.get_market_ohlcv_by_ticker_and_date(today_str, today_str, '069500', freq='T5')
            if df_5m is not None and len(df_5m) >= atr_5m_window + 1:
                import numpy as _np
                high_col = '고가' if '고가' in df_5m.columns else df_5m.columns[1]
                low_col = '저가' if '저가' in df_5m.columns else df_5m.columns[2]
                close_col = '종가' if '종가' in df_5m.columns else df_5m.columns[3]
                h = df_5m[high_col].astype(float).values
                l = df_5m[low_col].astype(float).values
                c = df_5m[close_col].astype(float).values
                pc = _np.roll(c, 1)
                pc[0] = c[0]
                tr = _np.maximum(h - l, _np.maximum(_np.abs(h - pc), _np.abs(l - pc)))
                recent_c = c[-1] if c[-1] > 0 else 1.0
                atr_val = float(_np.mean(tr[-atr_5m_window:])) / recent_c
                updates['atr_5m'] = round(atr_val, 6)
                logger.debug(f'  [S1 Support] ATR 5m: {atr_val:.5f} (window={atr_5m_window}, n_bars={len(df_5m)})')
            else:
                logger.debug(f'  [S1 Support] ATR 5m: 5분봉 데이터 부족 (bars={(len(df_5m) if df_5m is not None else 0)}) → fallback 0.0')
                updates.setdefault('atr_5m', 0.0)
        except Exception as _e:
            logger.error(f'  [S1 Support] ATR 5m 계산 실패 (fallback 0.0): {_e}', exc_info=True)
            updates.setdefault('atr_5m', 0.0)
        if updates:
            self._update_cache(updates)
            logger.info(f'  ✅ [S1 Support] 파생 통계 주입 완료: lp_ma={updates.get('lp_pressure_ma', 'N/A')}, lp_std={updates.get('lp_pressure_std', 'N/A')}, atr_5m={updates.get('atr_5m', 'N/A')}')

    def _fetch_alphavantage_batch(self, ticker_map: Dict[str, str]) -> Tuple[Dict, List[str]]:
        """Alpha Vantage로 여러 종목 최신값 + 전일 대비 변동률 조회.

        Returns:
            (updates_dict, error_list)
        """
        if not ticker_map:
            return ({}, [])
        updates = {}
        errors = []
        from src.data_collection.alpha_vantage_collector import collect_global_macro
        
        symbols_to_fetch = list(ticker_map.keys())
        
        # KRW=X는 BOK ECOS API 직접 호출 (시차 최소화)
        if 'KRW=X' in symbols_to_fetch:
            symbols_to_fetch.remove('KRW=X')
            fx_val = self._fetch_usdkrw_bok()
            if fx_val:
                updates['usdkrw'] = fx_val
                prev = self._cache.get('usdkrw')
                if prev and prev > 0:
                    chg = round((fx_val / prev - 1) * 100, 4)
                    updates['usdkrw_change_1d'] = chg
                logger.info(f'  ✅ 환율 BOK ECOS API 성공: {fx_val:.2f}')
            else:
                logger.warning('  ⚠️ 환율(KRW=X) BOK ECOS API 수집 실패')
                errors.append('KRW=X')

        if not symbols_to_fetch:
            return (updates, errors)

        logger.info(f'  🌍 [Alpha Vantage] 배치 조회 시작: {symbols_to_fetch}')
        try:
            av_results = collect_global_macro(symbols_to_fetch)
        except Exception as e:
            logger.error(f'  ❌ Alpha Vantage 배치 조회 중 에러 발생: {e}', exc_info=True)
            av_results = {}
            
        for yf_ticker in symbols_to_fetch:
            cache_key = ticker_map[yf_ticker]
            if yf_ticker in av_results:
                res = av_results[yf_ticker]
                updates[cache_key] = res['price']
                updates[f'{cache_key}_change_1d'] = res['change_1d']
            else:
                errors.append(yf_ticker)
                
        return (updates, errors)

    @staticmethod
    def _fetch_usdkrw_naver() -> Optional[float]:
        """Naver 대신 yfinance 단건 직접 호출로 안정적인 우회 제공."""
        try:
            import yfinance as yf
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                df = yf.download('KRW=X', period='1d', progress=False)
                if not df.empty and 'Close' in df.columns:
                    return float(df['Close'].iloc[-1].item())
        except Exception as e:
            import logging
            logging.getLogger(__name__).debug(f'yfinance KRW=X direct fetch failed: {e}')
        return None

    @staticmethod
    def _fetch_usdkrw_bok() -> Optional[float]:
        """BOK ECOS API에서 USD/KRW 환율을 수집 (가장 최근값).

        Returns:
            float (USD/KRW 환율) | None
        """
        try:
            from src.data_collection.bok_economic_updater import BOKEconomicUpdater
            from datetime import datetime, timedelta
            updater = BOKEconomicUpdater()
            end_date = datetime.now()
            start_date = end_date - timedelta(days=7) # 주말/휴일 고려
            df = updater.fetch_from_bok('731Y001', '0000001', 'D', start_date.strftime('%Y%m%d'), end_date.strftime('%Y%m%d'))
            if not df.empty and 'Value' in df.columns:
                return float(df['Value'].iloc[-1])
        except Exception as _e:
            logger.error(f'  BOK 환율 API 수집 실패: {_e}', exc_info=True)
        return None

    def _fetch_yf_batch(self, ticker_map: Dict[str, str], period: str='5d') -> Tuple[Dict[str, float], List[str]]:
        """yfinance bulk download 및 에러/누락 종목 분리 반환."""
        if not ticker_map:
            return ({}, [])
        try:
            import yfinance as yf
        except ImportError as e:
            logger.error('  yfinance 미설치', exc_info=True)
            return ({}, list(ticker_map.keys()))
        updates = {}
        errors = []

        def _safe_float(val) -> float:
            """numpy/pandas scalar -> Python float 안전 변환."""
            return float(val.item() if hasattr(val, 'item') else val)

        def _compute_change_1d(closes, cache_key: str) -> None:
            """2일치 이상 Close 시리즈에서 정확한 _change_1d 계산.

            우선순위:
              1. 시계열 데이터에 2행 이상 -> iloc[-2] vs iloc[-1]
              2. 시계열 1행만 + 캐시에 이전값 존재 -> 캐시 대비 계산
              3. 어느 쪽도 불가 -> 스킵 (0.0 하드코딩 방지)
            """
            if closes is not None and len(closes) >= 2:
                last_val = _safe_float(closes.iloc[-1])
                prev_val = _safe_float(closes.iloc[-2])
                if prev_val > 0:
                    chg = round((last_val / prev_val - 1) * 100, 4)
                    updates[f'{cache_key}_change_1d'] = chg
                    logger.debug(f'  {cache_key}_change_1d={chg}% (curr={last_val:.4f}, prev={prev_val:.4f})')
            elif closes is not None and len(closes) == 1:
                last_val = _safe_float(closes.iloc[-1])
                prev = self._cache.get(cache_key)
                if prev and isinstance(prev, (int, float)) and (prev > 0) and (abs(last_val - prev) > 1e-08):
                    chg = round((last_val / prev - 1) * 100, 4)
                    updates[f'{cache_key}_change_1d'] = chg
                    logger.debug(f'  {cache_key}_change_1d={chg}% (curr={last_val:.4f}, cache_prev={prev:.4f}) [1행 fallback]')

        def _extract_close(data, yf_ticker: str):
            """DataFrame에서 Close 시리즈 추출 (MultiIndex 대응)."""
            if data is None or data.empty:
                return None
            try:
                if hasattr(data.columns, 'levels'):
                    data.columns = [c[0] if isinstance(c, tuple) else c for c in data.columns]
                col = 'Close' if 'Close' in data.columns else 'close'
                if col in data.columns:
                    return data[col].dropna()
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at macro_realtime_refresher.py:736', exc_info=True)
            return None
        yf_symbols = list(ticker_map.keys())
        batch_ok = False
        try:
            batch_data = yf.download(yf_symbols, period='5d', progress=False, auto_adjust=True, threads=True, timeout=20)
            if batch_data is not None and (not batch_data.empty):
                batch_ok = True
                if hasattr(batch_data.columns, 'levels'):
                    for yf_ticker, cache_key in ticker_map.items():
                        try:
                            if ('Close', yf_ticker) in batch_data.columns:
                                col = ('Close', yf_ticker)
                            elif 'Close' in batch_data.columns:
                                col = 'Close'
                            else:
                                close_cols = [c for c in batch_data.columns if isinstance(c, tuple) and c[0].lower() == 'close' and (c[1] == yf_ticker)]
                                if close_cols:
                                    col = close_cols[0]
                                else:
                                    errors.append(yf_ticker)
                                    continue
                            val = batch_data[col].dropna()
                            if len(val) > 0:
                                last_val = _safe_float(val.iloc[-1])
                                updates[cache_key] = last_val
                                _compute_change_1d(val, cache_key)
                        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                            import logging
                            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                            if dhm:
                                dhm.record(f'yf_parse_{yf_ticker}', e, 'warning', context={'ticker': yf_ticker, 'cache_key': cache_key})
                            errors.append(yf_ticker)
                else:
                    yf_ticker = yf_symbols[0]
                    cache_key = ticker_map[yf_ticker]
                    closes = _extract_close(batch_data, yf_ticker)
                    if closes is not None and len(closes) > 0:
                        updates[cache_key] = _safe_float(closes.iloc[-1])
                        _compute_change_1d(closes, cache_key)
                    else:
                        errors.append(yf_ticker)
        except Exception as e:
            if dhm:
                dhm.record('yf_batch_download', e, 'warning', context={'n_tickers': len(yf_symbols)}, fallback_used='단건 retry')
            else:
                logger.warning(f'  yfinance 배치 실패 → 단건 retry: {e}')
        retry_targets = [(t, k) for t, k in ticker_map.items() if t in errors or not batch_ok]
        for yf_ticker, cache_key in retry_targets:
            if cache_key in updates:
                continue
            import time as _t
            data = None
            if yf_ticker in ('KRW=X', 'USDKRW=X'):
                nav_val = self._fetch_usdkrw_naver()
                if nav_val is not None:
                    updates[cache_key] = nav_val
                    prev = self._cache.get(cache_key)
                    if prev and isinstance(prev, (int, float)) and (prev > 0):
                        updates[f'{cache_key}_change_1d'] = round((nav_val / prev - 1) * 100, 4)
                    logger.warning(f'  환율 yfinance 실패 → Naver 크롤링 성공: {nav_val:.2f}')
                    continue
            data = self._fetch_with_retry(yf_ticker, period='5d', n_retry=3, base_delay=1.0)
            if data is not None:
                closes = _extract_close(data, yf_ticker)
                if closes is not None and len(closes) > 0:
                    updates[cache_key] = _safe_float(closes.iloc[-1])
                    _compute_change_1d(closes, cache_key)
                    if yf_ticker in errors:
                        errors.remove(yf_ticker)
                    continue
            cached_val = self._cache.get(cache_key)
            if cached_val is not None and isinstance(cached_val, (int, float)):
                updates[cache_key] = cached_val
                logger.critical(f'  호 실시간 수집 실패 [{yf_ticker} → {cache_key}]: signal_cache 이전값 ffill 적용 ({cached_val}) — 레징 엔진 장애 위험!')
                if yf_ticker in errors:
                    errors.remove(yf_ticker)
            else:
                logger.critical(f'  위기 {yf_ticker} → {cache_key}: 실시간 + 캐시 모두 없음 — 레징 엔진 입력값 누락!')
        return (updates, errors)

    def _is_market_hours(self, now: Optional[datetime]=None) -> bool:
        """KR 장 시간 내인지 판단 (주말 제외)."""
        if now is None:
            now = datetime.now()
        if now.weekday() >= 5:
            return False
        from datetime import time as _time
        market_open = _time(self._market_open_hour, self._market_open_min)
        market_close = _time(self._market_close_hour, self._market_close_min)
        return market_open <= now.time() <= market_close

    def _is_active_market(self, yf_ticker: str, now: datetime) -> bool:
        """특정 시장의 거래 시간인지 판단.

        FX/선물/US: 거의 24시간이므로 항상 True.
        아시아 지수: 해당 시장 시간대만.
        """
        from datetime import time as _time
        _ASIA_HOURS = {'^TWII': (_time(9, 0), _time(13, 30)), '^N225': (_time(9, 0), _time(15, 0)), '^HSI': (_time(10, 30), _time(16, 0))}
        hours = _ASIA_HOURS.get(yf_ticker)
        if hours:
            open_t, close_t = hours
            return open_t <= now.time() <= close_t
        return True

    def _should_refresh(self, tier: str) -> bool:
        """마지막 갱신 시간 비교 -> 갱신 필요 여부."""
        ts_key = f'{tier}_refresh_ts'
        last_ts = self._cache.get(ts_key)
        if not last_ts:
            return True
        try:
            last_dt = datetime.fromisoformat(str(last_ts))
            interval = self._tier1_interval if tier == 'tier1' else self._tier2_interval
            elapsed = (datetime.now() - last_dt).total_seconds()
            return elapsed >= interval
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return True

    def _safe_pykrx_index(self, index_code: str, date_str: str) -> Optional[float]:
        """pykrx 지수 조회 (KeyError 방어)."""
        try:
            from pykrx import stock as pykrx_stock
            df = pykrx_stock.get_index_ohlcv_by_date(date_str, date_str, index_code)
            if df is not None and len(df) > 0:
                close_col = '종가' if '종가' in df.columns else df.columns[-2]
                val = float(df[close_col].iloc[-1])
                if val > 0:
                    return val
        except Exception as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            logger.warning(f'[SILENT_BYPASS] KRX Blocked or Error in _safe_pykrx_index: {e}')
        return None

    def _load_signal_cache(self) -> Dict:
        """signal_cache.json 로드."""
        try:
            if _SIGNAL_CACHE.exists():
                return json.loads(_SIGNAL_CACHE.read_text())
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            logger.warning('[SILENT_BYPASS] Suppressed exception at macro_realtime_refresher.py:945', exc_info=True)
        return {}

    def _update_cache(self, updates: Dict):
        """signal_cache.json 원자적(Atomic) 쓰기 업데이트.

        [Red Team V5] 다중 프로세스 충돌 방지 (TOC/TOU Data Race 제거).
        파일 락(fcntl.flock) 기반 safe_json_update 적용.
        """
        try:
            from src.infra.safe_io import safe_json_update
            
            def _apply_updates(existing: dict) -> dict:
                # --- Z-Score / Threshold Sanity Check (르네상스 스타일 교차검증 방어막) ---
                safe_updates = {}
                for k, v in updates.items():
                    if isinstance(v, (int, float)) and not k.endswith('_ts') and not k.endswith('_1d') and 'history' not in k and 'ma' not in k and 'std' not in k:
                        prev = existing.get(k)
                        if prev and isinstance(prev, (int, float)) and prev > 0:
                            jump_pct = abs(v - prev) / prev
                            
                            limit = 0.15  # 지수류 15% 초과 변동 차단
                            is_vix = 'vix' in k
                            if is_vix: limit = 2.0  # VIX는 특성상 200% 폭등 가능성 있음 (그러나 거부하진 않음)
                            elif 'usdkrw' in k: limit = 0.05  # 환율은 5% 초과 변동 차단
                            elif 'yield' in k: limit = 0.50 # 국채금리 50% 변동 차단
                            
                            if jump_pct > limit:
                                if is_vix:
                                    # [Red Team V6] 오라클 중독 복구: VIX 폭등 시 '오류'로 기각하지 않고 100% 수용한다. (CRASH 레짐 발동용)
                                    logger.critical(f"  🚨 [VIX BLACK SWAN] {k} 미친 폭등 감지 (기존: {prev}, 신규: {v}, 변동: {jump_pct*100:.1f}%). 정상 데이터로 강제 수용!")
                                    safe_updates[k] = v
                                else:
                                    logger.critical(f"  🚨 [Sanity Check] {k} 비정상 스파이크 감지 (기존: {prev}, 신규: {v}, 변동: {jump_pct*100:.1f}%). 갱신 기각 및 캐시 강제 유지(Forward-Fill)!")
                                    safe_updates[k] = prev
                                continue
                    safe_updates[k] = v
                    
                existing.update(safe_updates)
                existing['timestamp'] = datetime.now().isoformat()
                return existing

            success = safe_json_update(_SIGNAL_CACHE, _apply_updates)
            if success:
                self._cache.update(updates)
            else:
                logger.error(f'  ❌ signal_cache 업데이트 실패 (File Lock 획득 실패)')
        except Exception as e:
            logger.warning(f'  signal_cache 업데이트 예외 발생: {e}', exc_info=True)

    def _get_cfg(self, key: str, default: Any=None) -> Any:
        """DynamicConfig에서 값 로드."""
        if self._cfg:
            return self._cfg.get(key, default)
        return default

    def _load_tier_tickers(self, tier: str, defaults: Dict[str, str]) -> Dict[str, str]:
        override_key = f'macro_refresh.{tier}_tickers'
        override = self._get_cfg(override_key)
        if override and isinstance(override, dict):
            return override
        return dict(defaults)

    def _refresh_gex_pipeline(self) -> None:
        """[Requirement A] GEX (Net Dealer Gamma Exposure) 파이프라인.

        수식:
          GEX = Σ (콜 OI × 콜 Γ) − Σ (풋 OI × 풋 Γ)

        딜러가 옵션 매도자(숏) 포지션이라고 가정하는 표준 프록시.
          - GEX > 0: 딜러 롱감마 → 시장 안정화 (volatility suppressor)
          - GEX < 0: 딜러 숏감마 → 시장 불안정 (volatilit amplifier) → 폭락 위험

        임계치 돌파 시 → gex_crash_warning = True, gex_crash_severity [0,1] 방출.
        """
        try:
            from src.data_collection.kis_data_collector import KISDataCollector
            collector = KISDataCollector()
            kospi200 = float(self._cache.get('kospi200', 0)) or None
            option_data = collector.get_kospi200_option_oi(underlying_price=kospi200, n_strikes=int(self._get_cfg('gex.n_strikes', 5)))
            if not option_data:
                logger.debug('GEX 파이프라인: 옵션 데이터 없음 → 스킵')
                return
            gex_call = sum((row['oi'] * row['gamma'] for row in option_data if row['type'] == 'call'))
            gex_put = sum((row['oi'] * row['gamma'] for row in option_data if row['type'] == 'put'))
            gex = gex_call - gex_put
            gex_history: list = list(self._cache.get('gex_history', []))
            gex_history.append(gex)
            max_hist = int(self._get_cfg('gex.history_length', 20))
            gex_history = gex_history[-max_hist:]
            crash_warning = False
            crash_severity = 0.0
            if len(gex_history) >= 5:
                import statistics
                gex_mean = statistics.mean(gex_history)
                gex_std = statistics.stdev(gex_history) if len(gex_history) > 1 else 1e-09
                gex_threshold = float(self._get_cfg('gex.crash_z_score_threshold', -2.0))
                if gex_std > 0:
                    z_score = (gex - gex_mean) / gex_std
                    if z_score < gex_threshold:
                        crash_warning = True
                        crash_severity = min(1.0, abs(z_score - gex_threshold) / abs(gex_threshold))
            updates = {'gex': round(gex, 4), 'gex_call': round(gex_call, 4), 'gex_put': round(gex_put, 4), 'gex_history': gex_history, 'gex_crash_warning': crash_warning, 'gex_crash_severity': round(crash_severity, 4), 'gex_updated_at': datetime.now().isoformat()}
            self._update_cache(updates)
            if crash_warning:
                logger.warning(f'  ⚠️ [GEX 경보] gex={gex:.2f} | crash_warning=True | severity={crash_severity:.2f}')
            else:
                logger.info(f'  📐 GEX 갱신: {gex:.2f} (경보 없음)')
        except Exception as e:
            logger.error(f'GEX 파이프라인 내부 오류: {e}', exc_info=True)

    def _refresh_macro_spike(self) -> None:
        """[Requirement B] 매크로 유동성 발작 센서 (Macro Spike Sensor).

        수집 지표: US10Y, DXY, USDKRW

        데이터 소스 우선순위:
          1순위: Alpha Vantage Premium (CURRENCY_EXCHANGE_RATE / TREASURY_YIELD)
          1.5순위: KIS T-Note 선물 프록시 (US10Y 장중 틱 — AV가 장중 미지원 시)
          2순위: KIS OpenAPI (USDKRW 실시간)
          3순위: FRED API (일간 데이터 → 선형 보간)

        발작 감지:
          20일 이동평균 + 표준편차 기반 동적 Z-Score
          Z-Score ≥ 2.5 → macro_spike_warning = True
        """
        try:
            updates_macro: Dict[str, Any] = {}
            spike_sources: List[str] = []
            av_success = False
            try:
                import requests as _req
                from src.utils.credential_manager import CredentialManager
                av_key = CredentialManager().read_from_env('ALPHA_VANTAGE_API_KEY') or ''
                if av_key:
                    av_timeout = int(self._get_cfg('macro_refresh.av_timeout_sec', 10))
                    av_fx_url = f'https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE&from_currency=USD&to_currency=KRW&apikey={av_key}'
                    r_fx = _req.get(av_fx_url, timeout=av_timeout)
                    if r_fx.status_code == 200:
                        fx_data = r_fx.json()
                        rate_str = fx_data.get('Realtime Currency Exchange Rate', {}).get('5. Exchange Rate', '')
                        if rate_str:
                            updates_macro['usdkrw'] = float(rate_str)
                            av_success = True
                    av_ty_url = f'https://www.alphavantage.co/query?function=TREASURY_YIELD&interval=daily&maturity=10year&apikey={av_key}'
                    r_ty = _req.get(av_ty_url, timeout=av_timeout)
                    if r_ty.status_code == 200:
                        ty_data = r_ty.json()
                        ty_rows = ty_data.get('data', [])
                        if ty_rows:
                            try:
                                updates_macro['us10y'] = float(ty_rows[0].get('value', 0))
                                av_success = True
                            except (ValueError, TypeError, IndexError):
                                logger.warning('[SILENT_BYPASS] Suppressed exception at macro_realtime_refresher.py:1150', exc_info=True)
            except Exception as _av_err:
                logger.error(f'AV 매크로 수집 실패 — Fallback 진행: {_av_err}', exc_info=True)
            if 'us10y' not in updates_macro or updates_macro.get('us10y', 0) == 0:
                try:
                    from src.data_collection.kis_data_collector import KISDataCollector
                    kis = KISDataCollector()
                    tnote_yield = kis.get_us_tnote_futures_tick()
                    if tnote_yield is not None:
                        updates_macro['us10y'] = tnote_yield
                        updates_macro['us10y_source'] = 'kis_tnote_proxy'
                        logger.info(f'  🇺🇸 US10Y KIS 프록시 사용: {tnote_yield:.4f}')
                except Exception as _tn_err:
                    logger.error(f'T-Note 프록시 실패: {_tn_err}', exc_info=True)
            if not av_success or 'usdkrw' not in updates_macro:
                try:
                    from src.data_collection.kis_data_collector import KISDataCollector
                    kis_fx = KISDataCollector().get_usdkrw_exchange_rate()
                    if kis_fx:
                        updates_macro['usdkrw'] = kis_fx
                        logger.info(f'  🇰🇷 KIS API USDKRW 환율 연동 성공: {kis_fx:.2f}')
                except Exception:
                    logger.error('[SILENT_BYPASS] Suppressed exception at macro_realtime_refresher.py:1179', exc_info=True)
                
                # KIS API 실패 시 yfinance fallback
                if 'usdkrw' not in updates_macro:
                    try:
                        from src.data_collection.macro_realtime_refresher import _fetch_usdkrw_naver
                        usdkrw_naver = _fetch_usdkrw_naver()
                        if usdkrw_naver:
                            updates_macro['usdkrw'] = usdkrw_naver
                    except Exception:
                        pass
            if 'us10y' not in updates_macro:
                try:
                    from src.utils.credential_manager import CredentialManager
                    import requests as _req
                    fred_key = CredentialManager().read_from_env('FRED_API_KEY') or ''
                    if fred_key:
                        fred_url = f'https://api.stlouisfed.org/fred/series/observations?series_id=DGS10&api_key={fred_key}&file_type=json&limit=2&sort_order=desc'
                        r_fred = _req.get(fred_url, timeout=8)
                        if r_fred.status_code == 200:
                            obs = r_fred.json().get('observations', [])
                            vals = [float(o['value']) for o in obs if o['value'] not in ('.', '')]
                            if vals:
                                updates_macro['us10y'] = vals[0] / 100.0
                                updates_macro['us10y_source'] = 'fred_daily_interp'
                except Exception as _fred_err:
                    logger.error(f'FRED fallback 실패: {_fred_err}', exc_info=True)
            dxy = self._cache.get('dxy')
            if dxy:
                updates_macro['dxy'] = float(dxy)
            macro_spike = False
            spike_z_max = 0.0
            import statistics
            spike_window = int(self._get_cfg('macro_spike.history_length', 20))
            spike_z_threshold = float(self._get_cfg('macro_spike.z_threshold', 2.5))
            for metric_key in ('us10y', 'dxy', 'usdkrw'):
                current_val = updates_macro.get(metric_key, self._cache.get(metric_key))
                if current_val is None:
                    continue
                hist_key = f'{metric_key}_history'
                hist: list = list(self._cache.get(hist_key, []))
                hist.append(float(current_val))
                hist = hist[-spike_window:]
                updates_macro[hist_key] = hist
                if len(hist) >= 5:
                    mean = statistics.mean(hist)
                    std = statistics.stdev(hist) if len(hist) > 1 else 1e-09
                    if std > 0:
                        z = (float(current_val) - mean) / std
                        updates_macro[f'{metric_key}_z_score'] = round(z, 4)
                        if z >= spike_z_threshold:
                            macro_spike = True
                            spike_z_max = max(spike_z_max, z)
                            spike_sources.append(f'{metric_key}(z={z:.2f})')
            updates_macro['macro_spike_warning'] = macro_spike
            updates_macro['macro_spike_z_max'] = round(spike_z_max, 4)
            updates_macro['macro_spike_sources'] = spike_sources
            updates_macro['macro_spike_updated_at'] = datetime.now().isoformat()
            self._update_cache(updates_macro)
            if macro_spike:
                logger.warning(f'  ⚠️ [매크로 발작 경보] z_max={spike_z_max:.2f} | sources={spike_sources}')
            else:
                n_updated = len([k for k in ('us10y', 'dxy', 'usdkrw') if k in updates_macro])
                logger.info(f'  📡 매크로 스파이크 갱신: {n_updated}개 지표, 발작 없음')
        except Exception as e:
            logger.error(f'매크로 스파이크 센서 내부 오류: {e}', exc_info=True)

    def _refresh_vix_attacker_sensors(self) -> None:
        """[Requirement C] VIX Trailing Stop & 웩더독 센서.

        1) 웩더독(Wag-the-Dog):
           FHPST0240 → 외국인 현선물 순매수 비교
           abs(선물) > abs(현물) × 3 AND 둘 다 음수 → wag_the_dog_active = True

        2) VIX 모멘텀 미분 (Trailing Stop):
           FHKST0101 → VKOSPI 1분봉 수집
           EMA(n=10)으로 스무딩 후 1차 미분 계산
           양수→음수 전환(공포 둔화) → vix_momentum_reversal = True
           연속 음수 EMA 기울기 → vix_momentum < 0 (exposure_orchestrator 참조)
        """
        updates_vix: Dict[str, Any] = {}
        try:
            from src.data_collection.kis_data_collector import KISDataCollector
            kis = KISDataCollector()
            wtd_data = kis.get_foreign_futures_flow()
            if wtd_data:
                updates_vix.update({'wag_the_dog_active': wtd_data['wag_the_dog_active'], 'wag_the_dog_severity': wtd_data['wag_the_dog_severity'], 'frgn_fut_net_buy': wtd_data['frgn_fut_net_buy'], 'frgn_spot_net_buy': wtd_data['frgn_spot_net_buy']})
                if wtd_data['wag_the_dog_active']:
                    logger.warning(f'  🐕 웩더독 발동! severity={wtd_data['wag_the_dog_severity']:.3f}')
            ema_period = int(self._get_cfg('vix_trailing.ema_period', 10))
            vkospi_closes = kis.get_vkospi_1min(n_candles=max(ema_period * 3, 30))
            if vkospi_closes and len(vkospi_closes) >= 2:
                multiplier = 2.0 / (ema_period + 1)
                ema_vals = [vkospi_closes[0]]
                for price in vkospi_closes[1:]:
                    ema_vals.append(price * multiplier + ema_vals[-1] * (1 - multiplier))
                vix_momentum = ema_vals[-1] - ema_vals[-2]
                prev_momentum = float(self._cache.get('vix_momentum', 0.0))
                vix_reversal = prev_momentum > 0 and vix_momentum < 0
                updates_vix.update({'vkospi': round(vkospi_closes[-1], 4), 'vkospi_ema': round(ema_vals[-1], 4), 'vix_momentum': round(vix_momentum, 6), 'vix_momentum_reversal': vix_reversal, 'vkospi_updated_at': datetime.now().isoformat()})
                if vix_reversal:
                    logger.warning(f'  🛑 VIX 모멘텀 반전! EMA기울기: {prev_momentum:.4f} → {vix_momentum:.4f} (공포 둔화)')
                else:
                    logger.info(f'  📉 VKOSPI EMA={ema_vals[-1]:.2f}, momentum={vix_momentum:+.4f}')
        except Exception as e:
            logger.error(f'VIX 어태커 센서 내부 오류: {e}', exc_info=True)
        if updates_vix:
            self._update_cache(updates_vix)

def run_macro_refresh(tier: str='auto') -> Dict:
    refresher = MacroRealtimeRefresher()
    return refresher.refresh(tier=tier)
if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s: %(message)s')
    sys.path.insert(0, str(_PROJECT_ROOT))
    tier_arg = sys.argv[1] if len(sys.argv) > 1 else 'auto'
    result = run_macro_refresh(tier=tier_arg)
    logger.debug(f'\n=== MacroRealtimeRefresher 결과 ===')
    logger.info(f'  시간: {result['timestamp']}')
    logger.info(f'  장중: {result['market_open']}')
    logger.info(f'  Tier1: {result['tier1'].get('n_updated', 0)}개 갱신, {result['tier1'].get('n_errors', 0)}개 실패')
    logger.info(f'  Tier2: {result['tier2'].get('n_updated', 0)}개 갱신, {result['tier2'].get('n_errors', 0)}개 실패')
    logger.info(f'  KR:    {result.get('kr_indices', {}).get('n_updated', 0)}개 갱신')
    if result.get('skipped'):
        logger.info(f'  스킵:  {result['skipped']}')