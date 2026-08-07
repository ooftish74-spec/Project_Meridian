"""
Project_First — Regime Engine
==============================
매크로 레짐 판정 (BULL/CAUTION/BEAR/CRASH).
모든 임계값은 DynamicConfig에서 로드.

Usage:
    from src.intelligence.regime_engine import RegimeEngine
    engine = RegimeEngine()
    result = engine.detect()
    # {'regime': 'caution', 'confidence': 0.65, 'transitions': {...}}
"""
import json
import logging
import numpy as np
import pandas as pd
from datetime import datetime
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List
from config.dynamic_config import DynamicConfig
try:
    from hmmlearn.hmm import GaussianHMM as _GaussianHMM
    _HMMLEARN_OK = True
except ImportError as e:
    _GaussianHMM = None
    _HMMLEARN_OK = False
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = cfg.project_root()

class HMMRegimeLayer:
    """[Phase 74] GaussianHMM 4-state 시장 체제 추론.

    States: 0=Bull 1=Correction 2=Whipsaw 3=Crash
    """
    _STATE_NAMES = {0: 'bull', 1: 'correction', 2: 'whipsaw', 3: 'crash'}
    _MIN_SAMPLES = 60

    def __init__(self, n_states: int=4):
        self._n = n_states
        self._mdl = None
        self._ok = False
        self._log = logging.getLogger(self.__class__.__name__)

    def fit(self, obs_df) -> bool:
        import numpy as np
        if obs_df is None or len(obs_df) < self._MIN_SAMPLES:
            return False
        X = obs_df[['returns', 'volatility']].fillna(0.0).values
        if _HMMLEARN_OK and _GaussianHMM is not None:
            try:
                _hmm_n_iter = int(cfg.get('regime.hmm_n_iter', 100))
                _hmm_rs = int(cfg.get('regime.hmm_random_state', 42))
                m = _GaussianHMM(n_components=self._n, covariance_type='diag', n_iter=_hmm_n_iter, random_state=_hmm_rs)
                m.fit(X)
                self._mdl, self._ok = (m, True)
                self._log.info(f'[Phase 74 HMM] 학습 완료: {self._n}상태 {len(X)}샘플')
                return True
            except Exception as e:
                self._log.warning(f'[Phase 74 HMM] 학습 실패: {e}')
        self._ok = False
        return False

    def predict_proba(self, obs: dict) -> dict:
        import numpy as np
        default = {'bull': 0.6, 'correction': 0.25, 'whipsaw': 0.1, 'crash': 0.05}
        if not self._ok or self._mdl is None:
            return default
        try:
            r = float(obs.get('kospi_return', 0.0))
            v = float(obs.get('kospi_volatility', 15.0)) / 100.0
            pp = self._mdl.predict_proba(np.array([[r, v]]))[0]
            return {self._STATE_NAMES[i]: round(float(p), 4) for i, p in enumerate(pp)}
        except Exception as e:
            self._log.debug(f'[Phase 74 HMM] predict_proba 실패: {e}')
            return default

    def best_state(self, proba: dict) -> str:
        return max(proba, key=proba.get)

