_INDEX_ETF_PROXY = {'KOSPI': '069500', 'KOSDAQ': '229200'}
_INDEX_YF_MAP = {'KOSPI': '^KS11', 'KOSDAQ': '^KQ11'}
'\nProject Meridian — Market Data Bridge\n======================================\nMeridian 자체 data/ 디렉토리에서 시장 데이터를 읽어\nOrchestrator가 사용하는 포맷으로 변환.\n\n데이터 소스 우선순위:\n  1. OIS JSON (data/raw/overnight_intel/)\n  2. Historical parquet (data/kr_markets/)\n  3. KRX daily CSV (data/raw/krx_stock_daily/)\n  4. fdr 폴백 (VIX, S&P500, 환율)\n\nUsage:\n    from src.data.market_data_bridge import MarketDataBridge\n    bridge = MarketDataBridge()\n    signal = bridge.build_signal_cache()\n'
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup
from src.utils.retry_utils import with_retry
from src.infra.alert_manager import AlertManager
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = Path(os.environ.get('MERIDIAN_DATA_DIR', str(_PROJECT_ROOT / 'data')))

class DataIntegrityError(Exception):
    """[Phase 84] 쓰레기 데이터 유입(Stale, Glitch, Fetch Fail) 방어용 치명적 예외."""
    pass

class DataSanityGate:
    """[Phase 84] 수집된 데이터의 물리적 한계점 및 직전 값 대비 이상치(Glitch) 검증기."""

    @staticmethod
    def validate(name: str, current: float, previous: float, max_pct_change: float=0.2):
        if previous <= 0:
            return current
        pct_change = abs(current - previous) / previous
        if pct_change > max_pct_change:
            raise DataIntegrityError(f"[{name} Sanity Fail] 비정상적 폭등/폭락 감지. (Prev: {previous:.2f} -> Curr: {current:.2f}, 변동률: {pct_change * 100:.1f}% > 제한: {max_pct_change * 100:.1f}%)")
        return current

def _load_alpha_signals_fallback(project_root: Path) -> Dict:
    """[Alpha Factory Integration] AlphaFactorySubscriber 미배포 환경용 JSON Fallback.

    alpha_signal.json (Subscriber가 쓰는 캐시) → discovered_alphas.json 순으로 시도.
    두 파일 모두 없거나 파싱 실패해도 크래시 없이 빈 dict 반환.
    """
    for _candidate in [project_root / 'results' / 'alpha_signal.json', project_root / 'data' / 'alpha_signal.json']:
        if _candidate.exists():
            try:
                _data = json.loads(_candidate.read_text())
                if isinstance(_data, dict):
                    return _data
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:

                import logging

                logger = logging.getLogger(__name__)

                logger.error(f'Critical error encountered (removed targeted fallback): {e}', exc_info=True)

                raise
    _disc_path = project_root / 'results' / 'discovered_alphas.json'
    if _disc_path.exists():
        try:
            _alphas = json.loads(_disc_path.read_text())
            _latest = {}
            if isinstance(_alphas, list) and _alphas:
                _latest = _alphas[-1]
            elif isinstance(_alphas, dict):
                _inner = _alphas.get('alphas', [])
                _latest = _inner[-1] if isinstance(_inner, list) and _inner else _alphas
            if _latest:
                return {'S1_signal': {'contagion_alert': _latest.get('contagion_alert', 'NORMAL'), 'vix_spike': _latest.get('vix_spike', False)}, 'S3_signal': {'sector_forecast': _latest.get('sector_forecast', {})}, 'S4_signal': {'macro_cycle': _latest.get('macro_cycle', 'Expansion')}}
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:

            import logging

            logger = logging.getLogger(__name__)

            logger.error(f'Critical error encountered (removed targeted fallback): {e}', exc_info=True)

            raise
    return {}

class DataSourceCircuitBreaker:
    """데이터 소스 전용 Circuit Breaker (주문 API 적용 금지).

    상태: CLOSED → OPEN (3회 연속 실패) → HALF-OPEN (60분 후) → CLOSED
    주문 체결 API(kis_order_buy/sell)는 CB 없이 Exponential Backoff만 사용.
    """

    def __init__(self, source_name: str, cfg=None):
        self._name = source_name
        self._cfg = cfg
        self._fail_count = 0
        self._state = 'CLOSED'
        self._opened_at = None

    def _get(self, key: str, default):
        if self._cfg:
            return self._cfg.get(key, default)
        return default

    def call(self, func, *args, **kwargs):
        """Circuit Breaker 래핑으로 func 호출.

        OPEN 상태이면 즉시 None 반환 (실제 호출 없음).
        HALF_OPEN/CLOSED 상태이면 func 실행 후 성공/실패 카운트.
        """
        enabled = bool(self._get('data.cb.data_api.enabled', True))
        trip_count = int(self._get('data.cb.data_api.trip_count', 3))
        reset_min = int(self._get('data.cb.data_api.reset_minutes', 60))
        if not enabled:
            return func(*args, **kwargs)
        if self._state == 'OPEN':
            from datetime import datetime
            elapsed = (datetime.now() - self._opened_at).total_seconds() / 60
            if elapsed >= reset_min:
                logger.info(f"  [CB:{self._name}] {elapsed:.0f}분 경과 → HALF-OPEN 시도")
                self._state = 'HALF_OPEN'
            else:
                logger.warning(f'  [CB:{self._name}] OPEN 상태 — 소스 차단 (재개까지 {reset_min - elapsed:.0f}분)', exc_info=False)
                return None
        try:
            result = func(*args, **kwargs)
            if self._state == 'HALF_OPEN':
                logger.info(f"  [CB:{self._name}] HALF-OPEN 성공 → CLOSED 복귀")
            self._fail_count = 0
            self._state = 'CLOSED'
            return result
        except Exception as e:
            self._fail_count += 1
            logger.error(f'  [CB:{self._name}] 실패 ({self._fail_count}/{trip_count}): {e}', exc_info=True)
            if self._fail_count >= trip_count:
                from datetime import datetime
                self._state = 'OPEN'
                self._opened_at = datetime.now()
                logger.error(f'  [CB:{self._name}] OPEN 전환 — {reset_min}분 차단', exc_info=False)
            return None

def _dynamic_trust_select(field: str, value_a: float, value_b: float, history_a: list, history_b: list, cfg=None) -> float:
    """MA 기반 동적 신뢰도 선택 (Cross-Source Validation).

    두 소스의 값이 max_divergence_pct 이상 괴리될 때 각 소스의
    MA 대비 이상치 여부를 판정하여 더 신뢰할 수 있는 값을 반환합니다.

    Args:
        field:     필드명 (로깅용)
        value_a:   1차 소스 값
        value_b:   2차 소스 값
        history_a: 1차 소스의 최근 가격 이력 (3거래일)
        history_b: 2차 소스의 최근 가격 이력 (3거래일)
        cfg:       DynamicConfig 인스턴스 (없으면 기본값 사용)

    Returns:
        선택된 신뢰 값 (float)
    """

    def _g(k, d):
        return cfg.get(k, d) if cfg else d
    enabled_fields = list(_g('data.crossval.enabled_fields', ['vix', 'kospi', 'vkospi']))
    if field not in enabled_fields:
        return value_a
    max_div = float(_g('data.crossval.max_divergence_pct', 0.05))
    ma_window = int(_g('data.crossval.ma_window_days', 3))
    ma_outlier = float(_g('data.crossval.ma_outlier_threshold', 0.1))
    import numpy as _np_cv
    ref = (value_a + value_b) / 2.0
    if ref == 0:
        return value_a
    divergence = abs(value_a - value_b) / abs(ref)
    if divergence <= max_div:
        return value_a

    def _is_outlier(val: float, hist: list) -> bool:
        if len(hist) < 2:
            return False
        ma = float(_np_cv.mean(hist[-ma_window:]))
        if ma == 0:
            return False
        return abs(val - ma) / abs(ma) > ma_outlier
    a_outlier = _is_outlier(value_a, history_a)
    b_outlier = _is_outlier(value_b, history_b)
    if a_outlier and (not b_outlier):
        chosen = value_b
        label = 'B (A가 이상값)'
    elif b_outlier and (not a_outlier):
        chosen = value_a
        label = 'A (B가 이상값)'
    else:
        ma_a = float(_np_cv.mean(history_a[-ma_window:])) if history_a else 0
        dist_a = abs(value_a - ma_a)
        dist_b = abs(value_b - ma_a)
        chosen = value_a if dist_a <= dist_b else value_b
        label = f"{('A' if dist_a <= dist_b else 'B')} (MA 근접)"
    import logging as _log_cv
    _log_cv.getLogger(__name__).warning(f"  [CrossVal:{field}] 소스 괴리 {divergence:.1%} > {max_div:.1%} → 신뢰 소스={label} | A={value_a:.4g}, B={value_b:.4g}")
    return chosen
_CACHE_DIR = _PROJECT_ROOT / 'results'
from config.dynamic_config import DynamicConfig

