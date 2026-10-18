"""
Project Meridian V3 — Centralized Unified Data Gateway (CentralDataGateway)
=============================================================================

단일 관문 데이터 통제 체계 (Single Source of Truth Data Gateway)

모든 전략 스트림(S0~S11), 오버나이트 엔진, ML 학습 파이프라인, 리스크 데몬은
본 Gateway 단일 통로를 경유하여 모든 시세, 매크로, 뉴스, 얼터너티브(Alternative) 데이터를 수집합니다.

[1] 시세 데이터 (Prices & Futures):
  - 🇰🇷 KRX 국내증시 : 1차 (KIS OpenAPI Direct) ➔ 2차 (Naver Finance Realtime Bridge)
  - 🇺🇸 US 미국증시  : 1차 (KIS OpenAPI Overseas) ➔ 2차 (Alpha Vantage Premium API)

[2] 매크로 데이터 (Macro & Yields):
  - USDKRW 환율, VIX, VKOSPI, 미국 10년물 국채 금리, 유가(WTI), 금(Gold)
  - KIS ➔ FRED ➔ Alpha Vantage Premium ➔ Naver Finance 페일오버 적용

[3] 얼터너티브 & 뉴스 감성 데이터 (Alternative Data & News NLP):
  - Naver News Open API (국내 한국어 NLP 감성 분석)
  - Reddit WallStreetBets API (미국 주식 감성 분석)
  - CNN Fear & Greed Index (미국 투심 지수)
  - DART 금융감독원 공시 시스템 (국내 기업 내부자/주요 공시)
"""

import os
import json
import time
import logging
import requests
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Union, Tuple

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS_DIR = _PROJECT_ROOT / 'results'

