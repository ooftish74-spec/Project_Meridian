"""
Project Meridian — Regime Detector
===================================
Rule-Based + HMM 앙상블 레짐 감지.

레짐: bull, caution, bear, crash
방법:
  1. Rule-Based (VIX, VKOSPI, KOSPI MA20, 변동성) — 60% 가중치
  2. HMM 2-state (hmmlearn, 있으면) — 40% 가중치

Usage:
    from src.regime.regime_detector import RegimeDetector
    detector = RegimeDetector()
    result = detector.detect(market_data)
"""
import pandas as pd
import json
import logging
import numpy as np
from datetime import datetime
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Any, Dict, List, Optional
logger = logging.getLogger(__name__)
try:
    from hmmlearn.hmm import GaussianHMM
    _HMM_AVAILABLE = True
except ImportError as e:
    _HMM_AVAILABLE = False
    logger.error('  hmmlearn 없음 → Rule-Based만 사용', exc_info=True)

class RegimeDetector:
    """Rule-Based + HMM 앙상블 레짐 감지기."""
    REGIMES = ['bull', 'caution', 'bear', 'crash', 'momentum_surge']
    HMM_STATE_MAP_2 = {0: 'bull', 1: 'bear'}
    HMM_STATE_MAP_4 = {0: 'bull', 1: 'caution', 2: 'bear', 3: 'crash'}

    def __init__(self):
        try:
            from config.dynamic_config import DynamicConfig
            self._cfg = DynamicConfig()
        except ImportError as e:
            self._cfg = None
        try:
            from src.streams.s1_edge.adaptive_threshold import AdaptiveThreshold
            self._adaptive_engine = AdaptiveThreshold()
        except Exception as e:
            logger.debug(f'AdaptiveThreshold load failed: {e}')
            self._adaptive_engine = None
        self._hmm_model = None
        self._hmm_fitted = False
        self._crash_radar_state_file = Path(__file__).resolve().parent.parent.parent / 'results' / 'crash_radar_state.json'
        self._crash_radar_state = self._load_crash_radar_state()

    def _get(self, key: str, default: Any=None) -> Any:
        if self._cfg:
            return self._cfg.get(key, default)
        return default

    def _load_crash_radar_state(self) -> dict:
        """CrashRadar Hysteresis 상태 파일에서 복원."""
        default = {'is_active': False, 'activated_at': None, 'last_crash_prob': 0.0, 'hold_days_elapsed': 0}
        try:
            if self._crash_radar_state_file.exists():
                state = json.loads(self._crash_radar_state_file.read_text(encoding='utf-8'))
                logger.debug(f'  [CrashRadar] 상태 복원: active={state.get('is_active')}, 경과={state.get('hold_days_elapsed')}일')
                return state
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  [CrashRadar] 상태 로드 실패 (초기화): {e}')
        return default

    def _save_crash_radar_state(self) -> None:
        """CrashRadar Hysteresis 상태 파일에 저장."""
        try:
            self._crash_radar_state_file.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._crash_radar_state_file, self._crash_radar_state, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  [CrashRadar] 상태 저장 실패 (비치명적): {e}')

    def _apply_crash_hysteresis(self, raw_result: dict) -> dict:
        """CrashRadar 결과에 Hysteresis 필터 적용.

        브릿지워터 스타일 상태 머신:
          - OFF → ON: crash_prob >= entry_threshold (예: 0.50)
          - ON  → ON: crash_prob >= exit_threshold  (예: 0.35) OR 최소 유지일 미달
          - ON  → OFF: crash_prob < exit_threshold AND 최소 유지일 경과

        이력 현상(Hysteresis) 효과:
          - 진입 임계치 > 해제 임계치: 임계치 근방 Whipsaw 방지
          - 최소 유지일: 단발성 스파이크에 의한 즉각 해제 방지

        Args:
            raw_result: _crash_radar()의 원본 결과

        Returns:
            is_crash_warning이 Hysteresis 적용된 결과
        """
        entry_thresh = float(self._get('regime.crash_radar_warn_threshold', 0.5))
        exit_thresh = float(self._get('regime.crash_radar_exit_threshold', 0.35))
        min_hold_days = int(self._get('regime.crash_radar_min_hold_days', 3))
        crash_prob = raw_result.get('crash_prob', 0.0)
        was_active = self._crash_radar_state.get('is_active', False)
        hold_elapsed = self._crash_radar_state.get('hold_days_elapsed', 0)
        if not was_active:
            if crash_prob >= entry_thresh:
                self._crash_radar_state.update({'is_active': True, 'activated_at': datetime.now().isoformat(), 'last_crash_prob': crash_prob, 'hold_days_elapsed': 0})
                logger.warning(f'  🚨 [CrashRadar Hysteresis] 경보 ON: crash_prob={crash_prob:.3f} >= entry={entry_thresh:.2f}')
                is_warning = True
            else:
                is_warning = False
        else:
            hold_elapsed += 1
            self._crash_radar_state['hold_days_elapsed'] = hold_elapsed
            self._crash_radar_state['last_crash_prob'] = crash_prob
            min_hold_met = hold_elapsed >= min_hold_days
            below_exit = crash_prob < exit_thresh
            if min_hold_met and below_exit:
                self._crash_radar_state.update({'is_active': False, 'activated_at': None, 'last_crash_prob': crash_prob, 'hold_days_elapsed': 0})
                logger.info(f'  ✅ [CrashRadar Hysteresis] 경보 OFF: crash_prob={crash_prob:.3f} < exit={exit_thresh:.2f}, 유지일={hold_elapsed}/{min_hold_days}')
                is_warning = False
            else:
                if below_exit:
                    logger.info(f'  ⏳ [CrashRadar Hysteresis] 경보 유지 (최소 유지일 미달): crash_prob={crash_prob:.3f}, 유지일={hold_elapsed}/{min_hold_days}')
                is_warning = True
        self._save_crash_radar_state()
        result = dict(raw_result)
        result['is_crash_warning'] = is_warning
        result['hysteresis'] = {'was_active': was_active, 'is_active': is_warning, 'hold_days': self._crash_radar_state.get('hold_days_elapsed', 0), 'min_hold_days': min_hold_days, 'entry_thresh': entry_thresh, 'exit_thresh': exit_thresh}
        return result

    def _get_hmm_state_map(self, n_states: int) -> dict:
        """HMM state→regime 매핑을 DynamicConfig에서 동적으로 로드.

        meridian_config.yaml의 regime.hmm_state_map_4 / hmm_state_map_2 참조.
        키 형식: state_0, state_1, ...

        Returns:
            {int: str} 형태의 state→regime 매핑
        """
        try:
            map_key = f'regime.hmm_state_map_{n_states}'
            mapping = {}
            for i in range(n_states):
                regime = self._get(f'{map_key}.state_{i}', None)
                if regime:
                    mapping[i] = str(regime)
            if len(mapping) == n_states:
                logger.debug(f'  [HMM] state_map {n_states}state DynamicConfig 로드: {mapping}')
                return mapping
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  [HMM] state_map DynamicConfig 로드 실패: {e}')
        if n_states == 4:
            return self.HMM_STATE_MAP_4
        return self.HMM_STATE_MAP_2

    def detect(self, market_data: Dict) -> Dict:
        """레짐 감지. (Priority 락 체크 포함)
        try:
            import json
            from datetime import datetime
            state_file = Path(__file__).resolve().parent.parent.parent / 'results' / 'regime_state.json'
            if state_file.exists():
                with open(state_file, 'r') as f:
                    state_data = json.load(f)
                
                # Check Priority & TTL
                if state_data.get('priority', 3) == 1 and state_data.get('ttl_until'):
                    ttl = datetime.fromisoformat(state_data['ttl_until'])
                    if datetime.now() < ttl:
                        logger.warning(f"  🚨 [Priority Lock] 우선순위가 높은(1) 수동/NightWatch 레짐 상태가 발효 중입니다. 남은 시간: {ttl - datetime.now()}")
                        logger.warning(f"  🚨 [Priority Lock] 강제로 {state_data.get('current_state')} 상태를 유지합니다. 일반 앙상블 분석은 기각(Bypass)됩니다.")
                        return {
                            'regime': state_data.get('current_state', 'crash'),
                            'confidence': 1.0,
                            'rule_regime': 'crash',
                            'rule_score': 0.0,
                            'hmm_regime': 'crash',
                            'hmm_state': -1,
                            'method': 'priority_lock_override'
                        }
        except Exception as e:
            logger.error(f"  Priority Lock 검사 중 오류: {e}")


        Args:
            market_data: {
                'signal_cache': {'vix': float, 'vkospi': float, ...},
                'kospi_returns': [float, ...],  # 최근 60일+ 일간 수익률
                'vix_history': [float, ...],    # 최근 60일+ VIX 수준
            }

        Returns:
            {
                'regime': str,          # 'bull', 'caution', 'bear', 'crash'
                'confidence': float,    # 0.0 ~ 1.0
                'rule_regime': str,     # Rule-Based 결과
                'rule_score': float,    # 0~100
                'hmm_regime': str,      # HMM 결과 (없으면 None)
                'hmm_state': int,       # HMM state (0 or 1)
                'method': str,          # 'rule_only' or 'ensemble'
            }
        """
        signal = market_data.get('signal_cache', {})
        critical_keys = ['vix', 'vkospi', 'usdkrw']
        import pandas as pd
        missing_count = sum((1 for k in critical_keys if k not in signal or pd.isna(signal[k])))
        import math
        k_decay = float(self._get('regime.watchdog_decay_k', 0.5))
        data_confidence_score = 100.0 * math.exp(-k_decay * missing_count)
        watchdog_triggered = False
        if data_confidence_score < 75.0:
            logger.error(f'  🚨 [Data Watchdog] 핵심 매크로 데이터 {missing_count}개 누락 (Score: {data_confidence_score:.1f}). Confidence 강제 삭감 대기.')
            watchdog_triggered = True
        rule_result = self._rule_based(signal, market_data)
        crash_radar_result = self._crash_radar(market_data)
        crash_radar_result = self._apply_crash_hysteresis(crash_radar_result)
        if crash_radar_result.get('is_crash_warning'):
            _crash_penalty = float(self._get('regime.crash_radar_score_penalty', 5.0))
            rule_result['score'] = max(0.0, rule_result.get('score', 50.0) - _crash_penalty)
            rule_result['crash_radar_triggered'] = True
            logger.warning(f'  [CrashRadar→Rule] score 패널티 -{_crash_penalty:.0f}점 적용 → score={rule_result['score']:.1f}')
        hmm_result = self._hmm_detect(market_data)
        gi_result = self._compute_gi_matrix(market_data)
        if hmm_result and hmm_result['regime'] is not None:
            regime, confidence = self._ensemble(rule_result, hmm_result, gi_result)
            method = 'ensemble'
        else:
            regime = rule_result['regime']
            confidence = rule_result['confidence']
            method = 'rule_only'
        if gi_result['season'] == 'Deflation' and regime == 'bull':
            logger.info('  [RegimeDetector] G/I Matrix(Deflation)가 HMM/Rule(Bull) 거짓 신호를 차단. Regime → caution')
            regime = 'caution'
            confidence = 0.5
        elif gi_result['season'] == 'Goldilocks' and regime == 'crash':
            logger.info('  [RegimeDetector] G/I Matrix(Goldilocks)가 HMM/Rule(Crash) 거짓 신호를 차단. Regime → caution')
            regime = 'caution'
            confidence = 0.5
        if watchdog_triggered:
            # [S1 Patch] Citadel-style Confidence Degradation
            confidence = min(confidence, 0.3)
            if regime == 'bull':
                logger.info('  [RegimeDetector] 데이터 불확실성으로 인해 Bull → Caution 하향')
                regime = 'caution'
            result = {'regime': regime, 'confidence': round(confidence, 3), 'rule_regime': rule_result['regime'], 'rule_score': rule_result['score'], 'hmm_regime': hmm_result['regime'] if hmm_result else None, 'hmm_state': hmm_result['state'] if hmm_result else -1, 'gi_season': gi_result['season'], 'method': 'watchdog_degraded', 'crash_type': rule_result.get('crash_type'), 'divergence_state': rule_result.get('divergence_state'), 'crash_radar': crash_radar_result}
        else:
            result = {'regime': regime, 'confidence': round(confidence, 3), 'rule_regime': rule_result['regime'], 'rule_score': rule_result['score'], 'hmm_regime': hmm_result['regime'] if hmm_result else None, 'hmm_state': hmm_result['state'] if hmm_result else -1, 'gi_season': gi_result['season'], 'method': method, 'crash_type': rule_result.get('crash_type'), 'divergence_state': rule_result.get('divergence_state'), 'crash_radar': crash_radar_result}
        logger.info(f'  🏷️ Regime: {regime} (conf={confidence:.2f}, method={method})')
        try:
            _mri = self._compute_mri(signal, None)
            _df = self._defense_factor(_mri)
        except Exception as e:
            logger.error(f'  Regime MRI/DF 계산 중 로직 에러: {e}')
            raise
        result['mri'] = round(_mri, 4)
        result['defense_factor'] = round(_df, 4)
        return result

    def _crash_radar(self, market_data: Dict) -> Dict:
        from src.utils.metric_parser import parse_vix, parse_metric
        """CrashRadar — VIX 속도 + 거래량 이상 + 공포지수 복합 분석.

        Rule-Based와 독립적으로 실행되어 Crash 조기 경보.
        3개 신호의 가중합 → Crash Probability Score (0.0~1.0)

        신호:
          ① VIX Velocity: VIX의 변화율 (급등 속도)
          ② Volume Anomaly: 거래량 Z-Score (이상 급증)
          ③ Fear Composite: PCR + VKOSPI 복합 공포지수

        Returns:
            {
                'crash_prob': float,       # 0.0~1.0
                'vix_velocity': float,     # VIX 변화율
                'volume_zscore': float,    # 거래량 Z-Score
                'fear_composite': float,   # 복합 공포지수 (0~1)
                'is_crash_warning': bool,  # 경보 발령 여부
            }
        """
        signal = market_data.get('signal_cache', {})
        result = {'crash_prob': 0.0, 'vix_velocity': 0.0, 'volume_zscore': 0.0, 'fear_composite': 0.0, 'is_crash_warning': False}
        try:
            from src.utils.metric_parser import parse_vix
            vix_now = parse_vix(signal, 0.0)
            vix_history = market_data.get('vix_history', [])
            vix_history_clean = [v for v in vix_history if v and (not np.isnan(float(v)))]
            vix_velocity_score = 0.0
            if vix_history_clean and len(vix_history_clean) >= 6:
                # [S1 Patch] RenTec-style Strict Out-of-Sample
                # 당일 수집된 VIX(vix_history_clean[-1])가 기준점에 포함되어 급등이 희석되는 현상(Look-ahead bias) 제거
                vix_prev_5d = float(np.mean(vix_history_clean[-6:-1]))
                vix_velocity = (vix_now - vix_prev_5d) / max(vix_prev_5d, 1e-09)
                result['vix_velocity'] = round(vix_velocity, 4)
                vel_warn = float(self._get('regime.crash_vix_vel_warn', 0.3))
                vel_alarm = float(self._get('regime.crash_vix_vel_alarm', 0.6))
                if vix_velocity >= vel_alarm:
                    vix_velocity_score = 1.0
                elif vix_velocity >= vel_warn:
                    vix_velocity_score = (vix_velocity - vel_warn) / max(vel_alarm - vel_warn, 1e-09)
            vol_zscore_score = 0.0
            volume_history = market_data.get('volume_history', [])
            vol_clean = [v for v in volume_history if v and (not np.isnan(float(v)))]
            if vol_clean and len(vol_clean) >= 20:
                vol_arr = np.array(vol_clean, dtype=float)
                vol_mean = float(np.mean(vol_arr[:-1]))
                vol_std = float(np.std(vol_arr[:-1]))
                vol_now = vol_arr[-1]
                if vol_std > 1e-09:
                    z = (vol_now - vol_mean) / vol_std
                    result['volume_zscore'] = round(float(z), 4)
                    z_thresh = float(self._get('regime.crash_volume_z_threshold', 3.0))
                    vol_zscore_score = min(1.0, max(0.0, (z - z_thresh) / z_thresh))
            fear_score = 0.0
            pcr = parse_metric(signal, 'options_pcr', 1.0)
            vkospi = parse_metric(signal, 'vkospi', 15.0)
            pcr_normal = float(self._get('regime.crash_pcr_normal', 0.8))
            pcr_extreme = float(self._get('regime.crash_pcr_extreme', 1.5))
            vkospi_normal = float(self._get('regime.crash_vkospi_normal', 15.0))
            vkospi_extreme = float(self._get('regime.crash_vkospi_extreme', 40.0))
            pcr_score = min(1.0, max(0.0, (pcr - pcr_normal) / max(pcr_extreme - pcr_normal, 1e-09)))
            vkospi_score = min(1.0, max(0.0, (vkospi - vkospi_normal) / max(vkospi_extreme - vkospi_normal, 1e-09)))
            fear_score = (pcr_score + vkospi_score) / 2.0
            result['fear_composite'] = round(fear_score, 4)
            w_vix = float(self._get('regime.crash_radar_w_vix', 0.4))
            w_vol = float(self._get('regime.crash_radar_w_vol', 0.3))
            w_fear = float(self._get('regime.crash_radar_w_fear', 0.3))
            crash_prob = w_vix * vix_velocity_score + w_vol * vol_zscore_score + w_fear * fear_score
            result['crash_prob'] = round(min(1.0, crash_prob), 4)
            try:
                _cr_state = self._crash_radar_state
                _hold_days = int(_cr_state.get('hold_days_elapsed', 0)) if isinstance(_cr_state, dict) else 0
                _decay_days = int(self._get('regime.crash_decay_days', 5))
                # [S1 Patch] Bridgewater-style Macro-Conditional Hysteresis
                # 거시 구조(VIX)가 안정화되었을 때만 기하급수적 감쇠 적용
                if _hold_days > _decay_days:
                    if vix_now < 25.0:
                        _excess = _hold_days - _decay_days
                        _decay_factor = 0.85 ** _excess  # 기하급수적 감쇠 (일당 15% 차감)
                        _decayed = result['crash_prob'] * _decay_factor
                        logger.info(f'  [CrashDecay] VIX 안정화(<25.0) 조건 충족. CRASH 지속 초과={_excess}일 → crash_prob {result["crash_prob"]:.3f} → {_decayed:.3f} (지수감쇠 적용)')
                        result['crash_prob'] = round(_decayed, 4)
                    else:
                        logger.info(f'  [CrashDecay] VIX({vix_now:.1f}) >= 25.0 유지중. 기간 경과({_hold_days}일)에도 불구하고 감쇠 보류 (구조적 위기 지속).')
            except Exception as _decay_e:
                logger.error(f'  [CrashDecay] 감쇠 계산 실패 (비치명적): {_decay_e}', exc_info=True)
            warn_thresh = float(self._get('regime.crash_radar_warn_threshold', 0.5))
            result['is_crash_warning'] = crash_prob >= warn_thresh
            if result['is_crash_warning']:
                logger.warning(f'  🚨 [CrashRadar] 경보! crash_prob={crash_prob:.3f} (vix_vel={vix_velocity_score:.3f}, vol_z={vol_zscore_score:.3f}, fear={fear_score:.3f})')
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  CrashRadar 계산 실패 (비치명적): {e}')
        return result

    def _rule_based(self, signal: Dict, market_data: Dict) -> Dict:
        from src.utils.metric_parser import parse_vix, parse_metric
        """이동평균, OIS, VIX, 환율 기반의 룰베이스 레짐 판별."""
        from src.utils.metric_parser import parse_vix
        vix = parse_vix(signal, 20.0)
        vkospi = parse_metric(signal, 'vkospi', 18.0)
        usdkrw = parse_metric(signal, 'usdkrw', 1350.0)
        usdkrw_prev = parse_metric(signal, 'usdkrw_prev', usdkrw)
        score = 50.0
        vix_history = market_data.get('vix_history', [])
        if vix_history and len(vix_history) >= 20:
            vix_history_clean = [v for v in vix_history if v is not None and (not np.isnan(v))]
            if len(vix_history_clean) >= 20:
                vix_bull = np.percentile(vix_history_clean, 60)
                vix_caution = np.percentile(vix_history_clean, 85)
                vix_bear = np.percentile(vix_history_clean, 95)
            else:
                vix_bull = self._get('regime.vix_bull_threshold', 18)
                vix_caution = self._get('regime.vix_caution_threshold', 25)
                vix_bear = self._get('regime.vix_bear_threshold', 35)
        else:
            vix_bull = self._get('regime.vix_bull_threshold', 18)
            vix_caution = self._get('regime.vix_caution_threshold', 25)
            vix_bear = self._get('regime.vix_bear_threshold', 35)
        vix_boost = self._get('regime.rule_vix_boost', 20)
        vix_interp = self._get('regime.rule_vix_interp', 10)
        vix_crash_penalty = self._get('regime.rule_vix_crash_penalty', 25)
        if vix < vix_bull:
            score += vix_boost
        elif vix < vix_caution:
            score += vix_interp * (1 - (vix - vix_bull) / (vix_caution - vix_bull))
        elif vix < vix_bear:
            score -= vix_interp * ((vix - vix_caution) / (vix_bear - vix_caution))
        else:
            score -= vix_crash_penalty
        vkospi_history = market_data.get('vkospi_history', [])
        if vkospi_history and len(vkospi_history) >= 20:
            vkospi_clean = [v for v in vkospi_history if v is not None and (not np.isnan(v))]
            if len(vkospi_clean) >= 20:
                vkospi_low = np.percentile(vkospi_clean, 60)
                vkospi_mid = np.percentile(vkospi_clean, 85)
                vkospi_high = np.percentile(vkospi_clean, 95)
            else:
                vkospi_low = self._get('regime.vol_bull_threshold', 12)
                vkospi_mid = self._get('regime.vol_caution_threshold', 20)
                vkospi_high = self._get('regime.vol_bear_threshold', 30)
        else:
            vkospi_low = self._get('regime.vol_bull_threshold', 12)
            vkospi_mid = self._get('regime.vol_caution_threshold', 20)
            vkospi_high = self._get('regime.vol_bear_threshold', 30)
        vk_boost = self._get('regime.rule_vkospi_boost', 15)
        vk_mild = self._get('regime.rule_vkospi_mild', 5)
        vk_stress = self._get('regime.rule_vkospi_stress', 10)
        vk_crash = self._get('regime.rule_vkospi_crash', 20)
        if vkospi < vkospi_low:
            score += vk_boost
        elif vkospi < vkospi_mid:
            score += vk_mild
        elif vkospi < vkospi_high:
            score -= vk_stress
        else:
            score -= vk_crash
        fx_change = (usdkrw - usdkrw_prev) / usdkrw_prev * 100 if usdkrw_prev > 0 else 0
        fx_risk_threshold = self._get('regime.rule_fx_risk_pct', 1.0)
        fx_risk_penalty = self._get('regime.rule_fx_risk_penalty', 8)
        fx_safe_threshold = self._get('regime.rule_fx_safe_pct', -0.5)
        fx_safe_boost = self._get('regime.rule_fx_safe_boost', 5)
        if fx_change > fx_risk_threshold:
            score -= fx_risk_penalty
        elif fx_change < fx_safe_threshold:
            score += fx_safe_boost
        ois = parse_metric(signal, 'ois', 50.0)
        ois_bull = self._get('regime.rule_ois_bull', 70)
        ois_neutral = self._get('regime.rule_ois_neutral', 50)
        ois_bear = self._get('regime.rule_ois_bear', 30)
        ois_bull_boost = self._get('regime.rule_ois_bull_boost', 10)
        ois_neutral_boost = self._get('regime.rule_ois_neutral_boost', 3)
        ois_bear_penalty = self._get('regime.rule_ois_bear_penalty', 10)
        if ois >= ois_bull:
            score += ois_bull_boost
        elif ois >= ois_neutral:
            score += ois_neutral_boost
        elif ois <= ois_bear:
            score -= ois_bear_penalty
        pcr = parse_metric(signal, 'options_pcr', 1.0)
        pcr_risk_penalty = self._get('regime.pcr_risk_penalty', 15)
        pcr_extreme_th = self._get('regime.pcr_extreme_threshold', 1.5)
        vix_assurance_th = self._get('regime.pcr_vix_assurance', 25.0)
        if hasattr(self, '_adaptive_engine') and self._adaptive_engine:
            dyn_th = self._adaptive_engine.get_last()
            if dyn_th:
                pcr_extreme_th = dyn_th.get('pcr_extreme_threshold', pcr_extreme_th)
                vix_assurance_th = dyn_th.get('vix_assurance_threshold', vix_assurance_th)
                logger.debug(f'  [RegimeDetector] 동적 PCR 임계값 적용: {pcr_extreme_th:.3f}, VIX Assurance: {vix_assurance_th:.1f}')
        pcr_risk_th = max(1.1, pcr_extreme_th - 0.2)
        force_regime = None
        crash_type = None
        if pcr >= pcr_extreme_th:
            if vix >= vix_assurance_th:
                logger.error(f'  🚨 [Options PCR SSoT] 극단적 풋옵션 베팅(PCR={pcr:.2f}) + 시장 패닉(VIX={vix:.1f}) 동반 → Crash 강제 전환 (High Assurance)')
                score -= 50
                force_regime = 'crash'
                crash_type = 'options_panic'
            else:
                logger.warning(f'  ⚠️ [Options PCR SSoT] 극단적 풋옵션 베팅(PCR={pcr:.2f}) 감지되나, VIX({vix:.1f})가 기준치({vix_assurance_th}) 미달. 페널티만 적용.')
                score -= pcr_risk_penalty * 2
        elif pcr >= pcr_risk_th:
            score -= pcr_risk_penalty
        kospi = parse_metric(signal, 'kospi', 2600.0)
        kospi_ma20 = parse_metric(signal, 'kospi_ma20', 2600.0)
        force_regime = None
        crash_type = None
        if kospi > 0 and kospi_ma20 > 0:
            trend_ratio = kospi / kospi_ma20
            # [S1 Patch] Sanity Check: 데이터 스케일 오염 방어막 (0.5 ~ 2.0 범위 밖이면 무시)
            if 0.5 < trend_ratio < 2.0:
                if trend_ratio < 0.95:
                    score -= 40
                    force_regime = 'crash'
                    crash_type = 'flash_crash'
                elif trend_ratio < 0.97:
                    score -= 30
                    force_regime = 'bear'
                elif trend_ratio < 0.99:
                    score -= 20
                elif trend_ratio > 1.02:
                    score += 10
            else:
                logger.warning(f"  ⚠️ KOSPI vs MA20 괴리율 비정상(ratio={trend_ratio:.2f}). 스케일 오염으로 간주하여 패널티 무시.")
        kospi_rets = market_data.get('kospi_returns', [])
        if len(kospi_rets) >= 10:
            recent_10 = kospi_rets[-10:]
            neg_count = sum((1 for r in recent_10 if r < 0))
            cum_ret_10d = sum(recent_10) * 100
            if neg_count >= 8 or cum_ret_10d < -6.0:
                logger.info(f'  [RegimeDetector] Slow Bleed 감지! (10일간 {neg_count}회 음봉, 누적 {cum_ret_10d:.1f}%) → Crash 강제 전환')
                score -= 50
                force_regime = 'crash'
                crash_type = 'slow_bleed'
            elif neg_count >= 6 or cum_ret_10d < -3.0:
                logger.info(f'  [RegimeDetector] 하락 모멘텀 감지 (10일간 {neg_count}회 음봉, 누적 {cum_ret_10d:.1f}%) → Bear 강제 전환')
                score -= 30
                force_regime = force_regime or 'bear'
        export_yoy = parse_metric(signal, 'export_yoy', 0.0)
        macro_cycle = market_data.get('alpha_macro_cycle', 'Expansion')
        argus_scitech = parse_metric(signal, 'argus_scitech', 1.0)
        if force_regime == 'crash':
            if export_yoy < -5.0 or macro_cycle in ('Recession', 'Downturn') or argus_scitech < 0.4:
                crash_type = 'recession'
                logger.info(f'  [RegimeDetector] 펀더멘털 악화 감지 (export={export_yoy:.1f}%, cycle={macro_cycle}) → crash_type = recession')
        score = max(0, min(100, score))
        bull_threshold = self._get('regime.rule_score_bull', 65)
        caution_threshold = self._get('regime.rule_score_caution', 45)
        bear_threshold = self._get('regime.rule_score_bear', 25)
        if score >= bull_threshold:
            regime = 'bull'
        elif score >= caution_threshold:
            regime = 'caution'
        elif score >= bear_threshold:
            regime = 'bear'
        else:
            regime = 'crash'
        if force_regime:
            regime = force_regime
        argus_scitech = parse_metric(signal, 'argus_scitech', 1.0)
        ois_stress = max(0.0, min(1.0, (50 - ois) / 30))
        fx_stress = max(0.0, min(1.0, fx_change / 2.0))
        vix_stress = max(0.0, min(1.0, (vix - 18) / 17))
        cross_asset_stress = ois_stress * 0.4 + fx_stress * 0.4 + vix_stress * 0.2
        bok_rate = parse_metric(signal, 'argus_bok_rate', 0.5)
        semi_cycle = parse_metric(signal, 'argus_semi_cycle', 0.5)
        leading_macro_bad = ois < 40 or bok_rate < 0.4 or semi_cycle < 0.4
        export_yoy = parse_metric(signal, 'export_yoy', 0.0)
        macro_inflation = parse_metric(signal, 'argus_inflation', 0.5)
        lagging_macro_bad = export_yoy < -2.0 or macro_inflation > 0.7 or argus_scitech < 0.5
        market_is_bad = regime in ('bear', 'crash')
        prob_flash_crash = 0.0
        prob_recession = 0.0
        prob_liquidity_rally = 0.0
        prob_recovery = 0.0
        if market_is_bad:
            if crash_type == 'slow_bleed':
                prob_recession = 0.6 + cross_asset_stress * 0.4
                prob_flash_crash = max(0.0, 0.2 - cross_asset_stress)
            elif cross_asset_stress < 0.3 and (not leading_macro_bad):
                prob_flash_crash = 0.8
                prob_recession = 0.1
            elif cross_asset_stress > 0.6 or leading_macro_bad:
                prob_recession = 0.7 + cross_asset_stress * 0.3
                prob_flash_crash = 0.1
            else:
                prob_flash_crash = 0.4
                prob_recession = 0.4
        elif leading_macro_bad:
            prob_liquidity_rally = 0.7
        elif not leading_macro_bad and lagging_macro_bad:
            prob_recovery = 0.7
        total_prob = prob_flash_crash + prob_recession + prob_liquidity_rally + prob_recovery
        if total_prob > 0:
            prob_flash_crash /= total_prob
            prob_recession /= total_prob
            prob_liquidity_rally /= total_prob
            prob_recovery /= total_prob
        states = {'flash_crash': prob_flash_crash, 'hidden_recession': prob_recession, 'liquidity_rally': prob_liquidity_rally, 'recovery': prob_recovery}
        divergence_state = max(states, key=states.get) if total_prob > 0 else 'goldilocks'
        logger.info(f'  [RegimeDetector] Probabilistic Inference: FlashCrash={prob_flash_crash:.2f}, Recession={prob_recession:.2f}, Stress={cross_asset_stress:.2f} → state={divergence_state}')
        confidence = abs(score - 50) / 50
        conf_floor = self._get('regime.rule_confidence_floor', 0.3)
        confidence = max(conf_floor, min(1.0, confidence))
        return {'regime': regime, 'score': round(score, 1), 'confidence': confidence, 'crash_type': crash_type, 'divergence_state': divergence_state, 'prob_flash_crash': prob_flash_crash, 'prob_recession': prob_recession, 'prob_liquidity_rally': prob_liquidity_rally, 'prob_recovery': prob_recovery, 'cross_asset_stress': cross_asset_stress}

    def _hmm_detect(self, market_data: Dict) -> Optional[Dict]:
        """HMM 3-state 감지 (hmm_regime.py 활용).

        Returns:
            regime, state, confidence, transition_probs
        """
        try:
            from src.measurement.hmm_regime import HMMRegimePredictor
            import pandas as pd
            vix_hist = market_data.get('vix_history', [])
            kospi_rets = market_data.get('kospi_returns', [])
            if not vix_hist or not kospi_rets:
                return None
            n = min(len(vix_hist), len(kospi_rets))
            df = pd.DataFrame({'vix': vix_hist[-n:], 'kospi_ret': kospi_rets[-n:]})
            n_hmm_states = int(self._get('regime.hmm_n_states', 2))
            if not hasattr(self, '_hmm_predictor') or not hasattr(self, '_hmm_last_n') or n - self._hmm_last_n > 20:
                predictor = HMMRegimePredictor(n_components=n_hmm_states, lookback_window=min(252, n))
                predictor.fit(df)
                self._hmm_predictor = predictor
                self._hmm_last_n = n
            else:
                predictor = self._hmm_predictor
            probs = predictor.predict_regime_probabilities(df)
            best_regime = max(probs, key=probs.get).replace('_prob', '')
            confidence = probs[f'{best_regime}_prob']
            return {'regime': best_regime, 'state': 0, 'confidence': confidence, 'transition_probs': probs}
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'HMM 감지 오류: {e}')
            return None

    def _detect_transition_signal(self, states: np.ndarray, state_to_regime: Dict, current_state: int, current_regime: str) -> Dict:
        """V자 급등/급락 전환 시그널 감지.

        최근 N일의 state 시퀀스에서:
        - bear/crash → bull 전환: V자 급등 (recovery)
        - bull/caution → bear/crash 전환: 급락 (breakdown)

        Returns:
            {'type': str, 'strength': float, 'window': int}
        """
        window = self._get('regime.transition_window', 10)
        if len(states) < window:
            return {'type': 'none', 'strength': 0}
        recent = states[-window:]
        regime_seq = [state_to_regime.get(int(s), 'caution') for s in recent]
        mid = window // 2
        early = regime_seq[:mid]
        late = regime_seq[mid:]
        regime_score = {'bull': 3, 'caution': 2, 'bear': 1, 'crash': 0}
        early_avg = sum((regime_score.get(r, 2) for r in early)) / max(len(early), 1)
        late_avg = sum((regime_score.get(r, 2) for r in late)) / max(len(late), 1)
        delta = late_avg - early_avg
        strength = round(abs(delta) / 3.0, 3)
        v_threshold = self._get('regime.v_signal_threshold', 0.3)
        if delta > v_threshold:
            return {'type': 'v_recovery', 'strength': strength, 'delta': round(delta, 3), 'window': window, 'early_regime': early[-1] if early else 'unknown', 'late_regime': late[-1] if late else 'unknown'}
        elif delta < -v_threshold:
            return {'type': 'v_breakdown', 'strength': strength, 'delta': round(delta, 3), 'window': window, 'early_regime': early[-1] if early else 'unknown', 'late_regime': late[-1] if late else 'unknown'}
        return {'type': 'none', 'strength': strength, 'delta': round(delta, 3)}

    def _ensemble(self, rule: Dict, hmm: Dict, gi: Dict=None) -> tuple:
        """Rule + HMM + G/I Matrix 앙상블 (가중치 DynamicConfig)."""
        regime_scores = {r: 0.0 for r in self.REGIMES}
        rule_weight = self._get('regime.rule_weight', 0.5)
        hmm_weight = self._get('regime.hmm_weight', 0.35)
        gi_weight = self._get('regime.gi_weight', 0.15)
        regime_scores[rule['regime']] += rule_weight * rule['confidence']
        hmm_regime = hmm['regime']
        if hmm_regime in regime_scores:
            regime_scores[hmm_regime] += hmm_weight * hmm['confidence']
        if gi:
            season = gi['season']
            if season == 'Goldilocks':
                regime_scores['bull'] += gi_weight * gi['confidence']
            elif season == 'Reflation':
                regime_scores['bull'] += gi_weight / 2 * gi['confidence']
                regime_scores['caution'] += gi_weight / 2 * gi['confidence']
            elif season == 'Deflation':
                regime_scores['crash'] += gi_weight * gi['confidence']
            elif season == 'Stagflation':
                regime_scores['bear'] += gi_weight * gi['confidence']
        best_regime = max(regime_scores, key=regime_scores.get)
        best_score = regime_scores[best_regime]
        if rule['regime'] in ('crash', 'bear'):
            return (rule['regime'], rule['confidence'])
        total = max(sum(regime_scores.values()), 0.01)
        return (best_regime, min(1.0, best_score / total))

    def _compute_mri(self, signal_cache: Dict, market_data: Optional[Dict]) -> float:
        """
        VIXY, UUP, IEF(역수) Z-Score를 가중 합산하여 Macro Risk Index(MRI) 산출.
        결측 시 VIX, USDKRW, US10Y로 Fallback.
        """
        import numpy as np
        vixy_w = self._get('regime.mri_vixy_weight', 0.5)
        uup_w = self._get('regime.mri_uup_weight', 0.3)
        ief_w = self._get('regime.mri_ief_weight', 0.2)
        window = int(self._get('regime.mri_ma_window', 30))

        def _get_zscore(ticker, fallback_val, fallback_base, fallback_std):
            hist = signal_cache.get(f'{ticker}_history', [])
            if len(hist) >= window:
                arr = np.array([float(x) for x in hist[-window:]])
                mean = np.mean(arr)
                std = max(np.std(arr), 1e-05)
                return (arr[-1] - mean) / std
            else:
                val = float(signal_cache.get(fallback_val, fallback_base))
                return (val - fallback_base) / fallback_std
        vixy_z = _get_zscore('VIXY', 'vix', 20.0, 5.0)
        usdkrw_base = self._get('regime.mri_usdkrw_baseline', 1300.0)
        usdkrw_std = self._get('regime.mri_usdkrw_std', 50.0)
        uup_z = _get_zscore('UUP', 'usdkrw', usdkrw_base, usdkrw_std)
        us10y_base = self._get('regime.mri_us10y_baseline', 4.0)
        us10y_std = self._get('regime.mri_us10y_std', 0.5)
        ief_hist = signal_cache.get('IEF_history', [])
        if len(ief_hist) >= window:
            arr = np.array([float(x) for x in ief_hist[-window:]])
            ief_z = -((arr[-1] - np.mean(arr)) / max(np.std(arr), 1e-05))
        else:
            us10y = float(signal_cache.get('us10y', us10y_base))
            ief_z = (us10y - us10y_base) / us10y_std
        mri = vixy_z * vixy_w + uup_z * uup_w + ief_z * ief_w
        return float(mri)

    def _defense_factor(self, mri: float) -> float:
        """
        MRI 값을 바탕으로 지수 감쇠(Exponential decay) 방어 계수 산출.
        df = e^{-k * max(0, MRI)} clamp [0.001, 1.0]
        """
        import math
        k = self._get('regime.mri_decay_k', 0.8)
        min_df = self._get('defense.min_factor', 0.001)
        val = math.exp(-k * max(0.0, mri))
        return float(max(min_df, min(1.0, val)))

    def _compute_gi_matrix(self, market_data: Dict) -> Dict:
        from src.utils.metric_parser import parse_vix, parse_metric
        """
        경제 성장(Growth)과 물가(Inflation) 모멘텀을 Z-Score 기반으로 
        계산하여 4계절(4 Seasons) 매트릭스를 반환합니다.
        """
        signal = market_data.get('signal_cache', {})
        kospi_rets = market_data.get('kospi_returns', [])
        hy_spread = parse_metric(signal, 'high_yield_spread', 4.0)
        us10y = parse_metric(signal, 'us10y', 4.0)
        ois = parse_metric(signal, 'ois', 50.0)
        import numpy as np
        kospi = parse_metric(signal, 'kospi', 2600.0)
        kospi_ma20 = parse_metric(signal, 'kospi_ma20', 2600.0)
        # [S1 Patch] Bridgewater-style Macro Trend Oscillator
        # 일간 수익률 노이즈 대신 KOSPI 중장기 추세 이격도를 Z-Score로 변환하여 실물 성장 모멘텀 반영
        if kospi_ma20 > 0:
            trend_ratio = kospi / kospi_ma20
            # 과거 통계상 KOSPI 이격도 표준편차를 약 4~5%로 가정하여 Z-Score 산출
            z_kospi = (trend_ratio - 1.0) / 0.045
        else:
            kospi_1d = parse_metric(signal, 'kospi_change_1d', 0.0)
            z_kospi = kospi_1d / 1.5
        if 'hy_spread_zscore' in signal:
            z_hy = signal['hy_spread_zscore']
        else:
            z_hy = max(-3.0, min(3.0, (hy_spread - 4.0) / 1.5))
        if 'us10y_zscore' in signal:
            z_us10y = signal['us10y_zscore']
        else:
            z_us10y = max(-3.0, min(3.0, (us10y - 4.0) / 0.5))
        z_ois = -(ois - 50.0) / 20.0
        delta_g = z_kospi - z_hy
        delta_i = z_us10y + z_ois
        if delta_g >= 0 and delta_i < 0:
            season = 'Goldilocks'
        elif delta_g >= 0 and delta_i >= 0:
            season = 'Reflation'
        elif delta_g < 0 and delta_i < 0:
            season = 'Deflation'
        else:
            season = 'Stagflation'
        distance = np.sqrt(delta_g ** 2 + delta_i ** 2)
        confidence = min(1.0, distance / 2.0)
        logger.debug(f'  [G/I Matrix] ΔG={delta_g:.2f}, ΔI={delta_i:.2f} → {season} (conf={confidence:.2f})')
        return {'season': season, 'delta_g': delta_g, 'delta_i': delta_i, 'confidence': confidence}

    def get_transition_probs(self) -> Dict:
        """마지막 HMM 전환확률 행렬 조회."""
        if not self._hmm_fitted or self._hmm_model is None:
            return {}
        try:
            mat = self._hmm_model.transmat_
            mapping = getattr(self, '_state_to_regime', {})
            result = {}
            for i in range(mat.shape[0]):
                from_r = mapping.get(i, f'state_{i}')
                result[from_r] = {}
                for j in range(mat.shape[1]):
                    to_r = mapping.get(j, f'state_{j}')
                    result[from_r][to_r] = round(float(mat[i, j]), 4)
            return result
        except (FileNotFoundError, pd.errors.EmptyDataError):
            return {}
        except Exception as e:
            logger.error(f'  역사적 변동성 데이터 로드 중 에러: {e}')
            raise

    def get_regime_probabilities(self, market_data: Dict) -> Dict[str, float]:
        """HMM 상태별 확률 딕셔너리 반환 — Smart Wallet 전용 인터페이스.

        ★ Volatility-Scaled Merton-Kelly 모델 (2026-07-18)
        단일 레짐 문자열 대신 연속 확률 배열을 제공하여 Capital Allocator가
        if/else 하드코딩 없이 f_long 방정식을 연속 곡선으로 계산할 수 있도록 설계.

        앙상블 소스 (3-Way Fusion):
          ① CrashRadar crash_prob        (가중치 40%)
          ② HMM 확률 (있을 때)           (가중치 40%)
          ③ Rule-Based score 0~100 변환  (가중치 20%)

        Returns:
            {
                'normal': float,  # bull + caution 합산 (0~1)
                'bear':   float,  # 약세 확률 (0~1)
                'crash':  float,  # 폭락 확률 (0~1)
            }
            합이 반드시 1.0 (확률 정규화 보장).
            절대 raise 없음 — 모든 예외는 Fallback으로 처리.
        """
        try:
            result = self.detect(market_data)
            radar = result.get('crash_radar', {})
            p_crash_radar = float(radar.get('crash_prob', 0.0))
            hmm_regime = result.get('hmm_regime')
            hmm_conf = float(result.get('confidence', 0.5))
            p_crash_hmm = 0.0
            p_bear_hmm = 0.0
            if hmm_regime == 'crash':
                p_crash_hmm = hmm_conf
            elif hmm_regime == 'bear':
                p_bear_hmm = hmm_conf
            elif hmm_regime in ('caution',):
                p_bear_hmm = hmm_conf * 0.5
            rule_score = float(result.get('rule_score', 50.0))
            rule_norm = max(0.0, min(1.0, rule_score / 100.0))
            p_crash_rule = max(0.0, 1.0 - 2.0 * rule_norm)
            p_bear_rule = max(0.0, 1.0 - 2.0 * rule_norm) * 0.5 if rule_norm < 0.5 else 0.0
            w_radar_raw = float(self._get('regime.smart_w_radar', 0.4))
            w_hmm_raw = float(self._get('regime.smart_w_hmm', 0.4))
            w_rule_raw = float(self._get('regime.smart_w_rule', 0.2))
            w_sum = w_radar_raw + w_hmm_raw + w_rule_raw
            if w_sum > 0:
                w_radar = w_radar_raw / w_sum
                w_hmm = w_hmm_raw / w_sum
                w_rule = w_rule_raw / w_sum
            else:
                w_radar, w_hmm, w_rule = (0.4, 0.4, 0.2)
            p_crash_raw = w_radar * p_crash_radar + w_hmm * p_crash_hmm + w_rule * p_crash_rule
            p_bear_raw = w_hmm * p_bear_hmm + w_rule * p_bear_rule
            p_crash = min(1.0, max(0.0, p_crash_raw))
            p_bear = min(1.0, max(0.0, p_bear_raw))
            total_risk = p_crash + p_bear
            if total_risk > 1.0:
                p_crash /= total_risk
                p_bear /= total_risk
            p_normal = max(0.0, 1.0 - p_crash - p_bear)
            probs = {'normal': round(p_normal, 4), 'bear': round(p_bear, 4), 'crash': round(p_crash, 4)}
            logger.debug(f'  [SmartWallet] regime_probs → normal={p_normal:.3f}, bear={p_bear:.3f}, crash={p_crash:.3f}')
            return probs
        except Exception as e:
            logger.warning(f'  [SmartWallet] get_regime_probabilities 실패 → Fallback: {e}')
            return {'normal': 0.5, 'bear': 0.3, 'crash': 0.2}

    def __repr__(self) -> str:
        hmm_status = '✅ fitted' if self._hmm_fitted else '❌ not fitted'
        return f'RegimeDetector(hmm={hmm_status})'