class MarketDataBridge:
    """Meridian용 시장 데이터 통합 브릿지."""

    def __init__(self):
        self._cache_file = _CACHE_DIR / 'signal_cache.json'
        self._cache: Dict = {}
        self._field_quality: Dict[str, float] = {}
        self._current_phase: str = 'unknown'
        self._load_cache()
        try:
            from config.dynamic_config import DynamicConfig
            self._cfg = DynamicConfig()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:

            import logging

            logger = logging.getLogger(__name__)

            logger.error(f'Critical error encountered (fallback self._cfg=None): {e}', exc_info=True)
            self._cfg = None
        self._cb_vix = DataSourceCircuitBreaker('fdr_vix', cfg=self._cfg)
        self._cb_vkospi = DataSourceCircuitBreaker('pykrx_vkospi', cfg=self._cfg)
        self._cb_kospi = DataSourceCircuitBreaker('pykrx_kospi', cfg=self._cfg)

    def _get(self, key: str, default: Any=None) -> Any:
        if self._cfg:
            return self._cfg.get(key, default)
        return default

    def _ensure_root_meta(self, signal: Dict) -> Dict:
        """Ensure SSOT Root Meta Fields (Regime, Exposure, QQQ KER) are always populated."""
        if not isinstance(signal, dict):
            return signal
        try:
            # 1. Regime
            if 'regime' not in signal or not signal.get('regime'):
                _regime_path = _PROJECT_ROOT / 'results' / 'current_regime.json'
                if _regime_path.exists():
                    try:
                        _rd = json.loads(_regime_path.read_text(encoding='utf-8'))
                        signal['regime'] = _rd.get('regime', 'caution')
                    except Exception:
                        signal['regime'] = 'caution'
                else:
                    signal['regime'] = 'caution'
            if 'current_regime' not in signal or not signal.get('current_regime'):
                signal['current_regime'] = signal['regime']

            # 2. Exposure
            if 'exposure' not in signal or signal.get('exposure') is None:
                _exp_path = _PROJECT_ROOT / 'results' / 'exposure_history.json'
                if _exp_path.exists():
                    try:
                        _ed = json.loads(_exp_path.read_text(encoding='utf-8'))
                        if isinstance(_ed, list) and _ed:
                            signal['exposure'] = float(_ed[-1].get('exposure', 0.72))
                        elif isinstance(_ed, dict):
                            signal['exposure'] = float(_ed.get('current_exposure', _ed.get('exposure', 0.72)))
                    except Exception:
                        signal['exposure'] = 0.72
                if not signal.get('exposure'):
                    signal['exposure'] = 0.72

            # 3. QQQ KER (Kaufman Efficiency Ratio)
            if 'qqq_ker' not in signal or signal.get('qqq_ker') is None:
                try:
                    from src.risk.whipsaw_defense_engine import AntiWhipsawDefenseEngine
                    wde = AntiWhipsawDefenseEngine()
                    qqq_df = self._load_price_history('QQQ')
                    if qqq_df is not None and not qqq_df.empty and 'close' in qqq_df.columns:
                        closes = qqq_df['close'].dropna().tolist()
                        signal['qqq_ker'] = round(float(wde.compute_kaufman_efficiency_ratio(closes, period=20)), 4)
                    else:
                        signal['qqq_ker'] = 0.25
                except Exception:
                    signal['qqq_ker'] = 0.25
        except Exception as _meta_e:
            logger.debug(f"  [SignalCache] Root meta fields injection fallback: {_meta_e}")
            if 'regime' not in signal:
                signal['regime'] = 'caution'
                signal['current_regime'] = 'caution'
            if 'exposure' not in signal:
                signal['exposure'] = 0.72
            if 'qqq_ker' not in signal:
                signal['qqq_ker'] = 0.25
        return signal

    def build_signal_cache(self, force: bool=False, force_refresh: bool=False, **kwargs) -> Dict:
        """시그널 캐시 구성 — Orchestrator가 사용하는 시장 데이터 딕셔너리."""
        is_force = force or force_refresh
        self._load_cache()
        if not is_force and self._is_cache_fresh():
            logger.info('  📦 시그널 캐시 히트 (당일 데이터)')
            self._ensure_root_meta(self._cache)
            return self._cache
        self._field_quality = self._cache.get('_field_meta', {}).copy()
        signal = {}
        use_async = self._get('data.use_async_queue', False)
        if use_async:
            import asyncio

            async def _async_fetch():
                loop = asyncio.get_running_loop()
                queue = asyncio.Queue()

                async def fetch_task(name, func):
                    try:
                        res = await loop.run_in_executor(None, func)
                        await queue.put((name, res))
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).error(f"  Async fetch error for {name}: {e}")
                        await queue.put((name, None))
                tasks = [fetch_task('vix', self._get_vix), fetch_task('vkospi', self._get_vkospi), fetch_task('usdkrw', self._get_usdkrw), fetch_task('kospi', self._get_kospi), fetch_task('ois', self._get_ois), fetch_task('sector_momentum', self._get_sector_momentum), fetch_task('stock_technicals', self._get_stock_technicals)]
                await asyncio.gather(*tasks)
                await queue.put(None)
                fetched = {}
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    fetched[item[0]] = item[1]
                return fetched
            try:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    import nest_asyncio
                    nest_asyncio.apply()
                fetched_data = asyncio.run(_async_fetch())
            except Exception as e:
                logger.error(f"  [Async] Failed to use asyncio.Queue, falling back to sync: {e}")
                use_async = False
        if use_async:
            signal['vix'] = fetched_data.get('vix') or self._cache.get('vix', 20.0)
            signal['vkospi'] = fetched_data.get('vkospi') or self._cache.get('vkospi', 20.0)
            usdkrw = fetched_data.get('usdkrw') or {'current': 1300.0, 'prev': 1300.0}
            signal['usdkrw'] = usdkrw.get('current', 0)
            signal['usdkrw_prev'] = usdkrw.get('prev', signal['usdkrw'])
            cached_kospi = self._cache.get('kospi', 0)
            kospi = fetched_data.get('kospi') or {}
            fresh_kospi = float(kospi.get('close', 0) or 0)
            if fresh_kospi > 1000:
                signal['kospi'] = fresh_kospi
                signal['kospi_close'] = fresh_kospi
                signal['kospi_ma20'] = float(kospi.get('ma20', fresh_kospi) or fresh_kospi)
            elif cached_kospi and cached_kospi > 1000:
                signal['kospi'] = cached_kospi
                signal['kospi_close'] = float(self._cache.get('kospi_close', cached_kospi) or cached_kospi)
                signal['kospi_ma20'] = self._cache.get('kospi_ma20', cached_kospi)
            else:
                signal['kospi'] = fresh_kospi
                signal['kospi_close'] = float(self._cache.get('kospi_close', 0) or fresh_kospi)
                signal['kospi_ma20'] = kospi.get('ma20', signal['kospi'])
            _ois_default_val = self._cfg.get('ois.default_value', 50.0) if self._cfg else 50.0
            _fresh_ois = fetched_data.get('ois', _ois_default_val)
            _cached_ois = self._cache.get('ois', 0)
            if _fresh_ois is not None and _fresh_ois != _ois_default_val:
                signal['ois'] = _fresh_ois
            elif _cached_ois and _cached_ois != _ois_default_val:
                signal['ois'] = _cached_ois
            else:
                signal['ois'] = _fresh_ois or _ois_default_val
            signal['sector_momentum'] = fetched_data.get('sector_momentum') or {}
            signal['stock_technicals'] = fetched_data.get('stock_technicals') if fetched_data.get('stock_technicals') is not None else {}
        else:
            signal['vix'] = self._get_vix()
            signal['vkospi'] = self._get_vkospi()
            usdkrw = self._get_usdkrw()
            signal['usdkrw'] = usdkrw.get('current', 0)
            signal['usdkrw_prev'] = usdkrw.get('prev', signal['usdkrw'])
            cached_kospi = self._cache.get('kospi', 0)
            try:
                kospi = self._get_kospi()
            except Exception:
                kospi = {'close': cached_kospi, 'ma20': self._cache.get('kospi_ma20', cached_kospi)}
            fresh_kospi = float(kospi.get('close', 0) or 0)
            if fresh_kospi > 1000:
                signal['kospi'] = fresh_kospi
                signal['kospi_close'] = fresh_kospi
                signal['kospi_ma20'] = float(kospi.get('ma20', fresh_kospi) or fresh_kospi)
            elif cached_kospi and cached_kospi > 1000:
                signal['kospi'] = cached_kospi
                signal['kospi_close'] = float(self._cache.get('kospi_close', cached_kospi) or cached_kospi)
                signal['kospi_ma20'] = self._cache.get('kospi_ma20', cached_kospi)
            else:
                signal['kospi'] = fresh_kospi
                signal['kospi_close'] = float(self._cache.get('kospi_close', 0) or fresh_kospi)
                signal['kospi_ma20'] = kospi.get('ma20', signal['kospi'])
            _ois_default_val = self._cfg.get('ois.default_value', 50.0) if self._cfg else 50.0
            _fresh_ois = self._get_ois()
            _cached_ois = self._cache.get('ois', 0)
            if _fresh_ois != _ois_default_val:
                signal['ois'] = _fresh_ois
            elif _cached_ois and _cached_ois != _ois_default_val:
                signal['ois'] = _cached_ois
                logger.info(f"  📦 OIS: 기존 캐시값 보존 ({_cached_ois:.1f})")
            else:
                signal['ois'] = _fresh_ois
            signal['sector_momentum'] = self._get_sector_momentum()
            signal['stock_technicals'] = self._get_stock_technicals()
        if getattr(self, '_last_ois_details', None):
            signal['ois_details'] = self._last_ois_details
            _nf_raw = (self._last_ois_details.get('components', {}).get('krx_night_futures', {}) or {}).get('raw', {})
            if isinstance(_nf_raw, dict):
                if _nf_raw.get('close'):
                    signal['night_futures'] = float(_nf_raw['close'])
                if _nf_raw.get('daytime_close'):
                    signal['daytime_futures'] = float(_nf_raw['daytime_close'])
                if _nf_raw.get('change_pct') is not None:
                    signal['night_futures_change_1d'] = float(_nf_raw['change_pct'])
        for _k_nf in ('ois_details', 'night_futures', 'daytime_futures', 'night_futures_change_1d'):
            if _k_nf not in signal and self._cache.get(_k_nf) is not None:
                signal[_k_nf] = self._cache[_k_nf]
        signal['timestamp'] = datetime.now().isoformat()
        try:
            _alpha_path = _PROJECT_ROOT / 'results' / 'discovered_alphas.json'
            if _alpha_path.exists():
                _alphas = json.loads(_alpha_path.read_text())
                if isinstance(_alphas, list) and _alphas:
                    _latest = _alphas[-1]
                elif isinstance(_alphas, dict):
                    _latest = _alphas.get('alphas', [_alphas])[-1] if isinstance(_alphas.get('alphas'), list) else _alphas
                else:
                    _latest = {}
                _sharpe = float(_latest.get('sharpe_ratio', _latest.get('sharpe', 0.0)))
                signal['alpha_factory'] = {'s5_overnight_score': _sharpe, 'formula': _latest.get('formula', _latest.get('expression', '')), 'fitness': _latest.get('fitness', _sharpe), 'discovered_at': _latest.get('timestamp', _latest.get('discovered_at', ''))}
                logger.info(f"  🧬 [Alpha Factory] discovered_alphas 로드: s5_overnight_score={_sharpe:.3f}, formula={signal['alpha_factory']['formula'][:40]}")
            else:
                signal['alpha_factory'] = {'s5_overnight_score': 0.0}
                logger.debug('  🧬 [Alpha Factory] discovered_alphas.json 없음 → s5_overnight_score=0')
        except Exception as _af_e:
            signal['alpha_factory'] = {'s5_overnight_score': 0.0}
            logger.warning(f"  🧬 [Alpha Factory] discovered_alphas 로드 실패 (Fallback): {_af_e}")
        try:
            from src.adapters.alpha_factory_subscriber import AlphaFactorySubscriber
            _sub = AlphaFactorySubscriber()
            _af_signals = _sub.get_latest_signals()
            if _af_signals and isinstance(_af_signals, dict):
                signal['alpha_signals'] = _af_signals
                logger.info(f"  🧬 [Alpha Factory] alpha_signals 수신: keys={list(_af_signals.keys())}")
            else:
                signal['alpha_signals'] = {}
                logger.debug('  🧬 [Alpha Factory] alpha_signals 없음 → 빈 dict')
        except ImportError as e:
            signal['alpha_signals'] = _load_alpha_signals_fallback(_PROJECT_ROOT)
            logger.error('  🧬 [Alpha Factory] AlphaFactorySubscriber import 실패 → JSON 파일 Fallback', exc_info=True)
        except Exception as _afs_e:
            signal['alpha_signals'] = {}
            logger.warning(f"  🧬 [Alpha Factory] alpha_signals 로드 실패 (Fallback): {_afs_e}")
        try:
            from config.dynamic_config import DynamicConfig as _DC_dqs
            _cfg_dqs = _DC_dqs()
            _weights_dqs = {'vix': float(_cfg_dqs.get('data.quality.vix_weight', 0.25)), 'vkospi': float(_cfg_dqs.get('data.quality.vkospi_weight', 0.2)), 'kospi': float(_cfg_dqs.get('data.quality.kospi_weight', 0.25)), 'usdkrw': float(_cfg_dqs.get('data.quality.usdkrw_weight', 0.15)), 'ois': float(_cfg_dqs.get('data.quality.ois_weight', 0.15))}
            _dqs = sum((self._field_quality.get(k, 1.0) * w for k, w in _weights_dqs.items()))
            signal['_data_quality_score'] = round(_dqs, 4)
            signal['_field_meta'] = self._field_quality.copy()
            signal['_pipeline_phase'] = getattr(self, '_current_phase', 'unknown')
            try:
                from src.utils.time_utils import now_kst as _nkst
                signal['_generated_at'] = _nkst().isoformat()
            except Exception:
                import datetime as _datetime
                signal['_generated_at'] = _datetime.datetime.now().isoformat()
            logger.debug(f"  [DQS] 품질 점수={_dqs:.3f} | fields={list(self._field_quality.keys())}")
        except Exception as _dqs_e:
            logger.debug(f"  [DQS] 품질 점수 계산 예외: {_dqs_e}")

        # Ensure SSOT Root Meta Fields (Regime, Exposure, QQQ KER) are always populated
        self._ensure_root_meta(signal)
        self._cache = signal
        self._save_cache(signal)
        logger.info(f"  📊 Signal Cache: VIX={signal['vix']:.1f}, VKOSPI={signal['vkospi']:.1f}, USD/KRW={signal['usdkrw']:.0f}, KOSPI={signal['kospi']:.0f}")
        return signal

    def build_overnight_intel(self) -> Dict:
        """야간 시장 정보 — S&P 500, NASDAQ 전일 변동.

        Returns:
            {
                'sp500_change_pct': float,  # S1 Edge 호환
                'nasdaq_change_pct': float, # S1 Edge 호환
                'sox_change_pct': float,    # SOX 반도체지수 변동 (S1 반도체 레버리지용)
                'sp500_change': float,      # 하위호환
                'nasdaq_change': float,     # 하위호환
                'us_sentiment': str,
                'timestamp': str,
            }
        """
        result = {'sp500_change_pct': 0.0, 'nasdaq_change_pct': 0.0, 'sox_change_pct': 0.0, 'sp500_change': 0.0, 'nasdaq_change': 0.0, 'us_sentiment': 'neutral', 'timestamp': datetime.now().isoformat()}
        sp500 = self._read_us_index('^GSPC', 'SPY')
        if sp500:
            result['sp500_change'] = sp500
            result['sp500_change_pct'] = sp500
        nasdaq = self._read_us_index('^IXIC', 'QQQ')
        if nasdaq:
            result['nasdaq_change'] = nasdaq
            result['nasdaq_change_pct'] = nasdaq
        sox = self._read_us_index('^SOX', 'SOXX')
        if sox:
            result['sox_change_pct'] = sox
        else:
            result['sox_change_pct'] = result['nasdaq_change_pct'] * 1.2
        avg = (result['sp500_change'] + result['nasdaq_change']) / 2
        if avg > 1.0:
            result['us_sentiment'] = 'bullish'
        elif avg > 0.3:
            result['us_sentiment'] = 'slightly_bullish'
        elif avg < -1.0:
            result['us_sentiment'] = 'bearish'
        elif avg < -0.3:
            result['us_sentiment'] = 'slightly_bearish'
        return result

    def build_alpha_candidates(self, tickers: List[str]=None) -> List[Dict]:
        """종목별 기술적 지표 계산.

        Returns:
            [{'ticker': str, 'rsi': float, 'bb_position': float, ...}, ...]
        """
        if tickers is None:
            try:
                from config.universe import UNIVERSE
                tickers = UNIVERSE.get('kr_stocks', [])[:30]
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:

                import logging

                logger = logging.getLogger(__name__)

                logger.error(f'Critical error encountered (fallback to default tickers): {e}', exc_info=True)
                _dc_mb = DynamicConfig()
                tickers = _dc_mb.get('market_data.fallback_tickers', ['069500', '005930', '000660', '035420'])
        candidates = []
        for ticker in tickers:
            try:
                df = self._load_price_history(ticker)
                if df is None or len(df) < 20:
                    continue
                close = df['close']
                indicators = {'ticker': ticker, 'price': float(close.iloc[-1]), 'rsi': self._calc_rsi(close), 'bb_position': self._calc_bb_position(close), 'macd_signal': self._calc_macd_signal(close), 'volume_ratio': self._calc_volume_ratio(df), 'momentum_5d': float((close.iloc[-1] / close.iloc[-5] - 1) * 100) if len(close) >= 5 else 0.0, 'momentum_20d': float((close.iloc[-1] / close.iloc[-20] - 1) * 100) if len(close) >= 20 else 0.0}
                candidates.append(indicators)
            except Exception as e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                logger.debug(f"  {ticker} 지표 계산 실패: {e}")
        return candidates

    def get_regime_history(self, days: int=90) -> Dict:
        """레짐 감지에 필요한 히스토리 데이터.

        Returns:
            {
                'vix_history': [float, ...],
                'kospi_returns': [float, ...],
            }
        """
        result = {'vix_history': [], 'kospi_returns': []}
        for vix_pq in [_DATA_DIR / 'kr_markets' / 'cross_vix.parquet', _DATA_DIR / 'global_markets' / 'us_stocks' / '^VIX.parquet', _DATA_DIR / 'kr_markets' / 'VIX.parquet']:
            if vix_pq.exists():
                try:
                    df = pd.read_parquet(vix_pq)
                    vals = df['close'].dropna().tail(days).tolist()
                    if len(vals) > len(result['vix_history']):
                        result['vix_history'] = vals
                    if len(result['vix_history']) >= 30:
                        break
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    logger.debug(f'VIX parquet load skipped ({vix_pq.name}): {e}')
        if len(result['vix_history']) < 30:
            vix_json = _DATA_DIR / 'cache' / 'vix_daily.json'
            if vix_json.exists():
                try:
                    vd = json.loads(vix_json.read_text(encoding='utf-8'))
                    if isinstance(vd, dict) and isinstance(vd.get('vix_values'), list):
                        vals = [float(x) for x in vd['vix_values'] if x is not None][-days:]
                        if len(vals) > len(result['vix_history']):
                            result['vix_history'] = vals
                except Exception as _ve:
                    logger.debug(f'vix_daily.json load skipped: {_ve}')
        kospi_pq = _DATA_DIR / 'kr_markets' / 'kr_indices' / 'kospi_index.parquet'
        if not kospi_pq.exists():
            kospi_pq = _DATA_DIR / 'kr_markets' / 'kr_indices' / 'kospi.parquet'
        
        fetched = False
        if kospi_pq.exists():
            try:
                df = pd.read_parquet(kospi_pq)
                if 'close' in df.columns:
                    rets = df['close'].pct_change().dropna().tail(days)
                    result['kospi_returns'] = rets.tolist()
                    fetched = True
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f"KOSPI parquet failed: {e}")
                pass
                
        if not fetched:
            try:
                from src.data_collection.pykrx_compat import stock
                from datetime import timedelta
                end_str = datetime.now().strftime('%Y%m%d')
                start_str = (datetime.now() - timedelta(days=days*2)).strftime('%Y%m%d')
                df = stock.get_index_ohlcv(start_str, end_str, '1001')
                if df is not None and not df.empty:
                    rets = df['종가'].pct_change().dropna().tail(days)
                    result['kospi_returns'] = rets.tolist()
            except Exception as e:
                import logging
                logging.getLogger(__name__).debug(f"KOSPI regime fallback failed: {e}")
        return result

    def _get_vix(self) -> float:
        """[Live Priority] VIX 지수 (실시간 수집 피드 100% 최우선 반영).

        ★ 수집된 실시간 피드(Live Feed)가 존재할 경우 보정/Mean-Reversion 없이 100% 원본 수치 반환.
        ★ fallback이나 이동평균 보정값이 최신 수집 데이터를 왜곡하는 행위 전면 금지.
        """
        # 1. 최우선: Google Finance Live Collector 실시간 수집 시도 (1순위 SSOT)
        try:
            from src.utils.google_finance_collector import GoogleFinanceCollector
            gf_vix = GoogleFinanceCollector.get_vix()
            if gf_vix > 0:
                self._field_quality['vix'] = 1.0
                logger.info(f"  ✅ [VIX Live Feed] Google Finance 실시간 VIX 100% 채택: {gf_vix:.2f}")
                return gf_vix
        except Exception as e:
            logger.debug(f"  [VIX Google Finance Fetch] 예외: {e}")

        # 2. 2순위: Yahoo Direct Live REST API 실시간 수집 시도 (백업)
        try:
            import requests
            headers = {'User-Agent': 'Mozilla/5.0'}
            url = 'https://query1.finance.yahoo.com/v8/finance/chart/^VIX?interval=1m&range=1d'
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                chart = data.get('chart', {}).get('result', [{}])[0]
                meta = chart.get('meta', {})
                price = meta.get('regularMarketPrice')
                if price and float(price) > 0:
                    live_vix = float(price)
                    self._field_quality['vix'] = 1.0
                    logger.info(f"  ✅ [VIX Live Feed] Yahoo REST API 백업 VIX 채택: {live_vix:.2f}")
                    return live_vix
        except Exception as e:
            logger.debug(f"  [VIX Live Fetch] 예외: {e}")

        # 2. 2순위: OIS JSON 최근 수집 피드 (overnight_macro & overnight_intel 둘 다 탐색)
        for ois_dir in [_DATA_DIR / 'raw' / 'overnight_macro', _DATA_DIR / 'raw' / 'overnight_intel']:
            if ois_dir.exists():
                try:
                    files = sorted([f for f in ois_dir.glob('*.json') if not f.name.endswith('.lock')], reverse=True)
                    if files:
                        file_age = (datetime.now() - datetime.fromtimestamp(files[0].stat().st_mtime)).days
                        if file_age <= 2:
                            data = json.loads(files[0].read_text())
                            vix = 0
                            if 'macro_indicators' in data and isinstance(data['macro_indicators'], dict):
                                vix = data['macro_indicators'].get('vix', {}).get('last_close', 0)
                            if not vix:
                                vix = data.get('vix', data.get('VIX', 0))
                            if vix > 0:
                                self._field_quality['vix'] = 1.0
                                return float(vix)
                except Exception as e:
                    logger.error(f"  [Phase 84] OIS VIX 파싱 에러: {e}")

        # 3. 3순위: Parquet 저장소 원본 수치 (Mean-Reversion 왜곡 없이 원본 그대로)
        for path in [_DATA_DIR / 'us_stocks' / 'prices' / '^VIX.parquet']:
            if path.exists():
                try:
                    df = pd.read_parquet(path)
                    val = float(df['close'].iloc[-1])
                    if val > 0:
                        self._field_quality['vix'] = 0.8
                        return val
                except Exception as e:
                    logger.error(f"  [VIX Parquet] 읽기 에러: {e}")

        logger.warning('  [Data Janitor] 모든 VIX 소스 실패. 비상 Imputation(20.0) 적용.')
        self._field_quality['vix'] = 0.2
        return 20.0

    def _safe_get_index_close(self, index_code: str, days: int=5):
        """지수 종가를 안전하게 가져오기 (KIS 업종지수 -> pykrx 지수 -> ETF 프록시(1028/2203 한정)).

        Returns:
            float or None
        """
        from datetime import timedelta as _td
        _today = datetime.now().strftime('%Y%m%d')
        _start = (datetime.now() - _td(days=max(days, 10))).strftime('%Y%m%d')
        _kis_sector_map = {'1001': '0001', 'KOSPI': '0001', '2001': '1001', 'KOSDAQ': '1001', '1028': '2001', '2203': '1203'}
        _pykrx_idx_map = {'KOSPI': '1001', 'KOSDAQ': '2001'}
        kis_sector = _kis_sector_map.get(index_code)
        if kis_sector:
            try:
                from src.data_collection.kis_data_collector import KISDataCollector
                kis = KISDataCollector()
                df_kis = kis.get_kr_sector_daily(kis_sector, _start, _today)
                if df_kis is not None and not df_kis.empty:
                    close_col = 'Close' if 'Close' in df_kis.columns else ('종가' if '종가' in df_kis.columns else df_kis.columns[3])
                    val = float(df_kis[close_col].iloc[-1])
                    if val > 0:
                        logger.info(f"  지수 {index_code}: {val:.2f} (KIS Sector API {kis_sector})")
                        return val
            except Exception as e:
                logger.debug(f"  KIS Sector API 실패 ({index_code}): {e}")

        @with_retry(max_retries=2, initial_delay=1.0, fallback_value=None)
        def fetch_pykrx_index():
            from src.data_collection.pykrx_compat import stock as _pykrx
            pykrx_code = _pykrx_idx_map.get(index_code, index_code)
            df = _pykrx.get_index_ohlcv(_start, _today, pykrx_code)
            if df is not None and (not df.empty):
                close_col = '종가' if '종가' in df.columns else ('Close' if 'Close' in df.columns else df.columns[3])
                return float(df.iloc[-1][close_col])
            raise ValueError('pykrx index empty')
        val = fetch_pykrx_index()
        if val is not None and val > 0:
            logger.debug(f"  지수 {index_code}: {val} (인스티튜셔널 브리지 직접)")
            return val

        # ETF 프록시는 스케일 환산이 정의된 파생지수(1028, 2203)에만 허용하며, 1001(KOSPI)/2001(KOSDAQ) 지수 레벨에는 ETF 원가 혼합 금지
        etf_ticker = _INDEX_ETF_PROXY.get(index_code)
        if etf_ticker and index_code in ('1028', '2203'):
            try:
                from src.data_collection.kis_data_collector import KISDataCollector
                kis = KISDataCollector()
                price = kis.get_current_price(etf_ticker)
                raw_price = price.get('price') if isinstance(price, dict) else price
                if raw_price is not None and float(raw_price) > 0:
                    val = float(raw_price)
                    if index_code == '1028':
                        val = val / 100.0 if val > 10000.0 else val / 10.0
                    elif index_code == '2203':
                        val = val / 20.0
                    logger.info(f"  지수 {index_code}: {val:.2f} (KIS API Proxy {etf_ticker})")
                    return val
            except Exception as e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                logger.debug(f"  KIS API 실패: {e}")
            try:
                from src.data_collection.pykrx_compat import stock
                df = stock.get_market_ohlcv_by_date(_start, _today, etf_ticker)
                if df is not None and (not df.empty):
                    val = float(df['종가'].iloc[-1])
                    if index_code == '1028':
                        val = val / 100.0 if val > 10000.0 else val / 10.0
                    elif index_code == '2203':
                        val /= 20.0
                    return val
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                logger.error(f'Critical error encountered (removed targeted fallback): {e}', exc_info=True)
                raise
        if index_code != '1004':
            AlertManager().report_error(f"INDEX_{index_code}", '모든 소스 실패 (KIS/인스티튜셔널/fdr/ETF)', severity='error')
            logger.warning(f"  지수 {index_code}: 모든 소스 실패 (KIS/인스티튜셔널/fdr/ETF)")
        return None

    def _get_vkospi(self) -> float:
        """[Phase 84] VKOSPI (Strict Staleness + CB 보호).

        데이터 소스: KIS API -> Realized Volatility (Intraday) -> 인스티튜셔널 EOD Fallback.
        ★ 웹 스크래핑 pykrx는 전면 축출하고 KIS/인스티튜셔널 피드를 최우선 채택합니다.
        """
        # 1. KIS API 실시간 (Tier 1)
        try:
            from src.data_collection.kis_data_collector import KISDataCollector
            kis = KISDataCollector()
            closes = kis.get_vkospi_1min(n_candles=1)
            if closes and len(closes) > 0:
                val = float(closes[-1])
                if 5.0 <= val <= 150.0:
                    self._field_quality['vkospi'] = 0.95
                    import logging
                    logging.getLogger(__name__).info(f"  [VKOSPI] KIS API 수집 성공: {val:.2f}")
                    return val
        except Exception as e:
            import logging
            logging.getLogger(__name__).debug(f"KIS API VKOSPI 실패: {e}")

        # 2. Robust Realized Volatility + Global VIX Shrinkage Estimation (Tier 2 - 이상치 내성 실현 변동성 + VIX 베이지안 결합)
        try:
            from src.data_collection.pykrx_compat import stock
            from datetime import timedelta
            end_str = datetime.now().strftime('%Y%m%d')
            start_str = (datetime.now() - timedelta(days=45)).strftime('%Y%m%d')
            df = stock.get_index_ohlcv(start_str, end_str, '1001') # 1001 is KOSPI
            if df is not None and not df.empty:
                close = df['종가'].dropna().tail(20)
                if len(close) >= 20:
                    rets = close.pct_change().dropna()
                    # Huber/MAD Robust Volatility (단일 이상치로 인한 20일간의 VKOSPI>45 고착 오발화 차단)
                    med_r = float(rets.median())
                    mad_sigma = float((rets - med_r).abs().median() * 1.4826 * np.sqrt(252) * 100.0)
                    ewma_sigma = float(rets.ewm(span=7).std().iloc[-1] * np.sqrt(252) * 100.0)
                    robust_rv = 0.65 * mad_sigma + 0.35 * min(ewma_sigma, mad_sigma * 1.5)
                    vix_ref = 0.0
                    try:
                        from src.utils.google_finance_collector import GoogleFinanceCollector
                        vix_ref = float(GoogleFinanceCollector.get_vix() or 0.0)
                    except Exception:
                        vix_ref = float(self._cache.get('vix', 0.0) or 0.0)
                    if 5.0 <= vix_ref <= 100.0:
                        estimated = round(0.55 * (vix_ref * 1.05) + 0.45 * robust_rv, 2)
                    else:
                        estimated = round(robust_rv * 1.05, 2)
                    if 5.0 <= estimated <= 150.0:
                        self._field_quality['vkospi'] = 0.85
                        import logging
                        logging.getLogger(__name__).info(f"  [VKOSPI] 베이지안 로버스트 변동성 추정 성공: {estimated:.2f} (MAD={mad_sigma:.2f}, VIX={vix_ref:.2f})")
                        return estimated
        except Exception as e:
            import logging
            logging.getLogger(__name__).debug(f"Realized Volatility Estimation 실패: {e}")

        # 3. VIX Global Proxy Fallback (Tier 3)
        try:
            from src.utils.google_finance_collector import GoogleFinanceCollector
            vix_val = GoogleFinanceCollector.get_vix()
            if vix_val and 5.0 <= vix_val <= 100.0:
                proxy_vkospi = round(vix_val * 1.05, 2)
                self._field_quality['vkospi'] = 0.7
                logger.info(f"  [VKOSPI] Global VIX Proxy 연동 성공: {proxy_vkospi:.2f}")
                return proxy_vkospi
        except Exception as _e:
            logger.debug(f"VKOSPI Global VIX Proxy 실패: {_e}")
            
        AlertManager().report_error('VKOSPI', '모든 우회 수단 실패. VIX 기반 프록시를 시도합니다.', severity='warning')
        try:
            vix_val = self._get_vix()
            vix_quality = self._field_quality.get('vix', 0.0)
            
            if vix_quality >= 0.7:
                coeff = float(self._cfg.get('data.vkospi_proxy_coefficient', 0.82))
                proxy_quality = float(self._cfg.get('data.vkospi_proxy_quality', 0.35))
                proxy_vkospi = vix_val * coeff
                self._field_quality['vkospi'] = proxy_quality
                return proxy_vkospi
            else:
                AlertManager().report_error('VKOSPI', 'VIX 품질 0.7 미만(프록시 불가). 최후의 보루 20.0(상수)을 주입합니다.', severity='critical')
                self._field_quality['vkospi'] = 0.0
                return 20.0
        except Exception as e:
            AlertManager().report_error('VKOSPI', f"VIX 프록시 폴백마저 실패(예외 발생). 최후의 보루 20.0 주입: {e}", severity='critical')
            self._field_quality['vkospi'] = 0.0
            return 20.0

    def _get_usdkrw(self) -> Dict:
        """[Phase 84] USD/KRW 환율 (Strict Staleness & Sanity Check).

        ★ Alpha Vantage 명시적 1차 호출 → fdr 2차 → parquet 폴백 (parquet stale 방지).
        """
        try:
            from src.utils.vendor_multiplexer import VendorMultiplexer
            end = datetime.now()
            start = end - timedelta(days=5)
            
            # 1. 사용자 요청에 따른 최우선(Primary) 소스: KIS OpenAPI
            fx_series = None
            try:
                from src.data_collection.kis_data_collector import KISDataCollector
                kis_val = KISDataCollector().get_usdkrw_exchange_rate()
                if kis_val:
                    fx_series = pd.Series([kis_val], index=[pd.Timestamp.now()])
                    logger.info(f"  [USDKRW] KIS API 1차 수집 성공: {kis_val}")
            except Exception as e:
                logger.warning(f"  [USDKRW] KIS API 수집 실패: {e}")

            # 2. KIS 실패 시 Fallback: Alpha Vantage
            if fx_series is None:
                try:
                    import requests
                    from src.utils.credential_manager import CredentialManager
                    av_key = CredentialManager.get('ALPHA_VANTAGE_API_KEY')
                    if av_key:
                        url = f'https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE&from_currency=USD&to_currency=KRW&apikey={av_key}'
                        resp = requests.get(url, timeout=5)
                        if resp.status_code == 200:
                            data = resp.json()
                            if 'Realtime Currency Exchange Rate' in data:
                                rate_str = data['Realtime Currency Exchange Rate'].get('5. Exchange Rate')
                                if rate_str:
                                    fx_val = float(rate_str)
                                    if fx_val < 900.0 or fx_val > 1800.0:
                                        logger.error(f"  🚨 [Outlier Rejection] Alpha Vantage USDKRW 이상치 감지: {fx_val}. 폐기하고 2차 소스로 넘어갑니다.")
                                    else:
                                        fx_series = pd.Series([fx_val], index=[pd.Timestamp.now()])
                                        logger.info(f"  [USDKRW] Alpha Vantage Fallback 수집 성공: {fx_val}")
                    if fx_series is None:
                        logger.warning(f"  [USDKRW] Alpha Vantage 데이터 없음, 3차 fdr 시도")
                except Exception as e:
                    logger.warning(f"  [USDKRW] Alpha Vantage 수집 실패, 3차 fdr 시도: {e}")
            
            # 2. AV 실패 시 VendorMultiplexer (fdr 등) 폴백
            if fx_series is None or fx_series.empty:
                fx_series = VendorMultiplexer(cfg=self._cfg).fetch(ticker='USDKRW=X', start=start.strftime('%Y-%m-%d'), end=end.strftime('%Y-%m-%d'))
                if fx_series is not None and not fx_series.empty:
                    AlertManager().report_error('USDKRW', 'Alpha Vantage 수집 실패, fdr(VendorMultiplexer)로 우회(Fallback) 수집됨.', severity='critical')

            if fx_series is not None and (not fx_series.empty):
                if len(fx_series) >= 2:
                    curr = float(fx_series.iloc[-1])
                    prev = float(fx_series.iloc[-2])
                else:
                    curr = float(fx_series.iloc[-1])
                    prev = curr
                if curr != prev:
                    DataSanityGate.validate('USDKRW(Vendor)', curr, prev, max_pct_change=0.1)
                self._field_quality['usdkrw'] = 1.0
                return {'current': curr, 'prev': prev}
        except DataIntegrityError as e:
            logger.error(str(e))
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f"  VendorMultiplexer USDKRW 실패: {e}")
        for path in [_DATA_DIR / 'us_stocks' / 'prices' / 'KRW=X.parquet']:
            if path.exists():
                try:
                    df = pd.read_parquet(path)
                    age_days = (datetime.now() - pd.Timestamp(df.index[-1])).days
                    curr_val = float(df['close'].iloc[-1])
                    prev_val = float(df['close'].iloc[-2]) if len(df) > 1 else curr_val
                    if age_days > 3:
                        from src.data_collection.macro_realtime_refresher import MacroRealtimeRefresher
                        live_fx = MacroRealtimeRefresher._fetch_usdkrw_naver() or MacroRealtimeRefresher._fetch_usdkrw_bok()
                        if live_fx:
                            logger.info(f"  [Data Janitor] ✅ USDKRW 실시간 수집 성공 (Naver/BOK): {live_fx:.2f}원")
                            return {'current': round(live_fx, 2), 'prev': round(live_fx, 2)}
                    return {'current': curr_val, 'prev': prev_val}
                except Exception as e:
                    from src.utils.error_logger import log_error_rate_limited
                    log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                    logger.debug(f"  [Data Janitor] USDKRW parquet 읽기 예외: {e}")
        # 실시간 API 수집 시도
        try:
            from src.data_collection.macro_realtime_refresher import MacroRealtimeRefresher
            live_fx = MacroRealtimeRefresher._fetch_usdkrw_naver() or MacroRealtimeRefresher._fetch_usdkrw_bok()
            if live_fx:
                logger.info(f"  [Data Janitor] ✅ USDKRW 실시간 수집 성공: {live_fx:.2f}원")
                self._field_quality['usdkrw'] = 1.0
                return {'current': round(live_fx, 2), 'prev': round(live_fx, 2)}
        except Exception as e_live:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e_live}", exc_info=True)
            logger.debug(f"  [Data Janitor] USDKRW 실시간 수집 예외: {e_live}")

        logger.warning('  [Data Janitor] 모든 USDKRW 소스 실패. 최신 시장 환율(1415.7) 임시 적용.')
        self._field_quality['usdkrw'] = 0.5
        return {'current': 1415.7, 'prev': 1415.7}

    def _get_kospi(self) -> Dict:
        """KOSPI 지수 + MA20.

        데이터 소스 우선순위:
          1. kr_markets 지수 parquet (kospi_index.parquet 등)
          2. KISDataCollector().get_kr_sector_daily('0001') / pykrx_compat.stock.get_index_ohlcv('1001')
          3. Naver Finance Realtime KOSPI API Fetcher
        ★ ETF(kr_069500) 원가나 전종목 횡단면 테이블(raw/krx_stock_daily/kospi_*.parquet)을 지수 레벨로 사용하지 않음.
        """
        _SIGNALS_DIR = _DATA_DIR.parent / 'data' / 'signals'
        if not _SIGNALS_DIR.exists():
            _SIGNALS_DIR = _PROJECT_ROOT / 'data' / 'signals'
        # 1순위: 시계열 KOSPI 지수 Parquet에서 정확한 20일 이동평균 산출
        for ts_path in [
            _DATA_DIR / 'kr_markets' / 'kr_indices' / 'kospi_index.parquet',
            _DATA_DIR / 'kr_markets' / 'kr_indices' / 'kospi.parquet',
            _DATA_DIR / 'kr_markets' / 'index_kospi.parquet',
            _DATA_DIR / 'kr_markets' / 'KOSPI.parquet',
        ]:
            if ts_path.exists():
                try:
                    df_ts = pd.read_parquet(ts_path)
                    c_col = 'close' if 'close' in df_ts.columns else ('Close' if 'Close' in df_ts.columns else ('종가' if '종가' in df_ts.columns else None))
                    if c_col and len(df_ts) >= 20:
                        cs = pd.to_numeric(df_ts[c_col].astype(str).str.replace(',', ''), errors='coerce').dropna()
                        if len(cs) >= 20 and float(cs.iloc[-1]) > 1000 and float(cs.iloc[-1]) < 10000:
                            curr = float(cs.iloc[-1])
                            ma20 = float(cs.tail(20).mean())
                            self._field_quality['kospi'] = 0.9
                            return {'close': curr, 'ma20': ma20}
                except Exception:
                    pass

        # 2순위: KIS Sector Daily ('0001') 또는 pykrx_compat Index OHLCV ('1001')
        _end_str = datetime.now().strftime('%Y%m%d')
        _start_str = (datetime.now() - timedelta(days=45)).strftime('%Y%m%d')
        try:
            from src.data_collection.kis_data_collector import KISDataCollector
            df_kis = KISDataCollector().get_kr_sector_daily('0001', _start_str, _end_str)
            if df_kis is not None and not df_kis.empty:
                c_col = 'Close' if 'Close' in df_kis.columns else ('종가' if '종가' in df_kis.columns else None)
                if c_col:
                    cs = pd.to_numeric(df_kis[c_col], errors='coerce').dropna()
                    if len(cs) >= 1 and 1000.0 < float(cs.iloc[-1]) < 10000.0:
                        curr = float(cs.iloc[-1])
                        ma20 = float(cs.tail(20).mean()) if len(cs) >= 5 else curr
                        self._field_quality['kospi'] = 0.95
                        return {'close': curr, 'ma20': ma20}
        except Exception as e_kis:
            logger.debug(f"  KOSPI KIS Sector API 수집 실패: {e_kis}")

        try:
            from src.data_collection.pykrx_compat import stock as _pykrx_stock
            df_idx = _pykrx_stock.get_index_ohlcv(_start_str, _end_str, '1001')
            if df_idx is not None and not df_idx.empty:
                c_col = '종가' if '종가' in df_idx.columns else ('Close' if 'Close' in df_idx.columns else df_idx.columns[3])
                cs = pd.to_numeric(df_idx[c_col], errors='coerce').dropna()
                if len(cs) >= 1 and 1000.0 < float(cs.iloc[-1]) < 10000.0:
                    curr = float(cs.iloc[-1])
                    ma20 = float(cs.tail(20).mean()) if len(cs) >= 5 else curr
                    self._field_quality['kospi'] = 0.9
                    return {'close': curr, 'ma20': ma20}
        except Exception as e_idx:
            logger.debug(f"  KOSPI pykrx_compat 지수 수집 실패: {e_idx}")

        # 3순위: Naver Finance Realtime KOSPI API Fetcher
        try:
            import requests
            resp = requests.get('https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI', headers={'User-Agent': 'Mozilla/5.0'}, timeout=5).json()
            if 'datas' in resp and resp['datas']:
                data = resp['datas'][0]
                close_val = float(str(data.get('closePriceRaw', '0')).replace(',', ''))
                if close_val > 0:
                    ma20_val = float(self._cache.get('kospi_ma20', 0) or (close_val * 1.002))
                    if ma20_val > 10000 or ma20_val < 1000 or abs(ma20_val - close_val) / max(close_val, 1.0) > 0.15:
                        ma20_val = round(close_val * 1.002, 2)
                    self._field_quality['kospi'] = 0.9
                    logger.info(f"  📊 KOSPI Index (Naver Realtime API): {close_val:,.2f} (MA20: {ma20_val:,.2f})")
                    return {'close': close_val, 'ma20': ma20_val}
        except Exception as e_nav:
            logger.debug(f"  KOSPI Naver Realtime API 수집 실패: {e_nav}")

        logger.error('  ❌ 모든 KOSPI 소스 실패. [Uncertainty Explosion] 예외 발생 강제.')
        self._field_quality['kospi'] = 0.0
        raise RuntimeError("모든 KOSPI 소스 수집 실패")

    def _get_ois(self) -> float:
        """[Phase 84] Overnight Intelligence Score (0~100) (Live Calculation First)."""
        try:
            from src.intelligence.overnight_intelligence import OvernightIntelligenceScore
            intel = OvernightIntelligenceScore()
            res = intel.calculate(include_premarket=True)
            if isinstance(res, dict) and 'ois' in res and res['ois'] != 50.0:
                self._field_quality['ois'] = 0.95
                self._last_ois_details = res
                return float(res['ois'])
        except Exception as _live_ois_e:
            logger.debug(f'Live OvernightIntelligenceScore fallback: {_live_ois_e}')
        for ois_dir in [_DATA_DIR / 'raw' / 'overnight_macro', _DATA_DIR / 'raw' / 'overnight_intel']:
            if ois_dir.exists():
                try:
                    files = sorted([f for f in ois_dir.glob('*.json') if not f.name.endswith('.lock')], reverse=True)
                    if files:
                        mtime = datetime.fromtimestamp(files[0].stat().st_mtime)
                        age_hours = (datetime.now() - mtime).total_seconds() / 3600.0
                        if age_hours <= 12.0:
                            data = json.loads(files[0].read_text())
                            self._field_quality['ois'] = 0.95
                            raw_os = data.get('overnight_score')
                            if isinstance(raw_os, dict) and 'overnight_score' in raw_os:
                                return float(raw_os['overnight_score'])
                            elif isinstance(raw_os, (int, float)):
                                return float(raw_os)
                            ois_val = float(data.get('ois', data.get('score', data.get('ois_score', 50))))
                            return ois_val
                        else:
                            logger.warning(f"  [Phase 84] OIS 파일 폐기 (Stale: {age_hours:.1f}시간 경과 > 12h)")
                except Exception as e:
                    logger.error(f'Critical error encountered in _get_ois: {e}', exc_info=True)
        self._field_quality['ois'] = 0.5
        return 50.0

    def _get_sector_momentum(self) -> Dict:
        """섹터별 모멘텀 — ticker 기반 (S3 호환).

        Returns:
            {ticker: {'momentum_1m': float, 'momentum_3m': float,
                      'momentum_6m': float}, ...}

        데이터 소스 우선순위:
          1. feature_store parquet (ticker.parquet → close 기반 수익률)
          2. kr_markets parquet (kr_ticker.parquet)
          3. raw/sector_indices CSV 폴백 (레거시, sector name key)
        """
        sector_etf_tickers = ['091160', '395160', '139260', '117700', '305720', '371460', '091180', '227550', '091170', '143860', '244580', '464520', '133690', '379800', '381180', '409820', '371160', '411060', '453080', '195930', '192090']
        result = {}
        fs_dir = _DATA_DIR / 'feature_store'
        for ticker in sector_etf_tickers:
            close = None
            for path in [fs_dir / f"{ticker}.parquet", _DATA_DIR / 'kr_markets' / f"kr_{ticker}.parquet", _DATA_DIR / 'kr_markets' / f"{ticker}.parquet"]:
                if path.exists():
                    try:
                        df = pd.read_parquet(path)
                        c_col = 'close' if 'close' in df.columns else ('Close' if 'Close' in df.columns else None)
                        if c_col and len(df) >= 5:
                            close = pd.to_numeric(df[c_col], errors='coerce').dropna()
                            break
                    except Exception as e:
                        logger.warning(f"  [MarketDataBridge] 모멘텀 로드 예외 (스킵): {e}")
                        continue
            if close is None or len(close) < 5:
                continue
            n = len(close)
            mom_1m = float((close.iloc[-1] / close.iloc[-min(21, n)] - 1) * 100) if n >= 5 else 0.0
            mom_3m = float((close.iloc[-1] / close.iloc[-min(63, n)] - 1) * 100) if n >= 21 else 0.0
            mom_6m = float((close.iloc[-1] / close.iloc[-min(126, n)] - 1) * 100) if n >= 63 else 0.0
            result[ticker] = {'momentum_1m': round(mom_1m, 3), 'momentum_3m': round(mom_3m, 3), 'momentum_6m': round(mom_6m, 3)}
        if result:
            logger.debug(f"  섹터 모멘텀: {len(result)}종목 계산 완료")
        else:
            sector_dir = _DATA_DIR / 'raw' / 'sector_indices'
            if sector_dir.exists():
                try:
                    for f in sorted(sector_dir.glob('*.csv'), reverse=True)[:1]:
                        df = pd.read_csv(f)
                        for _, row in df.iterrows():
                            name = row.get('sector', row.get('name', ''))
                            change = row.get('change_pct', row.get('변동률', 0))
                            if name:
                                result[str(name)] = float(change)
                        break
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    logger.error(f'Critical error encountered (removed targeted fallback): {e}', exc_info=True)
                    raise
        return result

    def _get_stock_technicals(self) -> Dict:
        """S1/S3 자산 기술적 지표 (국내 대형주 및 미국 핵심 ETF/빅테크).

        Returns:
            {'005930': {'rsi_14': float, 'macd_hist': float, 'volume_ratio': float}, ...}
        """
        targets = {
            '005930': '삼성전자', 
            '000660': 'SK하이닉스',
            '069500': 'KODEX 200',
            '091160': 'KODEX 반도체',
            'NVDA': 'NVIDIA Corp',
            'QQQ': 'Invesco QQQ Trust',
            'SOXX': 'iShares Semiconductor ETF',
            'XLK': 'Technology Select Sector SPDR',
            'TSLA': 'Tesla Inc',
            'SPY': 'SPDR S&P 500 ETF Trust'
        }
        result = {}
        for ticker, name in targets.items():
            try:
                df = self._load_price_history(ticker)
                if df is None or len(df) < 5:
                    continue
                c_col = 'close' if 'close' in df.columns else 'Close'
                close = pd.to_numeric(df[c_col], errors='coerce').dropna()
                if len(close) < 5:
                    continue
                result[ticker] = {
                    'rsi_14': float(self._calc_rsi(close)) if len(close) >= 14 else 50.0,
                    'macd_hist': float(self._calc_macd_signal(close)) if len(close) >= 20 else 0.0,
                    'volume_ratio': float(self._calc_volume_ratio(df)) if 'volume' in [col.lower() for col in df.columns] and len(df) >= 5 else 1.0,
                    'close': float(close.iloc[-1]),
                    'name': name
                }
            except Exception as e:
                logger.debug(f"  {ticker} technicals 실패: {e}")
        return result

    def _check_staleness(self, last_date: 'datetime', market: str, path_name: str):
        import holidays
        from datetime import datetime
        now = datetime.now()
        hols = list(holidays.US(years=[last_date.year, now.year]).keys()) if market == 'US' else list(holidays.KR(years=[last_date.year, now.year]).keys())
        b_days = pd.bdate_range(start=last_date.date(), end=now.date(), freq='C', holidays=hols)
        elapsed_b_days = max(0, len(b_days) - 1)
        if elapsed_b_days >= 5:
            raise RuntimeError(f"DataStaleException: {path_name} is {elapsed_b_days} business days stale (last date: {last_date.date()}).")
    def _read_us_index(self, symbol: str, etf_alt: str) -> Optional[float]:
        """미국 지수 전일 변동률 (Parquet -> Google Finance Live -> Fallback)."""
        paths_to_check = [
            _DATA_DIR / 'us_stocks' / 'prices' / f'{symbol}.parquet',
            _DATA_DIR / 'us_stocks' / 'prices' / f'{etf_alt}.parquet',
            _DATA_DIR / 'global_markets' / 'us_stocks' / f'{symbol}.parquet', 
            _DATA_DIR / 'global_markets' / 'us_stocks' / f'{etf_alt}.parquet',
            _DATA_DIR / 'us_stocks' / 'prices' / f'{symbol}.csv',
            _DATA_DIR / 'us_stocks' / 'prices' / f'{etf_alt}.csv',
        ]
        for path in paths_to_check:
            if path.exists():
                try:
                    if path.suffix == '.csv':
                        df = pd.read_csv(path)
                    else:
                        df = pd.read_parquet(path)
                    df.columns = [c.lower() for c in df.columns]
                    if len(df) >= 2 and 'close' in df.columns:
                        last_date = pd.to_datetime(df['date'].iloc[-1] if 'date' in df.columns else df.index[-1])
                        try:
                            self._check_staleness(last_date, 'US', path.name)
                            return float((df['close'].iloc[-1] / df['close'].iloc[-2] - 1) * 100)
                        except RuntimeError as e:
                            # Stale file detected -> Attempt live institutional fetch before graceful degradation
                            try:
                                from src.utils.google_finance_collector import GoogleFinanceCollector
                                quote = GoogleFinanceCollector().get_quote_details(symbol)
                                if quote and 'change_pct' in quote and abs(quote['change_pct']) < 30.0:
                                    logger.info(f"  [US Index Live Feed] Google Finance 실시간 변동률 채택 ({symbol}: {quote['change_pct']:+.2f}%)")
                                    return float(quote['change_pct'])
                            except Exception:
                                pass
                            logger.warning(f"[Graceful Degradation] US Data Stale: {e}. Using stale values.")
                            return float((df['close'].iloc[-1] / df['close'].iloc[-2] - 1) * 100)
                except Exception as e:
                    logger.debug(f'US index path read skipped ({path.name}): {e}')
                    continue
        # Direct Live Fallback
        try:
            from src.utils.google_finance_collector import GoogleFinanceCollector
            quote = GoogleFinanceCollector().get_quote_details(symbol)
            if quote and 'change_pct' in quote and abs(quote['change_pct']) < 30.0:
                return float(quote['change_pct'])
        except Exception:
            pass
        return None

    def _load_price_history(self, ticker: str) -> Optional[pd.DataFrame]:
        """종목 가격 히스토리 로드 (국내 및 미국 주식 지원)."""
        from datetime import datetime, timedelta
        import logging
        logger = logging.getLogger(__name__)

        is_us = not str(ticker).isdigit()
        if is_us:
            us_paths = [
                _DATA_DIR / 'us_stocks' / f"{ticker}.csv",
                _DATA_DIR / 'us_stocks' / f"{ticker}.parquet",
                _DATA_DIR / 'us_stocks' / 'prices' / f"{ticker}.csv",
                _DATA_DIR / 'us_stocks' / 'prices' / f"{ticker}.parquet",
                _DATA_DIR / 'global_markets' / 'us_stocks' / f"{ticker}.parquet"
            ]
            for path in us_paths:
                if path.exists():
                    try:
                        if path.suffix == '.csv':
                            df = pd.read_csv(path, index_col=0)
                        else:
                            df = pd.read_parquet(path)
                        if df is not None and len(df) > 0:
                            df.columns = [c.lower() for c in df.columns]
                            return df
                    except Exception as _e_us_load:
                        logger.debug(f"US price load note: {_e_us_load}")
            # Fallback to KIS US OHLCV
            try:
                from src.data_collection.kis_data_collector import KISDataCollector
                collector = KISDataCollector()
                df = collector.get_us_daily_ohlcv(ticker)
                if df is not None and not df.empty:
                    df.columns = [c.lower() for c in df.columns]
                    return df
            except Exception as _e_kis_us:
                logger.debug(f"  [MarketDataBridge] KIS US OHLCV failed for {ticker}: {_e_kis_us}")
            return None
        from datetime import datetime, timedelta
        import logging
        logger = logging.getLogger(__name__)

        for path in [_DATA_DIR / 'kr_markets' / f"kr_{ticker}.parquet", _DATA_DIR / 'kr_markets' / f"{ticker}.parquet"]:
            if path.exists():
                try:
                    df = pd.read_parquet(path)
                    if len(df) > 0:
                        last_date = pd.to_datetime(df['date'].iloc[-1] if 'date' in df.columns else df.index[-1])
                        self._check_staleness(last_date, 'KR', path.name)
                    return df
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    logger.error(f'Critical error encountered (removed targeted fallback): {e}', exc_info=True)
                    raise
        krx_dir = _DATA_DIR / 'raw' / 'krx_stock_daily'
        if krx_dir.exists():
            try:
                csv_files = sorted(krx_dir.glob(f"*{ticker}*.csv"), reverse=True)
                if csv_files:
                    return pd.read_csv(csv_files[0])
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                logger.debug(f"CSV load note: {e}")

        # KIS OpenAPI 실시간 일봉 데이터 수집 (공식 라이브 데이터)
        try:
            from src.data_collection.kis_data_collector import KISDataCollector
            collector = KISDataCollector()
            start_dt = (datetime.now() - timedelta(days=120)).strftime('%Y%m%d')
            end_dt = datetime.now().strftime('%Y%m%d')
            df = collector.get_kr_daily_ohlcv(ticker, start_date=start_dt, end_date=end_dt)
            if df is not None and not df.empty:
                df.columns = [c.lower() for c in df.columns]
                return df
        except Exception as _e_kis_ohlcv:
            logger.debug(f"  [MarketDataBridge] KIS OHLCV load failed for {ticker}: {_e_kis_ohlcv}")

        return None

    def get_current_price(self, ticker: str) -> Optional[float]:
        """실시간 종목 가격 조회 (캐시, 실시간 기술적 지표, 시계열 데이터 결합)."""
        if not ticker:
            return None
        t_str = str(ticker).strip()

        # 1. 메모리 캐시 및 stock_technicals 확인
        if hasattr(self, '_cache') and isinstance(self._cache, dict):
            st = self._cache.get('stock_technicals', {}).get(t_str)
            if st and isinstance(st, dict) and 'close' in st:
                try:
                    val = float(st['close'])
                    if val > 0:
                        return val
                except (ValueError, TypeError):
                    pass

        # 2. results/signal_cache.json 확인
        try:
            from pathlib import Path
            import json
            sc_path = Path(__file__).resolve().parents[2] / 'results' / 'signal_cache.json'
            if sc_path.exists():
                sc_data = json.loads(sc_path.read_text(encoding='utf-8'))
                st = sc_data.get('stock_technicals', {}).get(t_str)
                if st and isinstance(st, dict) and 'close' in st:
                    val = float(st['close'])
                    if val > 0:
                        return val
        except Exception:
            pass

        # 3. _load_price_history 시계열 데이터 확인
        try:
            df = self._load_price_history(t_str)
            if df is not None and not df.empty and 'close' in df.columns:
                closes = df['close'].dropna()
                if len(closes) > 0:
                    val = float(closes.iloc[-1])
                    if val > 0:
                        return val
        except Exception:
            pass

        # 4. CentralDataGateway 실시간 조회 폴백
        try:
            from src.data_collection.central_data_gateway import CentralDataGateway
            cdg = CentralDataGateway()
            res = cdg.get_current_price(t_str)
            if isinstance(res, dict) and res.get('price'):
                return float(res['price'])
            elif isinstance(res, (int, float)) and res > 0:
                return float(res)
        except Exception:
            pass

        return None

    @staticmethod
    def _calc_rsi(close: pd.Series, period: int=14) -> float:
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - 100 / (1 + rs)
        return round(float(rsi.iloc[-1]), 2) if not rsi.empty else 50.0

    @staticmethod
    def _calc_bb_position(close: pd.Series, period: int=20) -> float:
        ma = close.rolling(period).mean()
        std = close.rolling(period).std()
        upper = ma + 2 * std
        lower = ma - 2 * std
        current = close.iloc[-1]
        band_width = upper.iloc[-1] - lower.iloc[-1]
        if band_width > 0:
            return round(float((current - lower.iloc[-1]) / band_width), 3)
        return 0.5

    @staticmethod
    def _calc_macd_signal(close: pd.Series) -> float:
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9).mean()
        hist = macd - signal
        return round(float(hist.iloc[-1]), 4) if not hist.empty else 0.0

    @staticmethod
    def _calc_volume_ratio(df: pd.DataFrame, period: int=20) -> float:
        if 'volume' not in df.columns:
            return 1.0
        avg_vol = df['volume'].tail(period).mean()
        if avg_vol > 0:
            return round(float(df['volume'].iloc[-1] / avg_vol), 2)
        return 1.0

    def _is_cache_fresh(self, max_ttl_seconds: int = 900) -> bool:
        ts = self._cache.get('timestamp', '') or self._cache.get('_generated_at', '')
        if not ts:
            return False
        try:
            cache_dt = datetime.fromisoformat(ts)
            now = datetime.now()
            if cache_dt.tzinfo is not None:
                from datetime import timezone
                now = datetime.now(timezone.utc)
            if cache_dt.date() != now.date():
                return False
            age_sec = (now - cache_dt).total_seconds()
            return age_sec < max_ttl_seconds
        except Exception as e:
            logger.error(f'Critical error encountered (fallback return False): {e}', exc_info=True)
            return False

    def _load_cache(self):
        if self._cache_file.exists():
            try:
                self._cache = json.loads(self._cache_file.read_text())
                self._ensure_root_meta(self._cache)
            except Exception as e:
                logger.error(f'Critical error encountered (fallback self._cache={{}}): {e}', exc_info=True)
                self._cache = {}

    _INDEX_SANITY_BOUNDS = {
        'sp500': (2000.0, 20000.0), 'SP500': (2000.0, 20000.0),
        'nasdaq': (5000.0, 60000.0), 'NASDAQ': (5000.0, 60000.0),
        'sox': (1500.0, 30000.0), 'SOX': (1500.0, 30000.0),
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

    def _save_cache(self, data: Dict) -> None:
        """버전화 캐시 + 3중 저장 구조 (Atomic) + 기존 글로벌/매크로/센서 전 필드 보존 및 ETF 오염 정화."""
        import shutil
        from src.utils.file_ops import atomic_write_json as _atomic_write
        _backup_path = _PROJECT_ROOT / 'results' / 'signal_cache_backup.json'
        primary_path = _PROJECT_ROOT / 'results' / 'signal_cache.json'
        try:
            bounds_map = self._INDEX_SANITY_BOUNDS
            existing = {}
            if primary_path.exists():
                try:
                    existing = json.loads(primary_path.read_text(encoding='utf-8'))
                except Exception:
                    existing = self._cache if isinstance(self._cache, dict) else {}
            if not isinstance(existing, dict):
                existing = {}

            # 1. 기존 캐시 및 신규 data에서 지수 스케일 범위 이탈 오염값(ETF 가격 등) 제거
            for _d_obj in (existing, data):
                for _k in list(_d_obj.keys()):
                    if _k in bounds_map and isinstance(_d_obj.get(_k), (int, float)):
                        _lo, _hi = bounds_map[_k]
                        if not (_lo <= float(_d_obj[_k]) <= _hi):
                            _d_obj.pop(_k, None)

            # 2. MacroRealtimeRefresher / MacroFeatureIntegrator / UnifiedCollector가 기록한 모든 유효 키 보존
            for _pk, _pv in existing.items():
                if _pk in ('timestamp', '_generated_at'):
                    continue
                if _pk not in data:
                    if _pv is not None and _pv != 'N/A' and _pv != '':
                        data[_pk] = _pv
                else:
                    cur_val = data.get(_pk)
                    if (cur_val is None or (isinstance(cur_val, (int, float)) and not isinstance(cur_val, bool) and cur_val == 0) or cur_val == 'N/A' or cur_val == '' or cur_val == []) and _pv not in (None, 'N/A', '', []):
                        if not (isinstance(_pv, (int, float)) and not isinstance(_pv, bool) and _pv == 0):
                            data[_pk] = _pv
            # US ETF 시계열에서 sp500/nasdaq/sox/gold/wti 1D/1M 변동률만 자동 보강 (지수 레벨에 ETF 가격 덮어쓰기 금지)
            for _idx_key, _chg1d_k, _chg1m_k, _tk_sym in [
                ('sp500', 'sp500_change_1d', 'sp500_change_1m', 'SPY'),
                ('nasdaq', 'nasdaq_change_1d', 'nasdaq_change_1m', 'QQQ'),
                ('sox', 'sox_change_1d', 'sox_change_1m', 'SOXX'),
                ('gold_us', 'gold_change_1d', 'gold_change_1m', 'GLD'),
                ('wti', 'wti_change_1d', 'wti_change_1m', 'USO'),
            ]:
                df_u = self._load_price_history(_tk_sym)
                if df_u is not None and len(df_u) >= 2 and 'close' in df_u.columns:
                    cs_u = pd.to_numeric(df_u['close'], errors='coerce').dropna()
                    if len(cs_u) >= 2:
                        if not data.get(_chg1d_k):
                            data[_chg1d_k] = round(float((cs_u.iloc[-1] / cs_u.iloc[-2] - 1.0) * 100.0), 2)
                        if not data.get(_chg1m_k) and len(cs_u) >= 21:
                            data[_chg1m_k] = round(float((cs_u.iloc[-1] / cs_u.iloc[-21] - 1.0) * 100.0), 2)
            # us10y & vix_change_1d / 1m & us_regime / cross_asset_direction 자동 보강
            _mf = data.get('macro_features') if isinstance(data.get('macro_features'), dict) else {}
            if not data.get('us10y') and _mf.get('dgs10'):
                data['us10y'] = round(float(_mf['dgs10']), 3)
            if not data.get('us10y'):
                for _mfn, _mkey, _vkey in [
                    ('global_market_state.json', 'US10Y', 'close'),
                    ('fred_macro.json', 'US_10Y', 'current'),
                ]:
                    _mp = _PROJECT_ROOT / 'data' / 'macro' / _mfn
                    if _mp.exists():
                        try:
                            _md = json.loads(_mp.read_text(encoding='utf-8')).get('indicators', {}).get(_mkey, {})
                            if _md.get(_vkey):
                                data['us10y'] = round(float(_md[_vkey]), 3)
                                if not data.get('us10y_change_1d') and _md.get('change_pct') is not None:
                                    data['us10y_change_1d'] = round(float(_md['change_pct']), 2)
                                break
                        except Exception:
                            pass
            if not data.get('us_regime') or data.get('us_regime') == 'N/A':
                data['us_regime'] = str(data.get('regime') or data.get('current_regime') or 'caution')
            if not data.get('us_regime_confidence'):
                data['us_regime_confidence'] = float(data.get('regime_confidence') or 0.65)
            if not data.get('cross_asset_direction') or data.get('cross_asset_direction') == 'N/A':
                _sp_c = float(data.get('sp500_change_1d') or 0.0)
                _gd_c = float(data.get('gold_change_1d') or 0.0)
                data['cross_asset_direction'] = 'risk_off' if (_sp_c < -0.3 and _gd_c >= 0) else ('risk_on' if _sp_c > 0.3 else 'neutral')
            _vh = data.get('vix_history')
            if isinstance(_vh, list) and len(_vh) >= 2:
                if not data.get('vix_change_1d') and float(_vh[-2]) > 0:
                    data['vix_change_1d'] = round((float(_vh[-1]) / float(_vh[-2]) - 1.0) * 100.0, 2)
                if not data.get('vix_change_1m') and len(_vh) >= 21 and float(_vh[-21]) > 0:
                    data['vix_change_1m'] = round((float(_vh[-1]) / float(_vh[-21]) - 1.0) * 100.0, 2)
            self._ensure_root_meta(data)
        except Exception as _merge_e:
            logger.debug(f"  [Cache] 필드 보존 병합 참고: {_merge_e}")
        try:
            from src.utils.time_utils import now_kst
            today = now_kst().strftime('%Y-%m-%d')
        except Exception:
            from datetime import datetime
            today = datetime.now().strftime('%Y-%m-%d')
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            versioned_dir = _PROJECT_ROOT / 'data' / 'versions' / today
            versioned_dir.mkdir(parents=True, exist_ok=True)
            versioned_path = versioned_dir / 'signal_cache.json'
            _atomic_write(versioned_path, data)
            if primary_path.exists():
                shutil.copy2(primary_path, _backup_path)
            _atomic_write(primary_path, data)
            logger.info(f"  💾 [Cache] 버전 저장 완료: {versioned_path}")
        except Exception as e:
            logger.error(f'  [Cache] 저장 실패: {e} — backup 서비스 유지', exc_info=True)
            if _backup_path.exists() and (not (primary_path := (_PROJECT_ROOT / 'results' / 'signal_cache.json')).exists()):
                shutil.copy2(_backup_path, primary_path)

    def __repr__(self) -> str:
        return f'MarketDataBridge(cache_fresh={self._is_cache_fresh()})'
    _nq_cache_data = {}
    _nq_cache_ts_val = None
    _NQ_CACHE_TTL_SEC = 30 * 60

    def get_nq_futures_change(self, force: bool=False):
        """나스닥 100 선물(NQ=F) 당일 등락률 수집.

        [Phase 10: Alpha Breakthrough] Phase 10-A: S5 야간 선물 필터 전용.
        장 개장 전(08:50 KST 전후) 실시간 등락률을 수집.
        Rate Limit 방어: 30분 TTL 캐시.

        Returns dict with keys: chg_1d_pct, chg_5d_pct, close, source, cached_at
        """
        import json
        from datetime import datetime
        from pathlib import Path
        if not force and self._nq_cache_ts_val is not None and self._nq_cache_data:
            from datetime import datetime as _dt
            age = (_dt.now() - self._nq_cache_ts_val).total_seconds()
            if age < self._NQ_CACHE_TTL_SEC:
                logger.debug('  [Phase 10] NQ 선물: 인메모리 캐시 사용')
                return dict(self._nq_cache_data)
        _project_root = Path(__file__).resolve().parent.parent.parent
        try:
            _mpc_file = _project_root / 'results' / 'macro_proxy.json'
            if _mpc_file.exists():
                _mpc_data = json.loads(_mpc_file.read_text())
                from datetime import datetime as _dt2
                _cached_at = _dt2.fromisoformat(_mpc_data.get('cached_at', '2000-01-01'))
                _age = (_dt2.now() - _cached_at).total_seconds()
                if _age < self._NQ_CACHE_TTL_SEC:
                    _nq = _mpc_data.get('nq_futures', {})
                    if _nq:
                        _r = {'chg_1d_pct': float(_nq.get('chg_1d_pct', 0.0)), 'chg_5d_pct': float(_nq.get('chg_5d_pct', 0.0)), 'close': float(_nq.get('close', 0.0)), 'source': 'macro_proxy_file', 'cached_at': _mpc_data.get('cached_at', '')}
                        self._nq_cache_data = _r
                        from datetime import datetime as _dt3
                        self._nq_cache_ts_val = _dt3.now()
                        logger.debug(f"  [Phase 10] NQ 선물: 파일 캐시 사용 ({_r['chg_1d_pct']:+.2f}%)")
                        return _r
        except Exception as _e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {_e}", exc_info=True)
            logger.debug(f"  [Phase 10] NQ 파일 캐시 로드 실패: {_e}")
        try:
            from src.utils.vendor_multiplexer import VendorMultiplexer
            from datetime import datetime, timedelta
            vm = VendorMultiplexer()
            _start = (datetime.now() - timedelta(days=15)).strftime('%Y-%m-%d')
            _end = datetime.now().strftime('%Y-%m-%d')
            _df = vm.fetch('QQQ', start=_start, end=_end, sources=['kis_api', 'alpha_vantage'])
            if _df is not None and not _df.empty:
                _closes = _df.dropna()
                if len(_closes) >= 2:
                    _latest = float(_closes.iloc[-1])
                    _prev = float(_closes.iloc[-2])
                    _chg1 = (_latest / _prev - 1) * 100 if _prev > 0 else 0.0
                    _chg5 = 0.0
                    if len(_closes) >= 6:
                        _c5 = float(_closes.iloc[-6])
                        _chg5 = (_latest / _c5 - 1) * 100 if _c5 > 0 else 0.0
                    from datetime import datetime as _dt4
                    _r = {'chg_1d_pct': round(_chg1, 4), 'chg_5d_pct': round(_chg5, 4), 'close': round(_latest, 2), 'source': 'vendor_multiplexer_primary', 'cached_at': _dt4.now().isoformat()}
                    self._nq_cache_data = _r
                    self._nq_cache_ts_val = _dt4.now()
                    logger.info(f"  🌐 [Phase 10: SSoT Primary] NQ/QQQ 선물: {_latest:,.0f}pts ({_chg1:+.2f}% 1일)")
                    return _r
        except Exception as _vme:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {_vme}", exc_info=True)
            logger.debug(f"  [Phase 10] NQ/QQQ 멀티플렉서 조회 실패: {_vme}")
        from datetime import datetime as _dt5
        _fb = {'chg_1d_pct': 0.0, 'chg_5d_pct': 0.0, 'close': 0.0, 'source': 'fallback', 'cached_at': _dt5.now().isoformat()}
        logger.debug('  [Phase 10] NQ 선물: fallback(0.0%) 사용')
        return _fb