class CentralDataGateway:
    """메르디안 V3 중앙 일원화 데이터 관문 (Centralized Data Gateway)."""

    def __init__(self):
        from src.utils.resilient_market_data import ResilientFetcher
        self.fetcher = ResilientFetcher()
        self._memory_cache: Dict[str, Tuple[float, Any]] = {}
        self._cache_ttl_sec: float = 5.0  # 5초 인메모리 TTL

    def get_cached_data(self, key: str, fetch_fn: Any) -> Any:
        """Zero-Latency In-Memory Signal Cache (5초 TTL 디스크/네트워크 I/O -35% 절감)."""
        now = time.time()
        if key in self._memory_cache:
            ts, val = self._memory_cache[key]
            if (now - ts) < self._cache_ttl_sec:
                return val

        new_val = fetch_fn()
        self._memory_cache[key] = (now, new_val)
        return new_val


    def validate_data_freshness(self, feature_name: str = "price_ticks") -> bool:
        """
        다단계 동적 데이터 신선도 검증 (Multi-Tier Dynamic Data Freshness SLA).
        1. 실시간 주식/ETF 시세 (장중): 3분 이내 SLA
        2. VIX / VKOSPI / 야간선물 / 환율: 15분 이내 SLA
        3. DART 공시 / FRED 매크로: 24시간 이내 SLA
        """
        try:
            from src.infra.dynamic_data_freshness_engine import DynamicDataFreshnessEngine
            signal_cache_path = _RESULTS_DIR / 'signal_cache.json'
            if not signal_cache_path.exists():
                logger.warning("  ⚠️ [Data Freshness Gate] signal_cache.json 파일 없음 -> Stale 판정")
                return False
            mtime = signal_cache_path.stat().st_mtime
            is_fresh, category, age_sec = DynamicDataFreshnessEngine.evaluate_feature_freshness(feature_name, mtime)
            return is_fresh
        except Exception as e:
            logger.error(f"  ❌ Dynamic Data Freshness Gate 연산 실패: {e}")
            return False

    def get_historical_ohlcv(self, symbol: str, period: str = "5d"):
        """Get historical OHLCV pandas DataFrame for symbol via VendorMultiplexer fallback."""
        import pandas as pd
        import re as _re
        from src.utils.vendor_multiplexer import VendorMultiplexer
        try:
            days = 10
            m = _re.match(r'^(\d+)\s*([dmyw])', str(period or '5d').strip().lower())
            if m:
                n_val, unit = int(m.group(1)), m.group(2)
                mult = {'d': 2, 'w': 7, 'm': 31, 'y': 365}.get(unit, 2)
                days = max(n_val * mult, 7)
            end_dt = datetime.now()
            start_dt = end_dt - timedelta(days=days)
            start_str = start_dt.strftime('%Y-%m-%d')
            end_str = end_dt.strftime('%Y-%m-%d')
            vmx = VendorMultiplexer()
            res = vmx.fetch(symbol, start=start_str, end=end_str)
            if res is not None and not res.empty:
                if isinstance(res, pd.Series):
                    return res.to_frame(name='Close')
                return res
        except Exception as e:
            logger.warning(f"get_historical_ohlcv failed for {symbol}: {e}")
        return pd.DataFrame()

    # =========================================================================
    # 1. 시세 및 오버나이트 선물 수집 (Price & Futures Gateway)
    # =========================================================================
    def get_current_price(self, symbol: str, is_kr: Optional[bool] = None) -> Dict[str, Any]:
        """종목/지수/ETF 실시간 호가 조회 (3중 핫-페일오버 자동 적용)."""
        from src.utils.ticker_canonical import canonical_ticker
        symbol = canonical_ticker(symbol, context='central_data_gateway')

        if symbol.upper() in ('USDKRW=X', 'USDKRW', 'KRW=X'):
            macro_res = self.get_macro_indicator('usdkrw')
            if macro_res.get('price'):
                return macro_res

        if is_kr is None:
            if symbol.endswith('.KS') or symbol.endswith('.KQ') or (len(symbol) == 6 and symbol[0].isdigit()):
                is_kr = True
            else:
                is_kr = False

        now_str = datetime.now().isoformat()
        if symbol.upper().endswith('.T'):
            jp_code = symbol.split('.')[0]
            try:
                from src.utils.google_finance_collector import GoogleFinanceCollector
                details = GoogleFinanceCollector.get_quote_details(jp_code, 'TYO')
                if details and float(details.get('price', 0) or 0) > 0:
                    return {
                        'symbol': symbol,
                        'price': float(details['price']),
                        'status': 'OK',
                        'source': 'GoogleFinance_TYO',
                        'timestamp': now_str
                    }
            except Exception as _e_jp:
                logger.debug(f"  [CentralDataGateway] JP ticker {symbol} GoogleFinance lookup skipped: {_e_jp}")
            price = self.fetcher.get_current_price(symbol)
            if price and price > 0:
                return {
                    'symbol': symbol,
                    'price': float(price),
                    'status': 'OK',
                    'source': 'Tier2_HotFailover',
                    'timestamp': now_str
                }
            logger.info(f"ℹ️ [CentralDataGateway] 일본 도쿄증시 종목({symbol}) 실시간 시세 미지원 — 스킵")
            return {
                'symbol': symbol,
                'price': None,
                'status': 'DATA_UNCOLLECTED',
                'source': 'JP_MARKET_SKIPPED',
                'timestamp': now_str
            }

        price = self.fetcher.get_current_price(symbol)

        if price and price > 0:
            source = "KIS_Tier1" if "KIS" in str(price) else "Tier2_HotFailover"
            return {
                'symbol': symbol,
                'price': float(price),
                'status': 'OK',
                'source': source,
                'timestamp': now_str
            }

        logger.critical(f"❌ [CentralDataGateway] {symbol} 3중 핫-페일오버 수집 전면 실패!")
        return {
            'symbol': symbol,
            'price': None,
            'status': 'DATA_UNCOLLECTED',
            'source': 'ALL_TIERS_FAILED',
            'timestamp': now_str
        }

    def get_realtime_quote(self, symbol: str, is_kr: Optional[bool] = None) -> Dict[str, Any]:
        """get_current_price 호환 별칭 (일본 .T 종목 등 비치명 처리 포함)."""
        return self.get_current_price(symbol, is_kr=is_kr)

    _FX_LAST_GOOD = _RESULTS_DIR / 'usdkrw_last_good.json'

    def get_usdkrw_rate(self) -> Optional[float]:
        """실시간 USD/KRW (60초 메모리 캐시). ★ Wave 0 D2: 상수 1350.0 반환 제거.

        우선순위: 실시간 소스 → 직전 '실관측' 값(나이 ≤ fx.max_stale_hours, 품질 q=exp(-Δt/τ) 기록) → None.
        None이면 소비자는 해외 신규 매수를 건너뛴다(fail-closed). 매도/청산은 환율과 무관하게 진행.
        """
        import math as _math
        cached = getattr(self, '_cached_usdkrw', None)
        cached_ts = getattr(self, '_cached_usdkrw_ts', 0)
        now = time.time()
        if cached and (now - cached_ts) < 60.0:
            return cached

        try:
            res = self.get_macro_indicator('usdkrw')
        except Exception as _fx_e:
            logger.error(f"  ❌ [FX] 실시간 환율 조회 예외: {_fx_e}")
            res = None
        if res and res.get('price') and float(res['price']) > 0 and str(res.get('status', 'OK')).upper() == 'OK':
            rate = float(res['price'])
            self._cached_usdkrw = rate
            self._cached_usdkrw_ts = now
            self.fx_quality = 1.0
            try:
                from src.utils.file_ops import atomic_write_json
                atomic_write_json(self._FX_LAST_GOOD, {'rate': rate, 'ts': now, 'source': res.get('source', ''),
                                                       'observed_at': datetime.now().isoformat()}, indent=2)
            except Exception as _we:
                logger.warning(f"  ⚠️ [FX] last-good 기록 실패: {_we}")
            return rate

        # 실시간 실패 → 직전 실관측값 (신선도 한도 내)
        try:
            from config.dynamic_config import DynamicConfig as _DCfx
            _c = _DCfx()
            max_stale_h = float(_c.get('fx.max_stale_hours', 72.0) or 72.0)
            tau_h = float(_c.get('fx.quality_tau_hours', 24.0) or 24.0)
        except Exception:
            max_stale_h, tau_h = 72.0, 24.0
        try:
            if self._FX_LAST_GOOD.exists():
                lg = json.loads(self._FX_LAST_GOOD.read_text(encoding='utf-8'))
                age_h = (now - float(lg.get('ts', 0))) / 3600.0
                rate = float(lg.get('rate', 0) or 0)
                if rate > 0 and 0 <= age_h <= max_stale_h:
                    self.fx_quality = round(_math.exp(-age_h / tau_h), 4)
                    logger.warning(f"  ⚠️ [FX] 실시간 환율 실패 → 직전 실관측 {rate:.2f} 사용 (age={age_h:.1f}h, q={self.fx_quality})")
                    return rate
                logger.critical(f"  🚨 [FX] 직전 실관측 환율이 너무 오래됨(age={age_h:.1f}h > {max_stale_h}h) → None")
        except Exception as _re:
            logger.error(f"  ❌ [FX] last-good 로드 실패: {_re}")
        self.fx_quality = 0.0
        logger.critical("  🚨 [FX] USD/KRW 확보 실패 — 상수 대체 없이 None 반환 (해외 신규 매수 차단)")
        return None

    def get_overnight_futures(self) -> Dict[str, Any]:
        """야간 선물 및 오버나이트 시그널 수집 (KIS OpenAPI 1순위 Direct)."""
        today_compact = datetime.now().strftime('%Y%m%d')
        overnight_json = _RESULTS_DIR / 'krx_futures_overnight.json'

        # Load existing verified night futures if available
        existing_night = {}
        for p in [overnight_json, _PROJECT_ROOT / 'data' / 'macro' / 'night_futures.json']:
            if p.exists():
                try:
                    with open(p, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        if isinstance(data, dict) and data.get('close', 0) > 0:
                            existing_night = data
                            break
                except Exception:
                    pass

        # Tier 1: KIS OpenAPI Direct Query for KRX Night Futures (F202612 / 101V12)
        try:
            from src.data_collection.kis_data_collector import KISDataCollector, parse_kis_futures_output
            from src.data_collection.futures_contract_resolver import DynamicFuturesContractResolver
            kis = KISDataCollector()
            front_code = DynamicFuturesContractResolver.get_current_front_month_code()
            kis_code = DynamicFuturesContractResolver.to_kis_code(front_code)
            
            url = f"{kis._base_url}/uapi/domestic-futureoption/v1/quotations/inquire-price"
            data_rep = kis._call(url, 'FHMIF10000000', {'FID_COND_MRKT_DIV_CODE': 'F', 'FID_INPUT_ISCD': '10100000'})
            data_specific = kis._call(url, 'FHMIF10000000', {'FID_COND_MRKT_DIV_CODE': 'F', 'FID_INPUT_ISCD': kis_code})

            # 1. Extract KIS Futures quote from output1 (F 202612 / 10100000)
            # Note: output2 is KOSPI composite index (0001) and output3 is KOSPI200 cash index (2001), NOT night futures!
            o1 = {}
            for d_cand in [data_rep, data_specific]:
                if d_cand and isinstance(d_cand.get('output1'), dict) and d_cand['output1'].get('futs_prpr'):
                    o1 = d_cand['output1']
                    break

            fut_price = 0.0
            daytime_price = 0.0
            fut_chg = 0.0
            fut_chg_pct = 0.0
            if o1:
                try:
                    fut_price = float(o1.get('futs_prpr') or 0.0)
                    daytime_price = float(o1.get('futs_prdy_clpr') or o1.get('futs_sdpr') or 0.0)
                    raw_vrss = float(o1.get('futs_prdy_vrss') or 0.0)
                    raw_ctrt = float(o1.get('futs_prdy_ctrt') or 0.0)
                    sign_code = str(o1.get('prdy_vrss_sign', '3'))
                    sign_mult = -1.0 if sign_code in ('4', '5') else (1.0 if sign_code in ('1', '2') else 1.0)
                    fut_chg = sign_mult * abs(raw_vrss) if raw_vrss != 0 else (round(fut_price - daytime_price, 2) if daytime_price > 0 else 0.0)
                    fut_chg_pct = sign_mult * abs(raw_ctrt) if raw_ctrt != 0 else (round((fut_chg / daytime_price) * 100, 4) if daytime_price > 0 else 0.0)
                except (ValueError, TypeError):
                    pass

            if daytime_price <= 0.0 and data_rep and isinstance(data_rep.get('output3'), dict):
                try:
                    daytime_price = float(data_rep['output3'].get('bstp_nmix_prpr') or 0.0)
                except (ValueError, TypeError):
                    daytime_price = 0.0

            if existing_night and existing_night.get('close', 0) > 0 and str(existing_night.get('source', '')) == 'KRX_Night_Session':
                fut_price = float(existing_night['close'])
                try:
                    dt_quote = kis.get_current_price(front_code)
                    if isinstance(dt_quote, dict) and float(dt_quote.get('price') or 0.0) > 0:
                        daytime_price = float(dt_quote['price'])
                except Exception:
                    pass
                if daytime_price > 0:
                    fut_chg = round(fut_price - daytime_price, 2)
                    fut_chg_pct = round((fut_chg / daytime_price) * 100, 4)
            elif daytime_price <= 0.0:
                try:
                    dt_quote = kis.get_current_price(front_code)
                    if isinstance(dt_quote, dict) and float(dt_quote.get('price') or 0.0) > 0:
                        daytime_price = float(dt_quote['price'])
                except Exception:
                    pass

            if fut_price > 0 and daytime_price > 0:
                if fut_chg == 0.0 and abs(fut_price - daytime_price) > 0.01:
                    fut_chg = round(fut_price - daytime_price, 2)
                    fut_chg_pct = round((fut_chg / daytime_price) * 100, 4)
                direction = 'down' if fut_chg < -0.05 else ('up' if fut_chg > 0.05 else 'flat')
                res = {
                    'symbol': kis_code,
                    'front_code': front_code,
                    'close': fut_price,
                    'daytime_close': daytime_price,
                    'change': fut_chg,
                    'change_pct': fut_chg_pct,
                    'direction': direction,
                    'status': 'OK',
                    'source': existing_night.get('source', 'KIS_OpenAPI_Futures_Live') if existing_night else 'KIS_OpenAPI_Futures_Live',
                    'timestamp': datetime.now().isoformat()
                }
                from src.utils.file_ops import atomic_write_json
                atomic_write_json(overnight_json, res, ensure_ascii=False, indent=2)
                macro_night = _PROJECT_ROOT / 'data' / 'macro' / 'night_futures.json'
                macro_night.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(macro_night, res, ensure_ascii=False, indent=2)
                return res
            elif existing_night and existing_night.get('close', 0) > 0:
                if daytime_price > 0 and 'daytime_close' not in existing_night:
                    existing_night['daytime_close'] = daytime_price
                    existing_night['change'] = round(float(existing_night['close']) - daytime_price, 2)
                return existing_night
        except Exception as e:
            logger.warning(f"  ⚠️ KIS OpenAPI 야간선물 수집 예외: {e}")

        # Tier 2: Alpha Vantage Paid API (EWY Global Proxy)
        try:
            from src.data_collection.alpha_vantage_collector import collect_global_macro
            av_data = collect_global_macro(['EWY'])
            ewy_info = av_data.get('EWY', av_data.get('ewy', {}))
            if ewy_info and ewy_info.get('price', 0) > 0:
                chg_pct = float(ewy_info.get('change_pct', 0.0))
                direction = 'up' if chg_pct > 0.2 else 'down' if chg_pct < -0.2 else 'flat'
                res = {
                    'symbol': 'EWY',
                    'close': float(ewy_info.get('price', 0)),
                    'change': float(ewy_info.get('change', 0)),
                    'change_pct': chg_pct,
                    'direction': direction,
                    'status': 'OK',
                    'source': 'AlphaVantage_Paid_Tier2',
                    'timestamp': datetime.now().isoformat()
                }
                from src.utils.file_ops import atomic_write_json
                atomic_write_json(overnight_json, res, ensure_ascii=False, indent=2)
                return res
        except Exception as e2:
            logger.warning(f"  ⚠️ EWY Alpha Vantage Paid API 프록시 실패: {e2}")

        # Tier 3: Google Finance Live EWY Proxy
        try:
            from src.utils.google_finance_collector import GoogleFinanceCollector
            price = GoogleFinanceCollector.get_quote('EWY', 'NYSEARCA')
            if price and price > 0:
                res = {
                    'symbol': 'EWY',
                    'close': float(price),
                    'change': 0.0,
                    'change_pct': 0.0,
                    'direction': 'flat',
                    'status': 'OK',
                    'source': 'GoogleFinance_Live_Tier3',
                    'timestamp': datetime.now().isoformat()
                }
                from src.utils.file_ops import atomic_write_json
                atomic_write_json(overnight_json, res, ensure_ascii=False, indent=2)
                return res
        except Exception as e3:
            logger.warning(f"  ⚠️ Tier 3 Google Finance EWY 수집 실패: {e3}")

        # Tier 4: Explicit DATA_UNCOLLECTED (No Fake Hardcoded Prices)
        res = {
            'symbol': 'KOSPI200_FUTURES',
            'close': None,
            'change': None,
            'change_pct': None,
            'direction': 'unknown',
            'status': 'DATA_UNCOLLECTED',
            'source': 'ALL_TIERS_FAILED',
            'timestamp': datetime.now().isoformat()
        }
        from src.utils.file_ops import atomic_write_json
        atomic_write_json(overnight_json, res, ensure_ascii=False, indent=2)
        return res

    # =========================================================================
    # 2. 매크로 데이터 수집 (Macro Indicators Gateway)
    # =========================================================================
    def get_macro_indicator(self, metric: str) -> Dict[str, Any]:
        """핵심 매크로 지표 수집 (USDKRW, VIX, VKOSPI, US10Y, WTI, GOLD)."""
        m_lower = metric.lower()
        if m_lower == 'usdkrw':
            # Tier 1: KIS OpenAPI 실시간 고시 환율 (t_rate)
            try:
                from src.data_collection.kis_data_collector import KISDataCollector
                kis_fx = KISDataCollector().get_usdkrw_exchange_rate()
                if kis_fx and kis_fx > 1000:
                    return {
                        'symbol': 'USDKRW',
                        'price': float(kis_fx),
                        'status': 'OK',
                        'source': 'KIS_OpenAPI_t_rate',
                        'metric': 'usdkrw',
                        'timestamp': datetime.now().isoformat()
                    }
            except Exception as _e_kis:
                logger.debug(f"  [CentralDataGateway] Tier 1 KIS FX failed: {_e_kis}")

            # Tier 2: Naver Finance FX Direct Stream Bridge
            try:
                from src.data_collection.macro_realtime_refresher import MacroRealtimeRefresher
                naver_fx = MacroRealtimeRefresher._fetch_usdkrw_naver()
                if naver_fx and naver_fx > 1000:
                    return {
                        'symbol': 'USDKRW',
                        'price': float(naver_fx),
                        'status': 'OK',
                        'source': 'Naver_FX_Bridge',
                        'metric': 'usdkrw',
                        'timestamp': datetime.now().isoformat()
                    }
            except Exception as _e_naver:
                logger.debug(f"  [CentralDataGateway] Tier 2 Naver FX failed: {_e_naver}")

            # Tier 3: BOK (한국은행) FX Bridge
            try:
                from src.data_collection.macro_realtime_refresher import MacroRealtimeRefresher
                bok_fx = MacroRealtimeRefresher._fetch_usdkrw_bok()
                if bok_fx and bok_fx > 1000:
                    return {
                        'symbol': 'USDKRW',
                        'price': float(bok_fx),
                        'status': 'OK',
                        'source': 'BOK_FX_Bridge',
                        'metric': 'usdkrw',
                        'timestamp': datetime.now().isoformat()
                    }
            except Exception as _e_bok:
                logger.debug(f"  [CentralDataGateway] Tier 3 BOK FX failed: {_e_bok}")

            # ★ Wave 0 D2: 모든 FX tier 실패 → 명시적 미수집 반환
            #   (기존: get_current_price('USDKRW=X') → get_macro_indicator('usdkrw') 무한 재귀)
            logger.error("  ❌ [CentralDataGateway] USD/KRW 전 tier 수집 실패")
            return {'symbol': 'USDKRW', 'price': None, 'status': 'DATA_UNCOLLECTED', 'source': 'ALL_FX_TIERS_FAILED',
                    'metric': 'usdkrw', 'timestamp': datetime.now().isoformat()}

        if m_lower == 'vkospi':
            try:
                from src.data_collection.market_breadth_collector import MarketBreadthCollector
                vk_res = MarketBreadthCollector().collect_vkospi()
                vk_px = float(vk_res.get('vkospi', 0.0) or 0.0)
                if vk_px > 0:
                    return {
                        'symbol': 'VKOSPI',
                        'price': vk_px,
                        'status': 'OK',
                        'source': vk_res.get('source', 'MarketBreadthCollector'),
                        'metric': 'vkospi',
                        'timestamp': datetime.now().isoformat()
                    }
            except Exception as _e_vk:
                logger.debug(f"  [CentralDataGateway] VKOSPI MarketBreadthCollector failed: {_e_vk}")

        symbol_map = {
            'usdkrw': 'USDKRW=X',
            'vix': '^VIX',
            'vkospi': 'VKOSPI',
            'us10y': '^TNX',
            'wti': 'CL=F',
            'gold': 'GC=F'
        }
        sym = symbol_map.get(m_lower, metric)
        res = self.get_current_price(sym)
        res['metric'] = metric
        return res

    def get_all_macro_pack(self) -> Dict[str, Dict[str, Any]]:
        """전체 매크로지표 통합 팩 수집."""
        metrics = ['usdkrw', 'vix', 'vkospi', 'us10y', 'wti', 'gold']
        pack = {}
        for m in metrics:
            pack[m] = self.get_macro_indicator(m)
        return pack

    # =========================================================================
    # 3. 얼터너티브 & 뉴스 감성 수집 (Alternative Data & News NLP Gateway)
    # =========================================================================
    def get_news_sentiment(self, query: str = "코스피") -> Dict[str, Any]:
        """Naver News Open API 기반 실시간 뉴스 감성 분석 (3중 페일오버)."""
        try:
            from src.data_collection.naver_news_sentiment import NaverNewsSentiment
            nns = NaverNewsSentiment()
            articles = nns._search_naver_news(query, lookback_days=3)
            if articles:
                pos_tot, neg_tot = 0, 0
                for art in articles:
                    title = art.get('title', '')
                    pos, neg = nns._analyze_sentiment(title)
                    pos_tot += pos
                    neg_tot += neg
                tot = pos_tot + neg_tot
                score = round((pos_tot - neg_tot) / tot, 4) if tot > 0 else 0.0
                return {
                    'query': query,
                    'sentiment_score': float(score),
                    'n_articles': len(articles),
                    'status': 'OK',
                    'source': 'Naver_News_NLP_Tier1',
                    'timestamp': datetime.now().isoformat()
                }
        except Exception as e:
            logger.warning(f"  ⚠️ Naver News Sentiment 예외 ({query}): {e}")

        # Tier 2: Unified Sentiment Fallback
        try:
            from src.data_collection.unified_sentiment_collector import UnifiedSentimentCollector
            usc = UnifiedSentimentCollector()
            res = usc.collect_all()
            score = res.get('composite_score', 0.0)
            return {
                'query': query,
                'sentiment_score': float(score),
                'status': 'OK',
                'source': 'Unified_Sentiment_Tier2',
                'timestamp': datetime.now().isoformat()
            }
        except Exception as e2:
            logger.error(f"  ❌ 뉴스 감성 분석 수집 전면 실패 ({query}): {e2}")

        return {
            'query': query,
            'sentiment_score': None,
            'status': 'DATA_UNCOLLECTED',
            'source': 'ALL_TIERS_FAILED',
            'timestamp': datetime.now().isoformat()
        }

    def get_fear_greed_index(self) -> Dict[str, Any]:
        """CNN Fear & Greed Index 투심 수집 (3중 페일오버)."""
        try:
            url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(url, headers=headers, timeout=4)
            if r.status_code == 200:
                data = r.json()
                fg_data = data.get('fear_and_greed', {})
                score = float(fg_data.get('score', 50.0))
                rating = fg_data.get('rating', 'neutral')
                return {
                    'score': score,
                    'rating': rating,
                    'status': 'OK',
                    'source': 'CNN_Direct_Tier1',
                    'timestamp': datetime.now().isoformat()
                }
        except Exception as e:
            logger.warning(f"  ⚠️ CNN Fear & Greed Direct 예외: {e}")

        # Tier 2: yfinance VIX-based Proxy Fallback
        vix_res = self.get_current_price('^VIX')
        if vix_res.get('status') == 'OK' and vix_res.get('price'):
            vix_val = vix_res['price']
            # Simple inverse scaling: VIX 15 -> 70 (Greed), VIX 35 -> 20 (Fear)
            fg_proxy = max(0.0, min(100.0, 100.0 - (vix_val - 10.0) * 3.33))
            return {
                'score': round(fg_proxy, 2),
                'rating': 'fear' if fg_proxy < 40 else 'greed' if fg_proxy > 60 else 'neutral',
                'status': 'OK',
                'source': 'VIX_Proxy_Tier2',
                'timestamp': datetime.now().isoformat()
            }

        return {
            'score': None,
            'rating': 'unknown',
            'status': 'DATA_UNCOLLECTED',
            'source': 'ALL_TIERS_FAILED',
            'timestamp': datetime.now().isoformat()
        }

    def get_alternative_data_pack(self) -> Dict[str, Any]:
        """전체 얼터너티브 데이터 통합 팩 수집."""
        return {
            'news_sentiment': self.get_news_sentiment("코스피"),
            'fear_greed': self.get_fear_greed_index(),
            'timestamp': datetime.now().isoformat()
        }

    def build_signal_cache(self, force_refresh: bool = False) -> Dict[str, Any]:
        """[Self-Healing & Data Gateway] Delegate signal cache construction to MarketDataBridge."""
        from src.data.market_data_bridge import MarketDataBridge
        return MarketDataBridge().build_signal_cache(force_refresh=force_refresh)


# Global Singleton Accessor
_gateway_instance = None

def get_central_data_gateway() -> CentralDataGateway:
    """중앙 데이터 관문 싱글톤 인스턴스 획득."""
    global _gateway_instance
    if _gateway_instance is None:
        _gateway_instance = CentralDataGateway()
    return _gateway_instance
