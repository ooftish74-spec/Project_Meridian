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
try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
try:
    from src.params.tables import load_table  # Wave2 TrackF: ticker tables live in config/params/tables
except Exception:  # pragma: no cover
    import json as _tj, pathlib as _tp
    def load_table(name, default=None):
        _r = next((d for d in _tp.Path(__file__).resolve().parents if (d / 'config' / 'params').is_dir()), None)
        try:
            return _tj.loads((_r / 'config' / 'params' / 'tables' / f'{name}.json').read_text(encoding='utf-8'))
        except Exception:
            return {} if default is None else default
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
        logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
        return None

class MacroRealtimeRefresher:
    """장중 매크로 데이터 실시간 갱신 엔진.

    모든 주기/대상 종목/장 시간은 DynamicConfig에서 동적 로드.
    """
    _DEFAULT_TIER1 = {'^VIX': 'vix', '^GSPC': 'sp500', '^IXIC': 'nasdaq', '^SOX': 'sox', '^DJI': 'dji', 'KRW=X': 'usdkrw', 'USDJPY=X': 'usdjpy', 'EURUSD=X': 'eurusd', 'DX-Y.NYB': 'dxy'}
    _DEFAULT_TIER2 = load_table('src.data_collection.macro_realtime_refresher.MacroRealtimeRefresher._DEFAULT_TIER2', {})

    def __init__(self):
        self._cfg = _load_config()
        self._cache = self._load_signal_cache()
        self._tier1_interval = pget('macro_refresh.tier1_interval_sec', 300)
        self._tier2_interval = pget('macro_refresh.tier2_interval_sec', 1800)
        self._market_open_hour = pget('macro_refresh.market_open_hour', 9)
        self._market_open_min = self._get_cfg('macro_refresh.market_open_min', 0)
        self._market_close_hour = pget('macro_refresh.market_close_hour', 15)
        self._market_close_min = pget('macro_refresh.market_close_min', 30)
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
        _need_kr_backfill = (
            not self._cache.get('kospi200')
            or not self._cache.get('kospi_close')
            or not self._cache.get('kosdaq_close')
        )
        if tier in ('auto', 't1', 'all') and (kr_market_open or tier == 'all' or _need_kr_backfill):
            kr_result = self._refresh_kr_indices()
            result['kr_indices'] = kr_result
            
        total_updated = result['tier1'].get('n_updated', 0) + result['tier2'].get('n_updated', 0) + result.get('kr_indices', {}).get('n_updated', 0)
        if total_updated > 0:
            self._update_cache({'macro_refresh_ts': now.isoformat()})
            logger.info(f"  ✅ MacroRefresh: {total_updated}개 갱신 완료 (T1={result['tier1'].get('n_updated', 0)}, T2={result['tier2'].get('n_updated', 0)}, KR={result.get('kr_indices', {}).get('n_updated', 0)})")
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
            if us10y and us30y and isinstance(us10y, (int, float)) and isinstance(us30y, (int, float)):
                updates['yield_curve_10y_30y'] = round(us10y - us30y, 4)
            try:
                import math as _math
                import pandas as _pd
                from pathlib import Path
                
                vix_window = int(pget('s1.vix_rolling_window', 20))
                
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
                    fallback_vix = 15.0

                # Google Finance 0-Lag 실시간 VIX 1순위 적용
                try:
                    from src.utils.google_finance_collector import GoogleFinanceCollector
                    gf_vix_details = GoogleFinanceCollector.get_quote_details('VIX')
                    if gf_vix_details and float(gf_vix_details.get('price', 0) or 0) > 0:
                        updates['vix'] = round(float(gf_vix_details['price']), 2)
                        if float(gf_vix_details.get('prev_close', 0) or 0) > 0:
                            updates['vix_prev'] = round(float(gf_vix_details['prev_close']), 2)
                        logger.info(f"  🟢 Live VIX real-time tick captured via Google Finance: {updates['vix']}")
                    else:
                        gf_vix = GoogleFinanceCollector.get_vix()
                        if gf_vix and gf_vix > 0:
                            updates['vix'] = gf_vix
                            logger.info(f"  🟢 Live VIX real-time tick captured via Google Finance: {updates['vix']}")
                except Exception as _gfe:
                    logger.warning(f"  ⚠️ Google Finance VIX fetch exception: {_gfe}")

                if 'vix' not in updates:
                    try:
                        import urllib.request
                        req = urllib.request.Request('https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_VIX.json', headers={'User-Agent': 'Mozilla/5.0'})
                        with urllib.request.urlopen(req, timeout=5) as resp:
                            rows_cboe = json.loads(resp.read().decode('utf-8')).get('data', [])
                        if rows_cboe:
                            c_last = float(rows_cboe[-1].get('close', 0) or 0)
                            if c_last > 0:
                                updates['vix'] = round(c_last, 2)
                                if len(rows_cboe) >= 2:
                                    c_prev = float(rows_cboe[-2].get('close', 0) or 0)
                                    if c_prev > 0:
                                        updates['vix_prev'] = round(c_prev, 2)
                                logger.info(f"  🟢 Live VIX updated via CBOE Official CDN: {updates['vix']}")
                    except Exception as _ve:
                        logger.warning(f"  ⚠️ CBOE VIX fetch exception: {_ve}")

                if 'vix' not in updates:
                    logger.warning(f"🚨 VIX 수집 실패! Intelligent Fallback 가동: {fallback_vix}")
                    updates['vix'] = fallback_vix
                    
                vix_now = updates.get('vix')
                vix_hist_raw = self._cache.get('vix_history', [])
                if not isinstance(vix_hist_raw, list):
                    vix_hist_raw = []
                # vix_history가 20일 미만이면 CBOE Official CDN / 로컬 cross_vix.parquet에서 1개월 일봉 히스토리 백필
                if len(vix_hist_raw) < vix_window:
                    try:
                        import urllib.request
                        req_vh = urllib.request.Request('https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_VIX.json', headers={'User-Agent': 'Mozilla/5.0'})
                        with urllib.request.urlopen(req_vh, timeout=5) as r_vh:
                            rows_vh = json.loads(r_vh.read().decode('utf-8')).get('data', [])
                            closes_vh = [round(float(it.get('close', 0) or 0), 2) for it in rows_vh[-252:] if 5.0 <= float(it.get('close', 0) or 0) <= 120.0]
                            if len(closes_vh) >= vix_window:
                                vix_hist_raw = closes_vh[-252:]
                    except Exception:
                        pass
                if len(vix_hist_raw) < vix_window:
                    try:
                        _cv_p = _PROJECT_ROOT / 'data' / 'kr_markets' / 'cross_vix.parquet'
                        if _cv_p.exists():
                            _cv_df = _pd.read_parquet(_cv_p)
                            _c_col = 'close' if 'close' in _cv_df.columns else ('Close' if 'Close' in _cv_df.columns else None)
                            if _c_col:
                                _vals = [round(float(x), 2) for x in _cv_df[_c_col].dropna().tolist() if 5.0 <= float(x) <= 120.0]
                                if len(_vals) >= 2:
                                    vix_hist_raw = _vals[-252:]
                    except Exception:
                        pass
                if vix_now and isinstance(vix_now, (int, float)) and (vix_now > 0):
                    today_date_str = datetime.now().strftime('%Y-%m-%d')
                    vix_hist_raw = list(vix_hist_raw)
                    if (self._cache.get('vix_history_last_date') == today_date_str or (len(vix_hist_raw) > 0 and abs(vix_hist_raw[-1] - float(vix_now)) < 1e-4)) and len(vix_hist_raw) > 0:
                        vix_hist_raw[-1] = float(vix_now)
                    else:
                        vix_hist_raw.append(float(vix_now))
                    if len(vix_hist_raw) > 252:
                        vix_hist_raw = vix_hist_raw[-252:]
                    updates['vix_history'] = vix_hist_raw
                    updates['vix_history_last_date'] = today_date_str
                if len(vix_hist_raw) >= 2:
                    window_data = vix_hist_raw[-vix_window:]
                    n_w = len(window_data)
                    mu = sum(window_data) / n_w
                    var = sum(((x - mu) ** 2 for x in window_data)) / n_w
                    sigma = _math.sqrt(var) if var > 0 else 0.0
                    updates['vix_ma_20'] = round(mu, 4)
                    updates['vix_std_20'] = round(sigma, 4)
                    if 'vix_prev' not in updates and vix_hist_raw[-2] > 0:
                        updates['vix_prev'] = round(float(vix_hist_raw[-2]), 2)
                    if not updates.get('vix_change_1d') and vix_hist_raw[-2] > 0:
                        updates['vix_change_1d'] = round((vix_hist_raw[-1] / vix_hist_raw[-2] - 1.0) * 100.0, 2)
                    if len(vix_hist_raw) >= 21 and vix_hist_raw[-21] > 0:
                        updates['vix_change_1m'] = round((vix_hist_raw[-1] / vix_hist_raw[-21] - 1.0) * 100.0, 2)
                    logger.debug(f"  [S1 Support] VIX Rolling: n={n_w}, ma={mu:.2f}, std={sigma:.2f}")
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

        ★ 지수 레벨(kospi, kospi_close, kospi_ma20, kospi200, kosdaq_close)에는
        절대로 ETF 가격(069500, 229200)을 대입하지 않고 pykrx_compat.stock.get_index_ohlcv
        ('1001', '1028', '2001')를 사용하며, ETF는 지수 OHLCV 실패 시 등락률 계산에만 사용.
        """
        updates = {}
        n_updated = 0
        try:
            try:
                from src.data_collection.pykrx_compat import stock as pykrx_stock
            except ImportError:
                from pykrx import stock as pykrx_stock
            from datetime import timedelta
            today_str = datetime.now().strftime('%Y%m%d')
            hist_start = (datetime.now() - timedelta(days=60)).strftime('%Y%m%d')
            try:
                _get_idx = getattr(pykrx_stock, 'get_index_ohlcv', None) or getattr(pykrx_stock, 'get_index_ohlcv_by_date')
                df_kospi = _get_idx(hist_start, today_str, '1001')
                if df_kospi is not None and len(df_kospi) >= 1:
                    close_col = '종가' if '종가' in df_kospi.columns else ('Close' if 'Close' in df_kospi.columns else df_kospi.columns[3])
                    closes = df_kospi[close_col].astype(float)
                    today_close = float(closes.iloc[-1])
                    if 1000.0 < today_close < 10000.0:
                        updates['kospi_close'] = today_close
                        updates['kospi'] = today_close
                        if len(closes) >= 2:
                            prev_close = float(closes.iloc[-2])
                            updates['kospi_prev_close'] = prev_close
                            if prev_close > 0:
                                chg_pct = round((today_close / prev_close - 1) * 100, 4)
                                updates['kospi_change_1d'] = chg_pct
                                updates['kospi_change_pct'] = chg_pct
                        high_col = '고가' if '고가' in df_kospi.columns else ('High' if 'High' in df_kospi.columns else df_kospi.columns[1])
                        low_col = '저가' if '저가' in df_kospi.columns else ('Low' if 'Low' in df_kospi.columns else df_kospi.columns[2])
                        try:
                            today_high = float(df_kospi[high_col].iloc[-1])
                            today_low = float(df_kospi[low_col].iloc[-1])
                            if today_close > 0 and today_high > today_low:
                                updates['intraday_range_pct'] = round((today_high - today_low) / today_close * 100, 4)
                        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                            logger.debug(f"Targeted fallback: {e}")
                        if len(closes) >= 20:
                            updates['kospi_ma20'] = round(float(closes.iloc[-20:].mean()), 2)
                        n_updated += 1
                        logger.debug(f"  KOSPI index(1001): close={today_close:.2f}, chg={updates.get('kospi_change_1d', '?')}%, MA20={updates.get('kospi_ma20', '?')}")
            except Exception as e:
                logger.warning(f"  KOSPI(1001) 지수 갱신 실패: {e}")

            if 'kospi_change_1d' not in updates:
                try:
                    df = pykrx_stock.get_market_ohlcv_by_date(hist_start, today_str, '069500')
                    if df is not None and len(df) >= 2:
                        close_col = '종가' if '종가' in df.columns else df.columns[3]
                        closes = df[close_col].astype(float)
                        today_etf = float(closes.iloc[-1])
                        prev_etf = float(closes.iloc[-2])
                        if prev_etf > 0:
                            chg_pct = round((today_etf / prev_etf - 1) * 100, 4)
                            updates['kospi_change_1d'] = chg_pct
                            updates['kospi_change_pct'] = chg_pct
                        high_col = '고가' if '고가' in df.columns else df.columns[1]
                        low_col = '저가' if '저가' in df.columns else df.columns[2]
                        try:
                            today_high = float(df[high_col].iloc[-1])
                            today_low = float(df[low_col].iloc[-1])
                            if today_etf > 0 and today_high > today_low:
                                updates['intraday_range_pct'] = round((today_high - today_low) / today_etf * 100, 4)
                        except Exception:
                            pass
                        n_updated += 1
                except Exception as e:
                    if dhm:
                        dhm.record('kr_kospi_069500', e, 'critical', context={'ticker': '069500', 'method': 'pykrx'}, fallback_used='yfinance ^KS11')
                    else:
                        logger.warning(f"  KOSPI 등락률 프록시(069500) 갱신 실패: {e}")

            if 'kospi_close' not in updates:
                try:
                    from src.data_collection.alpha_vantage_collector import collect_global_macro
                    av_res = collect_global_macro(['^KS11'])
                    _k_min = 1000
                    _k_max = 10000
                    if '^KS11' in av_res and _k_min < float(av_res['^KS11']['price']) < _k_max:
                        _av_px = float(av_res['^KS11']['price'])
                        updates['kospi'] = _av_px
                        updates['kospi_close'] = _av_px
                        updates['kospi_change_1d'] = av_res['^KS11']['change_1d']
                        updates['kospi_change_pct'] = av_res['^KS11']['change_1d']
                        _existing_ma20 = float(self._cache.get('kospi_ma20', 0) or 0)
                        if _k_min < _existing_ma20 < _k_max:
                            updates['kospi_ma20'] = _existing_ma20
                        else:
                            import pandas as _pd_ma
                            _etf_pq = _PROJECT_ROOT / 'data' / 'kr_markets' / 'kr_069500.parquet'
                            if _etf_pq.exists():
                                _df_e = _pd_ma.read_parquet(_etf_pq)
                                _ce = 'close' if 'close' in _df_e.columns else ('Close' if 'Close' in _df_e.columns else None)
                                if _ce and len(_df_e) >= 20:
                                    _cse = _pd_ma.to_numeric(_df_e[_ce], errors='coerce').dropna()
                                    if len(_cse) >= 20 and float(_cse.iloc[-1]) > 0:
                                        updates['kospi_ma20'] = round(_av_px * (float(_cse.tail(20).mean()) / float(_cse.iloc[-1])), 2)
                        n_updated += 1
                except Exception as e:
                    if dhm:
                        dhm.record('kr_kospi_alphavantage', e, 'critical', context={'ticker': '^KS11'}, fallback_used='없음 — KOSPI 데이터 완전 누락')
                    else:
                        logger.warning(f"  KOSPI(AlphaVantage) fallback 실패: {e}")

            try:
                _get_idx = getattr(pykrx_stock, 'get_index_ohlcv', None) or getattr(pykrx_stock, 'get_index_ohlcv_by_date')
                df_k200 = _get_idx(hist_start, today_str, '1028')
                if df_k200 is not None and len(df_k200) >= 1:
                    close_col = '종가' if '종가' in df_k200.columns else ('Close' if 'Close' in df_k200.columns else df_k200.columns[3])
                    closes_200 = df_k200[close_col].astype(float)
                    k200_close = float(closes_200.iloc[-1])
                    if 100.0 < k200_close < 2000.0:
                        updates['kospi200'] = k200_close
                        if len(closes_200) >= 2:
                            updates['kospi200_prev_close'] = float(closes_200.iloc[-2])
                        n_updated += 1
            except Exception as e:
                logger.warning(f"  KOSPI200(1028) 지수 갱신 실패: {e}")

            try:
                _get_idx = getattr(pykrx_stock, 'get_index_ohlcv', None) or getattr(pykrx_stock, 'get_index_ohlcv_by_date')
                df_kq = _get_idx(hist_start, today_str, '2001')
                if df_kq is not None and len(df_kq) >= 1:
                    close_col = '종가' if '종가' in df_kq.columns else ('Close' if 'Close' in df_kq.columns else df_kq.columns[3])
                    closes_kq = df_kq[close_col].astype(float)
                    today_kq = float(closes_kq.iloc[-1])
                    if 200.0 < today_kq < 5000.0:
                        updates['kosdaq_close'] = today_kq
                        if len(closes_kq) >= 2:
                            prev_kq = float(closes_kq.iloc[-2])
                            updates['kosdaq_prev_close'] = prev_kq
                            if prev_kq > 0:
                                kq_chg = round((today_kq / prev_kq - 1) * 100, 4)
                                updates['kosdaq_change_1d'] = kq_chg
                                updates['kosdaq_change_pct'] = kq_chg
                        if len(closes_kq) >= 20:
                            updates['kosdaq_ma20'] = round(float(closes_kq.iloc[-20:].mean()), 2)
                        n_updated += 1
            except Exception as e:
                logger.warning(f"  KOSDAQ(2001) 지수 갱신 실패: {e}")

            if 'kosdaq_change_1d' not in updates:
                try:
                    df = pykrx_stock.get_market_ohlcv_by_date(hist_start, today_str, '229200')
                    if df is not None and len(df) >= 2:
                        close_col = '종가' if '종가' in df.columns else df.columns[3]
                        closes = df[close_col].astype(float)
                        today_etf = float(closes.iloc[-1])
                        prev_etf = float(closes.iloc[-2])
                        if prev_etf > 0:
                            kq_chg = round((today_etf / prev_etf - 1) * 100, 4)
                            updates['kosdaq_change_1d'] = kq_chg
                            updates['kosdaq_change_pct'] = kq_chg
                        n_updated += 1
                except Exception as e:
                    if dhm:
                        dhm.record('kr_kosdaq_229200', e, 'critical', context={'ticker': '229200'})
                    else:
                        logger.warning(f"  KOSDAQ 등락률 프록시(229200) 갱신 실패: {e}")
            try:
                vk_val = self._safe_pykrx_index('1004', today_str)
                if vk_val is not None:
                    updates['vkospi'] = vk_val
                    n_updated += 1
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
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
                logger.warning(f"  KR 지수 갱신 실패: {e}")
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
            lp_window = int(pget('s1.lp_pressure_rolling_window', 20))
            lp_hist = self._cache.get('lp_pressure_history', [])
            if not isinstance(lp_hist, list):
                lp_hist = []
            try:
                from src.data_collection.ss_etf_feature_engine import SSETFFeatureEngine
                engine = SSETFFeatureEngine()
                sam_feat = engine.compute('005930') or {}
                lp_now = sam_feat.get('lp_delta_pressure')
                if lp_now is not None and isinstance(lp_now, (int, float)):
                    today_date_str = datetime.now().strftime('%Y-%m-%d')
                    lp_hist = list(lp_hist)
                    if self._cache.get('lp_pressure_history_last_date') == today_date_str and len(lp_hist) > 0:
                        lp_hist[-1] = float(lp_now)
                    else:
                        lp_hist.append(float(lp_now))
                    if len(lp_hist) > 252:
                        lp_hist = lp_hist[-252:]
                    updates['lp_pressure_history'] = lp_hist
                    updates['lp_pressure_history_last_date'] = today_date_str
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
                logger.debug(f"  [S1 Support] LP Pressure Rolling: n={n_w}, ma={mu:.2f}, std={sigma:.2f}")
            else:
                updates.setdefault('lp_pressure_ma', 0.0)
                updates.setdefault('lp_pressure_std', 500.0)
                logger.debug('  [S1 Support] LP Pressure 히스토리 부족 → fallback(0.0, 500.0)')
        except Exception as _e:
            logger.error(f'  [S1 Support] LP Pressure rolling 계산 실패: {_e}', exc_info=True)
        try:
            atr_5m_window = int(pget('s1.atr_5m_window', 5))
            import numpy as _np
            import pandas as _pd
            df_daily = None
            _p069500 = _PROJECT_ROOT / 'data' / 'kr_markets' / 'kr_069500.parquet'
            if _p069500.exists():
                df_daily = _pd.read_parquet(_p069500)
            if (df_daily is None or len(df_daily) < atr_5m_window + 1):
                from src.data_collection.pykrx_compat import stock as _pykrx
                today_str = datetime.now().strftime('%Y%m%d')
                start_str = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')
                df_daily = _pykrx.get_market_ohlcv_by_date(start_str, today_str, '069500')
            if df_daily is not None and len(df_daily) >= atr_5m_window + 1:
                high_col = 'high' if 'high' in df_daily.columns else ('고가' if '고가' in df_daily.columns else df_daily.columns[1])
                low_col = 'low' if 'low' in df_daily.columns else ('저가' if '저가' in df_daily.columns else df_daily.columns[2])
                close_col = 'close' if 'close' in df_daily.columns else ('종가' if '종가' in df_daily.columns else df_daily.columns[3])
                h = _pd.to_numeric(df_daily[high_col], errors='coerce').dropna().values
                l = _pd.to_numeric(df_daily[low_col], errors='coerce').dropna().values
                c = _pd.to_numeric(df_daily[close_col], errors='coerce').dropna().values
                n_min = min(len(h), len(l), len(c))
                if n_min >= atr_5m_window + 1:
                    h, l, c = h[-n_min:], l[-n_min:], c[-n_min:]
                    pc = _np.roll(c, 1)
                    pc[0] = c[0]
                    tr = _np.maximum(h - l, _np.maximum(_np.abs(h - pc), _np.abs(l - pc)))
                    recent_c = c[-1] if c[-1] > 0 else 1.0
                    # 일봉 ATR/Close를 정규장 5분봉(하루 78개 바) 스케일(1 / sqrt(78))로 변환
                    atr_daily = float(_np.mean(tr[-atr_5m_window:])) / recent_c
                    atr_val = atr_daily / _math.sqrt(78.0)
                    updates['atr_5m'] = round(atr_val, 6)
                    logger.debug(f"  [S1 Support] ATR 5m: {atr_val:.5f} (window={atr_5m_window}, n_bars={n_min})")
                else:
                    updates.setdefault('atr_5m', 0.0)
            else:
                updates.setdefault('atr_5m', 0.0)
        except Exception as _e:
            logger.debug(f'  [S1 Support] ATR 5m 계산 폴백(0.0): {_e}')
            updates.setdefault('atr_5m', 0.0)
        if updates:
            self._update_cache(updates)
            logger.info(f"  ✅ [S1 Support] 파생 통계 주입 완료: lp_ma={updates.get('lp_pressure_ma', 'N/A')}, lp_std={updates.get('lp_pressure_std', 'N/A')}, atr_5m={updates.get('atr_5m', 'N/A')}")

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
                logger.info(f"  ✅ 환율 BOK ECOS API 성공: {fx_val:.2f}")
            else:
                fx_nv = self._fetch_usdkrw_naver()
                if fx_nv and fx_nv > 0:
                    updates['usdkrw'] = fx_nv
                    logger.info(f"  ✅ 환율 Yahoo Direct 성공: {fx_nv:.2f}")
                else:
                    errors.append('KRW=X')

        if not symbols_to_fetch:
            return (updates, errors)

        logger.info(f"  🌍 [Alpha Vantage] 배치 조회 시작: {symbols_to_fetch}")
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
                updates[f"{cache_key}_change_1d"] = res['change_1d']
                if res.get('prev_close') is not None and float(res.get('prev_close', 0) or 0) > 0:
                    updates[f"{cache_key}_prev"] = float(res['prev_close'])
            else:
                errors.append(yf_ticker)
                
        return (updates, errors)

    @staticmethod
    def _fetch_usdkrw_naver() -> Optional[float]:
        """Naver FX 공식 REST API 단건 호출로 USDKRW 제공."""
        try:
            import urllib.request
            req = urllib.request.Request('https://api.stock.naver.com/marketindex/exchange/FX_USDKRW/prices?page=1&pageSize=5', headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                items = json.loads(resp.read().decode('utf-8'))
            if isinstance(items, list) and items:
                val = float(str(items[0].get('closePrice', '0')).replace(',', '') or 0)
                if val > 0:
                    return val
        except Exception as e:
            import logging
            logging.getLogger(__name__).debug(f"Naver FX_USDKRW fetch failed: {e}")
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
        """yfinance 대신 로컬 DataBus 및 캐시 참조 (deprecated yfinance 분리)."""
        return ({}, list(ticker_map.keys()) if ticker_map else [])

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
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            return True

    def _safe_pykrx_index(self, index_code: str, date_str: str) -> Optional[float]:
        """pykrx_compat 지수 조회 (KeyError/IndexError 방어)."""
        try:
            from src.data_collection.pykrx_compat import stock as pykrx_stock
            start_str = (datetime.now() - timedelta(days=15)).strftime('%Y%m%d')
            df = pykrx_stock.get_index_ohlcv_by_date(start_str, date_str, index_code)
            if df is not None and not df.empty and len(df) > 0:
                for c in ['종가', 'Close', 'close', '종가(pt)', '지수']:
                    if c in df.columns:
                        val = float(df[c].iloc[-1])
                        if val > 0:
                            return val
                if len(df.columns) > 0:
                    val = float(df.iloc[-1, -1] if len(df.columns) == 1 else df.iloc[-1, -2])
                    if val > 0:
                        return val
        except Exception as e:
            logger.debug(f"  _safe_pykrx_index ({index_code}) 폴백 스킵: {e}")
        return None

    def _load_signal_cache(self) -> Dict:
        """signal_cache.json 로드."""
        try:
            if _SIGNAL_CACHE.exists():
                return json.loads(_SIGNAL_CACHE.read_text())
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            logger.warning('[SILENT_BYPASS] Suppressed exception at macro_realtime_refresher.py:945', exc_info=True)
        return {}

    _INDEX_SANITY_BOUNDS = {
        'sp500': (2000.0, 20000.0), 'SP500': (2000.0, 20000.0),
        'nasdaq': (5000.0, 60000.0), 'NASDAQ': (5000.0, 60000.0),
        'sox': (2500.0, 30000.0), 'SOX': (2500.0, 30000.0),
        'dji': (15000.0, 100000.0), 'DJI': (15000.0, 100000.0),
        'dxy': (70.0, 160.0), 'DXY': (70.0, 160.0),
        'gold_us': (1000.0, 10000.0), 'GOLD_US': (1000.0, 10000.0),
        'wti': (20.0, 140.0), 'WTI': (20.0, 140.0),
        'kospi': (1000.0, 10000.0), 'KOSPI': (1000.0, 10000.0),
        'kospi_close': (1000.0, 10000.0), 'kospi_ma20': (1000.0, 10000.0),
        'kospi200': (150.0, 1500.0),
        'kosdaq_close': (300.0, 3000.0), 'kosdaq_ma20': (300.0, 3000.0), 'KOSDAQ': (300.0, 3000.0),
        'taiex': (5000.0, 100000.0), 'TAIEX': (5000.0, 100000.0),
        'nikkei': (10000.0, 150000.0), 'NIKKEI': (10000.0, 150000.0),
        'hangseng': (5000.0, 60000.0), 'HANGSENG': (5000.0, 60000.0),
        'us10y': (0.5, 15.0), 'US10Y': (0.5, 15.0),
        'usdkrw': (900.0, 2000.0), 'USDKRW': (900.0, 2000.0),
        'vix': (5.0, 120.0), 'VIX': (5.0, 120.0),
    }

    def _update_cache(self, updates: Dict):
        """signal_cache.json 원자적(Atomic) 쓰기 업데이트.

        [Red Team V5] 다중 프로세스 충돌 방지 (TOC/TOU Data Race 제거).
        파일 락(fcntl.flock) 기반 safe_json_update 적용 + 지수 스케일 오염 자동 정화.
        """
        try:
            from src.infra.safe_io import safe_json_update
            bounds_map = self._INDEX_SANITY_BOUNDS
            last_safe_updates: Dict[str, Any] = {}

            def _apply_updates(existing: dict) -> dict:
                nonlocal last_safe_updates
                # 0. 기존 캐시에 남아있는 ETF 가격 오염값(예: sp500=763, nasdaq=709, gold_us=382, dxy=28.9) 사전 제거
                for _ek in list(existing.keys()):
                    if _ek in bounds_map and isinstance(existing.get(_ek), (int, float)):
                        _lo, _hi = bounds_map[_ek]
                        if not (_lo <= float(existing[_ek]) <= _hi):
                            existing.pop(_ek, None)

                # --- Z-Score / Threshold Sanity Check (Level vs Rate-of-Change 분리 방어막) ---
                safe_updates = {}
                _max_rate_abs = 35.0
                for k, v in updates.items():
                    if k in bounds_map and isinstance(v, (int, float)):
                        _lo, _hi = bounds_map[k]
                        if not (_lo <= float(v) <= _hi):
                            logger.warning(f"  ⚠️ [Sanity Bounds] {k}={v} 정상 범위({_lo}~{_hi}) 이탈 — 기각")
                            continue
                    # 1. 등락률/변동폭(%pt) 변수: 0 교차 가능하므로 상대 비율 대신 절대 %pt 범위 검사
                    if isinstance(v, (int, float)) and (k.endswith('_1d') or k.endswith('_change_pct') or k == 'intraday_range_pct'):
                        if abs(float(v)) > _max_rate_abs:
                            logger.warning(f"  ⚠️ [Sanity Rate Bounds] {k}={v}% 비정상 등락률 이탈 — 기각")
                            continue
                    # 2. 가격/지수/금리/환율 Level 변수(bounds_map 등록 키): 상대 변동률 스파이크 검사
                    elif k in bounds_map and isinstance(v, (int, float)):
                        prev = existing.get(k)
                        if prev and isinstance(prev, (int, float)) and prev > 0:
                            jump_pct = abs(v - prev) / prev

                            limit = 0.15  # 지수류 15% 초과 변동 차단
                            is_vix = 'vix' in k.lower()
                            if is_vix: limit = 2.0
                            elif 'usdkrw' in k.lower(): limit = 0.05
                            elif 'yield' in k.lower() or 'us10y' in k.lower() or 'us5y' in k.lower() or 'us30y' in k.lower(): limit = 0.50

                            if jump_pct > limit:
                                if is_vix:
                                    logger.critical(f"  🚨 [VIX BLACK SWAN] {k} 급등 감지 (기존: {prev}, 신규: {v}, 변동: {jump_pct*100:.1f}%). 정상 데이터로 수용!")
                                    safe_updates[k] = v
                                else:
                                    logger.critical(f"  🚨 [Sanity Check] {k} 비정상 스파이크 감지 (기존: {prev}, 신규: {v}, 변동: {jump_pct*100:.1f}%). 갱신 기각 및 캐시 유지!")
                                    safe_updates[k] = prev
                                continue
                    safe_updates[k] = v
                    if k.upper() in bounds_map and isinstance(v, (int, float)):
                        safe_updates[k.upper()] = v

                # Telemetry Key Alias Mapping for US Market Regime & Strategy Compatibility
                if 'sp500_change_1d' in safe_updates:
                    safe_updates['spx_change_1d'] = safe_updates['sp500_change_1d']
                if 'nasdaq_change_1d' in safe_updates:
                    safe_updates['ixic_change_1d'] = safe_updates['nasdaq_change_1d']
                if 'sox_change_1d' in safe_updates:
                    safe_updates['soxx_change_1d'] = safe_updates['sox_change_1d']
                safe_updates.setdefault('spx_change_1d', safe_updates.get('sp500_change_1d', existing.get('spx_change_1d', 0.0)))
                safe_updates.setdefault('ixic_change_1d', safe_updates.get('nasdaq_change_1d', existing.get('ixic_change_1d', 0.0)))
                safe_updates.setdefault('soxx_change_1d', safe_updates.get('sox_change_1d', existing.get('soxx_change_1d', 0.0)))
                safe_updates.setdefault('ofi_z', existing.get('ofi_z', 0.0))
                safe_updates.setdefault('lead_lag', existing.get('lead_lag', 0.0))

                last_safe_updates = dict(safe_updates)
                existing.update(safe_updates)
                existing['timestamp'] = datetime.now().isoformat()
                return existing

            success = safe_json_update(_SIGNAL_CACHE, _apply_updates)
            if success:
                self._cache.update(last_safe_updates)
            else:
                logger.error(f"  ❌ signal_cache 업데이트 실패 (File Lock 획득 실패)")
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
            option_data = collector.get_kospi200_option_oi(underlying_price=kospi200, n_strikes=int(pget('gex.n_strikes', 5)))
            if not option_data:
                logger.debug('GEX 파이프라인: 옵션 데이터 없음 → 스킵')
                return
            gex_call = sum((row['oi'] * row['gamma'] for row in option_data if row['type'] == 'call'))
            gex_put = sum((row['oi'] * row['gamma'] for row in option_data if row['type'] == 'put'))
            gex = gex_call - gex_put
            today_date_str = datetime.now().strftime('%Y-%m-%d')
            gex_history: list = list(self._cache.get('gex_history', []))
            if self._cache.get('gex_history_last_date') == today_date_str and len(gex_history) > 0:
                gex_history[-1] = gex
            else:
                gex_history.append(gex)
            max_hist = int(pget('gex.history_length', 20))
            gex_history = gex_history[-max_hist:]
            crash_warning = False
            crash_severity = 0.0
            if len(gex_history) >= 5:
                import statistics
                gex_mean = statistics.mean(gex_history)
                gex_std = statistics.stdev(gex_history) if len(gex_history) > 1 else 1e-09
                gex_threshold = float(pget('gex.crash_z_score_threshold', -2.0))
                if gex_std > 0:
                    z_score = (gex - gex_mean) / gex_std
                    if z_score < gex_threshold:
                        crash_warning = True
                        crash_severity = min(1.0, abs(z_score - gex_threshold) / abs(gex_threshold))
            updates = {'gex': round(gex, 4), 'gex_call': round(gex_call, 4), 'gex_put': round(gex_put, 4), 'gex_history': gex_history, 'gex_history_last_date': today_date_str, 'gex_crash_warning': crash_warning, 'gex_crash_severity': round(crash_severity, 4), 'gex_updated_at': datetime.now().isoformat()}
            self._update_cache(updates)
            if crash_warning:
                logger.warning(f"  ⚠️ [GEX 경보] gex={gex:.2f} | crash_warning=True | severity={crash_severity:.2f}")
            else:
                logger.info(f"  📐 GEX 갱신: {gex:.2f} (경보 없음)")
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
                    av_timeout = int(pget('macro_refresh.av_timeout_sec', 10))
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
                    tnote_res = kis.get_us_tnote_futures_tick()
                    if isinstance(tnote_res, dict) and tnote_res.get('futures_price'):
                        updates_macro['us_tnote_futures'] = tnote_res['futures_price']
                        updates_macro['us_tnote_change_pct'] = tnote_res.get('change_pct', 0.0)
                        logger.info(f"  🇺🇸 T-Note 선물 KIS 수집: {tnote_res['futures_price']:.4f} ({tnote_res.get('change_pct', 0.0):+.2f}%)")
                    elif isinstance(tnote_res, (int, float)) and tnote_res > 0:
                        updates_macro['us10y'] = tnote_res
                        updates_macro['us10y_source'] = 'kis_tnote_proxy'
                        logger.info(f"  🇺🇸 US10Y KIS 프록시 사용: {tnote_res:.4f}")
                except Exception as _tn_err:
                    logger.error(f'T-Note 프록시 실패: {_tn_err}', exc_info=True)
            if not av_success or 'usdkrw' not in updates_macro:
                from src.utils.resilient_market_data import ResilientFetcher
                rf = ResilientFetcher()
                fx_val = rf.get_current_price("USDKRW=X") or rf.get_current_price("KRW=X")
                if fx_val and fx_val > 0:
                    updates_macro['usdkrw'] = fx_val
                    logger.info(f"  🇰🇷 ResilientFetcher USDKRW 환율 연동 성공: {fx_val:.2f}")
                else:
                    logger.warning("  ⚠️ ResilientFetcher USDKRW 환율 수집 실패 — 핫-페일오버 시도 중")

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
                                updates_macro['us10y'] = round(float(vals[0]), 4)
                                updates_macro['us10y_source'] = 'fred_daily_interp'
                except Exception as _fred_err:
                    logger.error(f'FRED US10Y 수집 실패: {_fred_err}', exc_info=True)
            dxy = self._cache.get('dxy')
            if dxy:
                updates_macro['dxy'] = float(dxy)
            macro_spike = False
            spike_z_max = 0.0
            import statistics
            spike_window = int(pget('macro_spike.history_length', 20))
            spike_z_threshold = float(pget('macro_spike.z_threshold', 2.5))
            today_date_str = datetime.now().strftime('%Y-%m-%d')
            for metric_key in ('us10y', 'dxy', 'usdkrw'):
                current_val = updates_macro.get(metric_key, self._cache.get(metric_key))
                if current_val is None:
                    continue
                hist_key = f'{metric_key}_history'
                date_key = f'{hist_key}_last_date'
                hist: list = list(self._cache.get(hist_key, []))
                if self._cache.get(date_key) == today_date_str and len(hist) > 0:
                    hist[-1] = float(current_val)
                else:
                    hist.append(float(current_val))
                hist = hist[-spike_window:]
                updates_macro[hist_key] = hist
                updates_macro[date_key] = today_date_str
                if len(hist) >= 5:
                    mean = statistics.mean(hist)
                    std = statistics.stdev(hist) if len(hist) > 1 else 1e-09
                    if std > 0:
                        z = (float(current_val) - mean) / std
                        updates_macro[f"{metric_key}_z_score"] = round(z, 4)
                        if z >= spike_z_threshold:
                            macro_spike = True
                            spike_z_max = max(spike_z_max, z)
                            spike_sources.append(f"{metric_key}(z={z:.2f})")
            updates_macro['macro_spike_warning'] = macro_spike
            updates_macro['macro_spike_z_max'] = round(spike_z_max, 4)
            updates_macro['macro_spike_sources'] = spike_sources
            updates_macro['macro_spike_updated_at'] = datetime.now().isoformat()
            self._update_cache(updates_macro)
            if macro_spike:
                logger.warning(f"  ⚠️ [매크로 발작 경보] z_max={spike_z_max:.2f} | sources={spike_sources}")
            else:
                n_updated = len([k for k in ('us10y', 'dxy', 'usdkrw') if k in updates_macro])
                logger.info(f"  📡 매크로 스파이크 갱신: {n_updated}개 지표, 발작 없음")
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
                    logger.warning(f"  🐕 웩더독 발동! severity={wtd_data['wag_the_dog_severity']:.3f}")
            ema_period = int(pget('vix_trailing.ema_period', 10))
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
                    logger.warning(f"  🛑 VIX 모멘텀 반전! EMA기울기: {prev_momentum:.4f} → {vix_momentum:.4f} (공포 둔화)")
                else:
                    logger.info(f"  📉 VKOSPI EMA={ema_vals[-1]:.2f}, momentum={vix_momentum:+.4f}")
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
    logger.debug(f"\n=== MacroRealtimeRefresher 결과 ===")
    logger.info(f"  시간: {result['timestamp']}")
    logger.info(f"  장중: {result['market_open']}")
    logger.info(f"  Tier1: {result['tier1'].get('n_updated', 0)}개 갱신, {result['tier1'].get('n_errors', 0)}개 실패")
    logger.info(f"  Tier2: {result['tier2'].get('n_updated', 0)}개 갱신, {result['tier2'].get('n_errors', 0)}개 실패")
    logger.info(f"  KR:    {result.get('kr_indices', {}).get('n_updated', 0)}개 갱신")
    if result.get('skipped'):
        logger.info(f"  스킵:  {result['skipped']}")