class RegimeEngine:
    """매크로 레짐 판정 엔진.

    입력 신호:
      - VIX (공포 지수)
      - KOSPI 추세 (20일/60일 이동평균)
      - 외국인 순매수 (20일 누적)
      - 금리 변동 (US 10Y)
      - 환율 변동 (USD/KRW)

    출력:
      - regime: 'bull' | 'caution' | 'bear' | 'crash'
      - confidence: 0.0 ~ 1.0
      - transitions: 레짐별 전환 확률
    """
    REGIMES = ['bull', 'caution', 'bear', 'crash']
    _REGIME_SCORE_MAP = {'bull': 1.0, 'caution': 0.5, 'bear': -0.5, 'crash': -1.0, 'neutral': 0.0}

    def __init__(self):
        self._state_file = _PROJECT_ROOT / 'results' / 'pipeline_state.json'
        self._history_file = _PROJECT_ROOT / 'results' / 'regime_history.json'
        from src.ml.hmm_regime_model import PredictiveHMMRegimeModel
        self._hmm = PredictiveHMMRegimeModel(n_states=int(cfg.get('regime.hmm_states', 4)))
        self._hmm.load_model()
        self._hmm_blend = float(cfg.get('regime.hmm_blend_weight', 0.4))

    def detect(self) -> Dict:
        """현재 레짐 판정.

        원칙 3: 측정-판정 분리
          _measure() → 순수 데이터 수집 (사실)
          _score()   → 측정값 → 레짐별 점수 변환 (판정 로직)
          _decide()  → 최고 스코어 레짐 선택 (결정)
        """
        measurements = self._measure()
        scores = self._score(measurements)
        if self._hmm.is_trained:
            hmm_features = {'return': measurements.get('kospi_return', 0.0), 'volatility': measurements.get('kospi_volatility', 15.0) / 100.0, 'usdkrw_change': measurements.get('usdkrw', 1350) / measurements.get('usdkrw_prev', 1350) - 1.0, 'vix': measurements.get('vix', 20.0)}
            hmm_pred = self._hmm.predict_current_regime(hmm_features)
            hmm_probs = hmm_pred.get('probabilities', {})
            for regime in self.REGIMES:
                base_score = scores.get(regime, 0.0)
                hmm_prob = hmm_probs.get(regime, 0.0)
                scores[regime] = base_score * (1.0 - self._hmm_blend) + hmm_prob * self._hmm_blend
            logger.info(f'  [Regime] HMM 예측 반영 - HMM Regime: {hmm_pred.get('regime')}')
        regime, confidence = self._decide(scores)
        regime, confidence = self._apply_smoothing(regime, confidence)
        result = {'regime': regime, 'confidence': round(confidence, 3), 'scores': {r: round(s, 3) for r, s in scores.items()}, 'measurements': measurements, 'signals': measurements, 'timestamp': datetime.now().isoformat()}
        prev = self._load_previous_regime()
        prev_regime = prev.get('kr_prev_regime', prev.get('kr_regime', prev.get('regime'))) if prev else None
        if prev_regime and prev_regime != regime:
            try:
                from src.measurement.event_ledger import log_event
                log_event('REGIME', {'from': prev_regime, 'to': regime, 'confidence': round(confidence, 3), 'scores': result['scores']}, source='regime_engine')
            except Exception as e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                logger.debug(f'  EventLedger 기록 실패 (non-critical): {e}')
        try:
            if self._hmm.is_trained:
                result['hmm_state_proba'] = hmm_probs
                result['hmm_regime'] = hmm_pred.get('regime', regime)
                result['hmm_transition'] = hmm_pred.get('transition_probabilities', {})
            else:
                result['hmm_state_proba'] = {'bull': 0.25, 'caution': 0.25, 'bear': 0.25, 'crash': 0.25}
                result['hmm_regime'] = regime
                result['hmm_transition'] = {}
        except Exception as _he:
            logger.error(f'[Phase 89 HMM] 정보 주입 실패: {_he}', exc_info=True)
            result['hmm_state_proba'] = {'bull': 0.25, 'caution': 0.25, 'bear': 0.25, 'crash': 0.25}
            result['hmm_regime'] = regime
        self._save_state(result)
        logger.info(f'  레짐: {regime.upper()} (conf={confidence:.2f})')
        return result

    def _measure(self) -> Dict:
        """순수 데이터 측정 — 판정 로직 없음.

        Returns:
            vix, kospi_trend, kospi_ma20_dist, kospi_ma60_dist,
            kospi_volatility 등 측정값 딕셔너리.
        """
        m = {}
        m['vix'] = self._read_latest_value('vix', default=20.0)
        kospi = self._read_price_series(cfg.get('regime.kospi_benchmark_ticker', '069500'))
        _ma_short = int(cfg.get('regime.ma_short', 20))
        _ma_long = int(cfg.get('regime.ma_long', 60))
        if kospi is not None and len(kospi) >= _ma_long:
            ma20 = kospi.rolling(_ma_short).mean().iloc[-1]
            ma60 = kospi.rolling(_ma_long).mean().iloc[-1]
            cur = kospi.iloc[-1]
            m['kospi_trend'] = 'up' if cur > ma20 > ma60 else 'down' if cur < ma20 < ma60 else 'sideways'
            m['kospi_ma20_dist'] = round((cur / ma20 - 1) * 100, 2)
            m['kospi_ma60_dist'] = round((cur / ma60 - 1) * 100, 2)
        else:
            m['kospi_trend'] = 'sideways'
            m['kospi_ma20_dist'] = 0.0
            m['kospi_ma60_dist'] = 0.0
        if kospi is not None and len(kospi) >= int(cfg.get('regime.vol_min_bars', 25)):
            ret = kospi.pct_change().dropna()
            m['kospi_volatility'] = round(ret.tail(20).std() * np.sqrt(252) * 100, 2)
        else:
            m['kospi_volatility'] = 15.0
        sc = self._load_signal_cache()
        m['usdkrw'] = float(sc.get('usdkrw', 1350))
        m['usdkrw_prev'] = float(sc.get('usdkrw_prev', m['usdkrw']))
        m['vkospi'] = float(sc.get('vkospi', 18))
        m['ois'] = float(sc.get('ois', 50))
        m['macro_composite'] = self._compute_macro_composite(sc)
        return m

    def _score(self, m: Dict) -> Dict[str, float]:
        """측정값 → 레짐별 점수 변환.

        모든 가중치/임계값은 DynamicConfig에서 로드 (M3: 하드코딩 제거).
        """
        w_vix = cfg.get('regime.weight_vix')
        w_trend = cfg.get('regime.weight_trend')
        w_vol = cfg.get('regime.weight_volatility')
        vix_bull = cfg.get('regime.vix_bull_threshold')
        vix_caution = cfg.get('regime.vix_caution_threshold')
        vix_bear = cfg.get('regime.vix_bear_threshold')
        trend_strong_up = cfg.get('regime.trend_strong_up_dist')
        trend_strong_down = cfg.get('regime.trend_strong_down_dist')
        vol_bull = cfg.get('regime.vol_bull_threshold')
        vol_caution = cfg.get('regime.vol_caution_threshold')
        vol_bear = cfg.get('regime.vol_bear_threshold')
        scores = {'bull': 0.0, 'caution': 0.0, 'bear': 0.0, 'crash': 0.0}
        vix = m.get('vix', 20.0)
        if vix < vix_bull:
            scores['bull'] += w_vix
        elif vix < vix_caution:
            scores['caution'] += w_vix
        elif vix < vix_bear:
            scores['bear'] += w_vix
        else:
            scores['crash'] += w_vix
        trend = m.get('kospi_trend', 'sideways')
        ma20_dist = m.get('kospi_ma20_dist', 0)
        if trend == 'up' and ma20_dist > trend_strong_up:
            scores['bull'] += w_trend
        elif trend == 'up':
            scores['caution'] += w_trend * 0.5
            scores['bull'] += w_trend * 0.5
        elif trend == 'down' and ma20_dist < trend_strong_down:
            scores['crash'] += w_trend * 0.5
            scores['bear'] += w_trend * 0.5
        elif trend == 'down':
            scores['bear'] += w_trend
        else:
            scores['caution'] += w_trend
        vol = m.get('kospi_volatility', 15.0)
        if vol < vol_bull:
            scores['bull'] += w_vol
        elif vol < vol_caution:
            scores['caution'] += w_vol
        elif vol < vol_bear:
            scores['bear'] += w_vol
        else:
            scores['crash'] += w_vol
        w_macro = cfg.get('regime.weight_macro', 0.15)
        macro = self._load_macro_features()
        try:
            from src.utils.adaptive_thresholds import VolatilityScaledThreshold
            z_ext = cfg.get('adaptive.z_score_extreme', 1.5)
            p_ext = cfg.get('adaptive.percentile_extreme', 90.0)
        except ImportError as e:
            z_ext, p_ext = (1.5, 90.0)

            class VolatilityScaledThreshold:

                @staticmethod
                def is_extreme(cv, hist, z, p):
                    return abs(cv) > 0.3
        if macro:
            hy = macro.get('fred_hy_spread', 0)
            hy_hist = self._read_macro_history('fred_hy_spread', default_val=0.0)
            if VolatilityScaledThreshold.is_extreme(-hy, [-h for h in hy_hist], z_score_limit=z_ext, percentile_limit=p_ext):
                _w_hy_bull = cfg.get('regime.w_hy_bull', 0.3)
                scores['bull'] += w_macro * _w_hy_bull
            elif VolatilityScaledThreshold.is_extreme(hy, hy_hist, z_score_limit=z_ext, percentile_limit=p_ext):
                _w_hy_bear = cfg.get('regime.w_hy_bear', 0.4)
                _w_hy_crash = cfg.get('regime.w_hy_crash', 0.2)
                scores['bear'] += w_macro * _w_hy_bear
                scores['crash'] += w_macro * _w_hy_crash
            yc_inv = macro.get('yield_curve_inverted', 0)
            if yc_inv:
                _w_yc_caution = cfg.get('regime.w_yc_caution', 0.3)
                _w_yc_bear = cfg.get('regime.w_yc_bear', 0.2)
                scores['caution'] += w_macro * _w_yc_caution
                scores['bear'] += w_macro * _w_yc_bear
            ism = macro.get('cross_ism_signal', 0)
            ism_hist = self._read_macro_history('cross_ism_signal', default_val=0.0)
            if VolatilityScaledThreshold.is_extreme(-ism, [-i for i in ism_hist], z_score_limit=z_ext, percentile_limit=p_ext):
                _w_ism_bear = cfg.get('regime.w_ism_bear', 0.2)
                scores['bear'] += w_macro * _w_ism_bear
            elif VolatilityScaledThreshold.is_extreme(ism, ism_hist, z_score_limit=z_ext, percentile_limit=p_ext):
                _w_ism_bull = cfg.get('regime.w_ism_bull', 0.2)
                scores['bull'] += w_macro * _w_ism_bull
            # [Phase 4] 뉴스 센티멘트 완전 폐기 및 수급(Flow)/OIS(야간 매크로) 정량 모델로 100% 이관
            _ois = m.get('ois', 50.0)
            _prog_net = m.get('program_net_buy', 0)
            
            # 수학적 OIS & 수급 합성(Flow-Macro Risk)
            if _ois < 40 or _prog_net < -5000:
                _w_quant_bear = cfg.get('regime.w_quant_bear', 0.2)
                scores['bear'] += w_macro * _w_quant_bear
                
                # 심각한 매크로 하락 + 대규모 기관 매도 = 크래시 징후 (Expected Gap Risk)
                if _ois < 30 and _prog_net < -10000:
                    scores['crash'] += w_macro * 0.3
            elif _ois > 60 and _prog_net > 5000:
                _w_quant_bull = cfg.get('regime.w_quant_bull', 0.2)
                scores['bull'] += w_macro * _w_quant_bull
        mc = m.get('macro_composite', 0)
        w_mc = cfg.get('regime.weight_macro_composite', 0.2)
        _mc_high = cfg.get('regime.mc_high', 1.5)
        _mc_mid_high = cfg.get('regime.mc_mid_high', 0.5)
        _mc_mid_low = cfg.get('regime.mc_mid_low', -0.5)
        _mc_low = cfg.get('regime.mc_low', -1.5)
        if mc > _mc_high:
            scores['bear'] += w_mc * min(mc / 3.0, 1.0)
            scores['caution'] += w_mc * 0.3
        elif mc > _mc_mid_high:
            scores['caution'] += w_mc * (mc / 1.5)
        elif mc < _mc_low:
            scores['bull'] += w_mc * min(abs(mc) / 3.0, 1.0)
        elif mc < _mc_mid_low:
            scores['bull'] += w_mc * 0.5
        usdkrw = m.get('usdkrw', 1350)
        usdkrw_prev = m.get('usdkrw_prev', usdkrw)
        if usdkrw_prev > 0:
            fx_pct = (usdkrw - usdkrw_prev) / usdkrw_prev * 100
            w_fx = cfg.get('regime.weight_fx_daily', 0.08)
            fx_bear_pct = cfg.get('regime.fx_bear_pct', 1.0)
            fx_caution_pct = cfg.get('regime.fx_caution_pct', 0.5)
            fx_bull_pct = cfg.get('regime.fx_bull_pct', -0.5)
            if fx_pct > fx_bear_pct:
                scores['bear'] += w_fx
            elif fx_pct > fx_caution_pct:
                scores['caution'] += w_fx * 0.5
            elif fx_pct < fx_bull_pct:
                scores['bull'] += w_fx * 0.5
        vkospi = m.get('vkospi', 18)
        w_vk = cfg.get('regime.weight_vkospi', 0.05)
        
        try:
            from src.utils.adaptive_thresholds import VolatilityScaledThreshold
            vk_hist = self._read_macro_history('vkospi')
            if vk_hist and len(vk_hist) >= 20:
                is_crash = VolatilityScaledThreshold.is_extreme(vkospi, vk_hist, z_score_limit=2.0, percentile_limit=95.0)
                is_caution = VolatilityScaledThreshold.is_extreme(vkospi, vk_hist, z_score_limit=1.0, percentile_limit=75.0)
                
                if is_crash:
                    scores['crash'] += w_vk
                    scores['bear'] += w_vk * 0.5
                elif is_caution:
                    scores['caution'] += w_vk
                elif vkospi < float(np.percentile(vk_hist, 25)):
                    scores['bull'] += w_vk
            else:
                if vkospi > 35:
                    scores['crash'] += w_vk
                    scores['bear'] += w_vk * 0.5
                elif vkospi > 25:
                    scores['caution'] += w_vk
                elif vkospi < 14:
                    scores['bull'] += w_vk
        except Exception as e:
            logger.warning(f"  [VKOSPI Adaptive] 실패: {e}")
            if vkospi > 35:
                scores['crash'] += w_vk
                scores['bear'] += w_vk * 0.5
            elif vkospi > 25:
                scores['caution'] += w_vk
            elif vkospi < 14:
                scores['bull'] += w_vk
        return scores

    def _decide(self, scores: Dict[str, float]) -> Tuple[str, float]:
        """최고 스코어 레짐 선택.

        ★ C2-09 FIX: confidence를 총합 대비 비율로 정규화 (이전: 원시 합계값 반환, 1.0 초과 가능)
        """
        best = max(scores, key=scores.get)
        total = sum(scores.values())
        confidence = scores[best] / total if total > 0 else 0.5
        return (best, round(confidence, 3))

    def _apply_smoothing(self, regime: str, confidence: float) -> Tuple[str, float]:
        """전환 스무딩 — 급격한 레짐 전환 방지."""
        smoothing_days = cfg.get('regime.transition_smoothing_days')
        min_conf = cfg.get('regime.confidence_min')
        prev = self._load_previous_regime()
        prev_kr = prev.get('kr_regime', prev.get('regime')) if prev else None
        if prev_kr and prev_kr != regime:
            if confidence < min_conf:
                return (prev_kr, prev.get('kr_regime_confidence', prev.get('confidence', 0.5)))
        return (regime, confidence)

    def _load_previous_regime(self) -> Optional[Dict]:
        """이전 레짐 로드 — pipeline_state.json SSoT."""
        if self._state_file.exists():
            try:
                return json.loads(self._state_file.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at regime_engine.py:465', exc_info=True)
        legacy = _PROJECT_ROOT / 'results' / 'current_regime.json'
        if legacy.exists():
            try:
                return json.loads(legacy.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at regime_engine.py:474', exc_info=True)
        return None

    def _save_state(self, result: Dict):
        """★ pipeline_state.json에 kr_regime 필드로 통합 저장."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            existing = {}
            if self._state_file.exists():
                try:
                    existing = json.loads(self._state_file.read_text())
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.warning('[SILENT_BYPASS] Suppressed exception at regime_engine.py:489', exc_info=True)
            existing['kr_prev_regime'] = existing.get('kr_regime')
            existing['kr_regime'] = result['regime']
            existing['kr_regime_confidence'] = result['confidence']
            existing['kr_regime_scores'] = result['scores']
            existing['kr_regime_measurements'] = result.get('measurements', {})
            existing['kr_regime_updated_at'] = datetime.now().isoformat()
            existing['kr_regime_updated_by'] = 'regime_engine'
            existing['operating_regime'] = self._compute_operating_regime(existing)
            existing['updated_at'] = datetime.now().isoformat()
            atomic_write_json(self._state_file, existing, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            logger.warning(f'  레짐 저장 실패: {e}', exc_info=True)
        try:
            regime_file = _PROJECT_ROOT / 'results' / 'current_regime.json'
            regime_out = {'regime': result['regime'], 'confidence': result['confidence'], 'scores': result['scores'], 'method': 'regime_engine_ssot', 'measurements': result.get('measurements', {}), 'macro_composite': result.get('measurements', {}).get('macro_composite', 0), 'timestamp': datetime.now().isoformat(), 'data_source': 'regime_engine'}
            atomic_write_json(regime_file, regime_out, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            logger.warning(f'  current_regime.json 기록 실패: {e}', exc_info=True)

    def _read_latest_value(self, name: str, default: float=0.0) -> float:
        """신호 값 읽기 (시그널 캐시에서)."""
        cache_file = _PROJECT_ROOT / 'results' / 'signal_cache.json'
        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text())
                return float(data.get(name, default))
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at regime_engine.py:538', exc_info=True)
        return default

    def _read_price_series(self, ticker: str) -> Optional[pd.Series]:
        """parquet에서 가격 시리즈 읽기."""
        parquet = _PROJECT_ROOT / 'data' / 'historical_10y' / f'kr_{ticker}.parquet'
        if parquet.exists():
            try:
                df = pd.read_parquet(parquet)
                close = pd.to_numeric(df['close'], errors='coerce').dropna()
                return close
            except Exception as e:
                logger.error(f'  가격 읽기 실패 ({ticker}): {e}', exc_info=True)
        return None

    def _load_macro_features(self) -> Dict:
        """signal_cache에서 macro_features 로드."""
        cache_file = _PROJECT_ROOT / 'results' / 'signal_cache.json'
        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text())
                return data.get('macro_features', {})
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at regime_engine.py:563', exc_info=True)
        return {}

    def _load_signal_cache(self) -> Dict:
        """signal_cache.json 전체 로드."""
        cache_file = _PROJECT_ROOT / 'results' / 'signal_cache.json'
        if cache_file.exists():
            try:
                return json.loads(cache_file.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at regime_engine.py:575', exc_info=True)
        return {}

    def _read_fx_history(self) -> Optional[pd.Series]:
        """환율 시계열 읽기 — pykrx 또는 data 디렉토리."""
        hist = self._read_macro_history('usdkrw')
        if hist and len(hist) >= 20:
            return pd.Series(hist)
        return None

    def _read_macro_history(self, key: str, default_val: float=0.0) -> List[float]:
        """overnight_macro에서 특정 피처의 시계열 이력을 추출합니다."""
        macro_dir = _PROJECT_ROOT / 'data' / 'raw' / 'overnight_macro'
        if not macro_dir.exists():
            return []
        files = sorted(macro_dir.glob('*.json'))
        if len(files) < 10:
            return []
        values = []
        for f in files[-120:]:
            try:
                data = json.loads(f.read_text())
                if key == 'usdkrw':
                    val = data.get('usdkrw', data.get('fx', {}).get('usdkrw'))
                elif key in ['fred_hy_spread', 'cross_ism_signal', 'yield_curve_inverted']:
                    val = data.get('macro_features', {}).get(key)
                elif key == 'news_llm_sentiment':
                    val = data.get('macro_features', {}).get('news_llm_sentiment', data.get('macro_features', {}).get('news_naver_sentiment'))
                else:
                    val = data.get(key)
                if val is not None:
                    values.append(float(val))
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at regime_engine.py:614', exc_info=True)
        return values

    def _compute_macro_composite(self, sc: Dict) -> float:
        """동적 매크로 합성 점수 — Z-score 기반.

        각 변수의 현재값을 60일 이동평균/표준편차 대비 Z-score로 변환.
        하드코딩 임계값 없이, 정상 범위 대비 이탈도로 판단.

        Returns: -3.0 ~ +3.0 (양수 = risk-on, 음수 = risk-off)
        """
        scores = []
        weights = []
        usdkrw = float(sc.get('usdkrw', 1350))
        fx_series = self._read_fx_history()
        if fx_series is not None and len(fx_series) >= 60:
            try:
                from src.utils.adaptive_thresholds import MultiHorizonEWMA
                ewma_engine = MultiHorizonEWMA()
                fx_mean = ewma_engine.compute(fx_series.tolist(), weights=(0.2, 0.5, 0.3))
            except Exception as e:
                fx_mean = fx_series.tail(60).mean()
            
            fx_std = fx_series.tail(60).std()
            if fx_std > 0:
                fx_z = (usdkrw - fx_mean) / fx_std
                scores.append(fx_z)
                weights.append(cfg.get('regime.weight_fx_comp', 0.25))
        vix = float(sc.get('vix', 20))
        vix_baseline = cfg.get('regime.vix_baseline', 20.0)
        vix_std = cfg.get('regime.vix_baseline_std', 8.0)
        try:
            _vix_hist = self._read_macro_history('vix')
            if _vix_hist and len(_vix_hist) >= 20:
                _arr = np.array([v for v in _vix_hist if v is not None and v > 0])
                if len(_arr) >= 10:
                    vix_baseline = float(np.mean(_arr))
                    vix_std = float(np.std(_arr))
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        vix_z = (vix - vix_baseline) / max(vix_std, 0.1)
        scores.append(vix_z)
        weights.append(cfg.get('regime.weight_vix_comp', 0.2))
        macro = sc.get('macro_features', {})
        hy = macro.get('fred_hy_spread', 0)
        credit_stress = macro.get('credit_stress', 0)
        hy_baseline = cfg.get('regime.hy_baseline', 5.0)
        hy_std_val = cfg.get('regime.hy_baseline_std', 2.0)
        if hy_baseline and hy_std_val:
            hy_z = (hy - hy_baseline) / max(hy_std_val, 0.1)
        else:
            hy_z = hy * 2.0
        credit_z = np.clip(hy_z + credit_stress * 3.0, -3, 3)
        scores.append(credit_z)
        weights.append(cfg.get('regime.weight_credit_comp', 0.15))
        usjp_change = macro.get('cross_usjp_change', 0)
        usjp_z = -usjp_change * 5.0
        scores.append(usjp_z)
        weights.append(cfg.get('regime.weight_usjp_comp', 0.1))
        ois = float(sc.get('ois', 50))
        ois_z = -(ois - 50) / 20.0
        scores.append(ois_z)
        weights.append(cfg.get('regime.weight_ois_comp', 0.1))
        event_adj = float(sc.get('event_confidence_adj', 0))
        event_z = event_adj * 3.0
        scores.append(event_z)
        weights.append(cfg.get('regime.weight_event_comp', 0.1))
        vkospi = float(sc.get('vkospi', 18))
        vk_baseline = cfg.get('regime.vkospi_baseline', 18.0)
        vk_std = cfg.get('regime.vkospi_baseline_std', 6.0)
        vkospi_z = (vkospi - vk_baseline) / max(vk_std, 0.1)
        scores.append(vkospi_z)
        weights.append(cfg.get('regime.weight_vkospi_comp', 0.1))
        if not scores:
            return 0.0
        composite = sum((s * w for s, w in zip(scores, weights))) / sum(weights)
        return max(-3.0, min(3.0, round(composite, 3)))

    def get_current_regime(self) -> str:
        """현재 KR 레짐 반환 (캐시 우선)."""
        prev = self._load_previous_regime()
        if prev:
            return prev.get('kr_regime', prev.get('regime', 'caution'))
        return 'caution'

    @staticmethod
    def _compute_operating_regime(state: Dict) -> Dict:
        """★ Operating Regime 합성 — 자산 그룹별 가중치 적용.

        US/KR 레짐을 수치화하여 자산별 최적 레짐을 결정.
        """
        score_map = RegimeEngine._REGIME_SCORE_MAP
        us = state.get('us_regime', state.get('regime', 'caution'))
        kr = state.get('kr_regime', 'caution')
        us_score = score_map.get(us, 0.0)
        kr_score = score_map.get(kr, 0.0)
        asset_weights = {'kr_index': {'us': 0.3, 'kr': 0.7}, 'kr_single': {'us': 0.2, 'kr': 0.8}, 'global_lev': {'us': 0.8, 'kr': 0.2}}

        def score_to_regime(s):
            if s >= 0.7:
                return 'bull'
            if s >= 0.0:
                return 'caution'
            if s >= -0.7:
                return 'bear'
            return 'crash'
        detail = {}
        for asset_type, w in asset_weights.items():
            composite = w['us'] * us_score + w['kr'] * kr_score
            detail[asset_type] = score_to_regime(composite)
        overall_score = 0.4 * us_score + 0.6 * kr_score
        return {'overall': score_to_regime(overall_score), 'detail': detail, 'us_score': round(us_score, 3), 'kr_score': round(kr_score, 3)}
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    engine = RegimeEngine()
    result = engine.detect()
    logger.info(json.dumps(result, indent=2, ensure_ascii=False))