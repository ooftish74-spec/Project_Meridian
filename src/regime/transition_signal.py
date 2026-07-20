"""
Transition Signal Detector — V자 급등/급락 시그널 → 파이프라인 연결
===================================================================

Medallion Upgrade Phase 3-B: HMM 레짐 전환 시그널.

HMM 전환확률 + IntradayRegime 반등 감지를 통합하여:
  1. V자 급등 시그널 → 노출도 확대 + 방어 비중 축소
  2. V자 급락 시그널 → 노출도 축소 + 방어 비중 확대
  3. 급전환 감지 → PortfolioOptimizer에 regime_change 트리거

ExposureOrchestrator, PortfolioOptimizer와 자동 연결.

Usage:
    from src.regime.transition_signal import TransitionSignalDetector
    tsd = TransitionSignalDetector()
    signal = tsd.detect(market_data)
    # → ExposureOrchestrator/PortfolioOptimizer 자동 반영
"""
import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'

class TransitionSignalDetector:
    """V자 급등/급락 통합 시그널 감지 + 파이프라인 트리거.

    데이터 소스:
      1. RegimeDetector (HMM 전환확률)
      2. IntradayRegimeDetector (장중 CUSUM 반등)
      3. 가격 모멘텀 (수익률 가속/감속)
    """

    def __init__(self):
        self._signal_history: List[Dict] = []

    def detect(self, market_data: Dict=None) -> Dict:
        """통합 전환 시그널 감지.

        Args:
            market_data: RegimeDetector에 전달할 시장 데이터
                         (없으면 파일에서 로드)

        Returns:
            {
                'signal_type': 'v_recovery' | 'v_breakdown' | 'none',
                'strength': float (0~1),
                'sources': {...},
                'exposure_adjustment': float,
                'trigger_rebalance': bool,
            }
        """
        sources = {}
        hmm_signal = self._get_hmm_signal(market_data)
        sources['hmm'] = hmm_signal
        intraday_signal = self._get_intraday_signal()
        sources['intraday'] = intraday_signal
        momentum_signal = self._get_momentum_signal(market_data)
        sources['momentum'] = momentum_signal
        result = self._aggregate(sources)
        result['sources'] = sources
        result['timestamp'] = datetime.now().isoformat()
        if result['signal_type'] != 'none':
            self._signal_history.append({'timestamp': result['timestamp'], 'signal_type': result['signal_type'], 'strength': result['strength']})
            logger.info(f'  ⚡ TransitionSignal: {result['signal_type']} (strength={result['strength']:.2f}, exposure_adj={result['exposure_adjustment']:.2f})')
        self._save_result(result)
        return result

    def _get_hmm_signal(self, market_data: Dict=None) -> Dict:
        """HMM 전환확률 기반 시그널."""
        try:
            from src.regime.regime_detector import RegimeDetector
            detector = RegimeDetector()
            if market_data is None:
                market_data = self._load_market_data()
            result = detector.detect(market_data)
            v_signal = {}
            trans_probs = {}
            if 'hmm_regime' in result and result.get('method') == 'ensemble':
                trans_probs = detector.get_transition_probs()
                v_signal = {'regime': result.get('hmm_regime'), 'transition_probs': trans_probs}
            signal_type = 'none'
            strength = 0
            if trans_probs:
                current_regime = result.get('regime', 'caution')
                if current_regime in ('bear', 'crash'):
                    bull_prob = trans_probs.get(current_regime, {}).get('bull', 0)
                    caution_prob = trans_probs.get(current_regime, {}).get('caution', 0)
                    recovery_prob = bull_prob + caution_prob
                    threshold = cfg.get('regime.hmm_recovery_prob_threshold', 0.3)
                    if recovery_prob > threshold:
                        signal_type = 'v_recovery'
                        strength = min(1.0, recovery_prob)
                elif current_regime in ('bull', 'caution'):
                    crash_prob = trans_probs.get(current_regime, {}).get('crash', 0)
                    bear_prob = trans_probs.get(current_regime, {}).get('bear', 0)
                    breakdown_prob = crash_prob + bear_prob
                    threshold = cfg.get('regime.hmm_breakdown_prob_threshold', 0.25)
                    if breakdown_prob > threshold:
                        signal_type = 'v_breakdown'
                        strength = min(1.0, breakdown_prob)
            return {'signal_type': signal_type, 'strength': round(strength, 3), 'regime': result.get('regime', 'caution'), 'method': result.get('method', 'unknown')}
        except Exception as e:
            logger.debug(f'  HMM signal 실패: {e}')
            return {'signal_type': 'none', 'strength': 0}

    def _get_intraday_signal(self) -> Dict:
        """IntradayRegimeDetector 반등 시그널."""
        try:
            f = _RESULTS / 'intraday_regime.json'
            if not f.exists():
                return {'signal_type': 'none', 'strength': 0}
            data = json.loads(f.read_text())
            regime = data.get('regime', 'normal')
            recovery = data.get('recovery', {})
            if regime == 'recovery' and recovery.get('detected'):
                return {'signal_type': 'v_recovery', 'strength': recovery.get('strength', 0), 'regime': regime}
            elif regime in ('crisis', 'high_vol'):
                crisis_str = cfg.get('regime.intraday_crisis_strength', 0.5)
                highvol_str = cfg.get('regime.intraday_highvol_strength', 0.3)
                return {'signal_type': 'v_breakdown', 'strength': crisis_str if regime == 'crisis' else highvol_str, 'regime': regime}
            return {'signal_type': 'none', 'strength': 0, 'regime': regime}
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return {'signal_type': 'none', 'strength': 0}

    def _get_momentum_signal(self, market_data: Dict=None) -> Dict:
        """가격 모멘텀 반전 시그널.

        최근 수익률의 가속/감속 패턴으로 전환점 감지.
        """
        try:
            returns = []
            if market_data:
                returns = market_data.get('kospi_returns', [])
            if not returns:
                cache_f = _RESULTS / 'signal_cache.json'
                if cache_f.exists():
                    cache = json.loads(cache_f.read_text())
                    returns = cache.get('kospi_returns', [])
            if len(returns) < 10:
                return {'signal_type': 'none', 'strength': 0}
            window = cfg.get('regime.momentum_reversal_window', 5)
            recent = returns[-window:]
            prev = returns[-2 * window:-window]
            if not prev:
                return {'signal_type': 'none', 'strength': 0}
            recent_avg = sum(recent) / len(recent)
            prev_avg = sum(prev) / len(prev)
            delta = recent_avg - prev_avg
            threshold = cfg.get('regime.momentum_reversal_threshold', 0.005)
            if prev_avg < 0 and recent_avg > 0 and (delta > threshold):
                norm = cfg.get('regime.momentum_norm_divisor', 0.02)
                strength = min(1.0, abs(delta) / norm)
                return {'signal_type': 'v_recovery', 'strength': round(strength, 3), 'prev_avg': round(prev_avg, 6), 'recent_avg': round(recent_avg, 6)}
            elif prev_avg > 0 and recent_avg < 0 and (delta < -threshold):
                norm = cfg.get('regime.momentum_norm_divisor', 0.02)
                strength = min(1.0, abs(delta) / norm)
                return {'signal_type': 'v_breakdown', 'strength': round(strength, 3), 'prev_avg': round(prev_avg, 6), 'recent_avg': round(recent_avg, 6)}
            return {'signal_type': 'none', 'strength': 0}
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return {'signal_type': 'none', 'strength': 0}

    def _get_current_vix(self) -> float:
        """현재 VIX 값을 가져옵니다."""
        market_data = self._load_market_data()
        vix_history = market_data.get('vix_history', [])
        if vix_history and len(vix_history) > 0:
            return float(vix_history[-1])
        return 20.0

    def _aggregate(self, sources: Dict) -> Dict:
        """3개 소스를 가중 합산하여 최종 시그널 결정."""
        w_hmm = cfg.get('regime.transition_weight_hmm', 0.45)
        w_intraday = cfg.get('regime.transition_weight_intraday', 0.3)
        w_momentum = cfg.get('regime.transition_weight_momentum', 0.25)
        recovery_score = 0
        breakdown_score = 0
        for name, weight in [('hmm', w_hmm), ('intraday', w_intraday), ('momentum', w_momentum)]:
            src = sources.get(name, {})
            sig_type = src.get('signal_type', 'none')
            strength = src.get('strength', 0)
            if sig_type == 'v_recovery':
                recovery_score += weight * strength
            elif sig_type == 'v_breakdown':
                breakdown_score += weight * strength
        threshold = cfg.get('regime.transition_signal_threshold', 0.2)
        current_vix = self._get_current_vix()
        vix_scalar = max(0.5, min(1.0, 20.0 / current_vix)) if current_vix > 0 else 1.0
        if recovery_score > threshold and recovery_score > breakdown_score:
            base_boost = cfg.get('regime.recovery_exposure_boost_base', 1.05)
            max_boost = cfg.get('regime.recovery_exposure_boost_max', 1.5)
            dynamic_boost = base_boost + (max_boost - base_boost) * recovery_score * vix_scalar
            return {'signal_type': 'v_recovery', 'strength': round(recovery_score, 3), 'exposure_adjustment': round(dynamic_boost, 3), 'trigger_rebalance': recovery_score > threshold * cfg.get('regime.trigger_multiplier', 1.5)}
        elif breakdown_score > threshold and breakdown_score > recovery_score:
            base_cut = cfg.get('regime.breakdown_exposure_cut_base', 0.8)
            min_cut = cfg.get('regime.breakdown_exposure_cut_min', 0.3)
            inverse_vix_scalar = max(1.0, current_vix / 20.0) if current_vix > 0 else 1.0
            dynamic_cut = base_cut - (base_cut - min_cut) * breakdown_score * inverse_vix_scalar
            dynamic_cut = max(min_cut, min(base_cut, dynamic_cut))
            return {'signal_type': 'v_breakdown', 'strength': round(breakdown_score, 3), 'exposure_adjustment': round(dynamic_cut, 3), 'trigger_rebalance': breakdown_score > threshold * 1.5}
        return {'signal_type': 'none', 'strength': 0, 'exposure_adjustment': 1.0, 'trigger_rebalance': False}

    def _load_market_data(self) -> Dict:
        """파일에서 시장 데이터 로드."""
        data = {'signal_cache': {}, 'kospi_returns': [], 'vix_history': []}
        try:
            cache_f = _RESULTS / 'signal_cache.json'
            if cache_f.exists():
                cache = json.loads(cache_f.read_text())
                data['signal_cache'] = cache
                vix = cache.get('vix', cache.get('VIX', {}).get('value', 20))
                if isinstance(vix, list):
                    data['vix_history'] = vix
                else:
                    data['vix_history'] = [float(vix)] * 60
        except Exception as _e_ts:
            logger.debug(f'  [transition_signal] 전환 시그널 실패: {_e_ts}')
        return data

    def _save_result(self, result: Dict) -> None:
        """결과 저장."""
        try:
            out = _RESULTS / 'transition_signal.json'
            out.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        except Exception as _e_ts2:
            logger.debug(f'  [transition_signal] 시그널 저장 실패: {_e_ts2}')

    def get_signal_history(self, n: int=20) -> List[Dict]:
        """최근 시그널 이력."""
        return self._signal_history[-n:]