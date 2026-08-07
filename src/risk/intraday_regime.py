"""
Intraday Regime Detector — 장중 레짐 전환 탐지
================================================

5분봉 데이터로 장중 레짐 전환을 실시간 감지:
  - Realized Volatility (이동 표준편차)
  - 거래량 서지 탐지
  - CUSUM 알고리즘으로 변화점 탐지
  - 누적 손실 기반 위기 감지

Usage:
    from src.risk.intraday_regime import IntradayRegimeDetector
    detector = IntradayRegimeDetector()
    result = detector.update(price_change_pct=-0.5, volume=1500000)
"""
import json
import logging
import math
from collections import deque
from src.utils.file_ops import atomic_write_json

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except ImportError as e:
    _cfg = None

class IntradayRegimeDetector:
    """장중 레짐 전환을 감지하여 실시간 리스크 조정.

    방법:
    - 5분봉 수익률의 이동 표준편차 (realized vol)
    - 거래량 서지 탐지
    - KOSPI 급락/급등 감지
    - CUSUM 알고리즘으로 변화점 탐지
    """

    def __init__(self, vol_window: int=None, surge_threshold: float=None):
        """
        Args:
            vol_window: 변동성 윈도우 (12 × 5min = 1시간)
            surge_threshold: 거래량 서지 배수
        """
        self._vol_window = vol_window or (_cfg.get('regime.intraday_vol_window', 12) if _cfg else 12)
        self._surge_threshold = surge_threshold or (_cfg.get('regime.intraday_surge_threshold', 3.0) if _cfg else 3.0)
        self._returns_buffer: deque = deque(maxlen=100)
        self._volume_buffer: deque = deque(maxlen=100)
        self._cusum_pos: float = 0.0
        self._cusum_neg: float = 0.0
        self._cusum_threshold = _cfg.get('regime.cusum_threshold', 0.03) if _cfg else 0.03
        self._baseline_vol: float = 0.01
        self._current_regime: str = 'normal'
        self._regime_history: List[Dict] = []

    def update(self, price_change_pct: float, volume: float=0, timestamp: Optional[str]=None) -> Dict:
        """5분봉 데이터로 업데이트.

        Args:
            price_change_pct: 5분봉 수익률 (%, 예: -0.3)
            volume: 5분봉 거래량
            timestamp: 타임스탬프 (optional)

        Returns:
            regime judgment dict
        """
        ret = price_change_pct / 100.0
        self._returns_buffer.append(ret)
        if volume > 0:
            self._volume_buffer.append(volume)
        result = self.measure()
        judgment = self.judge(result)
        if judgment['regime'] != self._current_regime:
            old = self._current_regime
            self._current_regime = judgment['regime']
            self._regime_history.append({'timestamp': timestamp or datetime.now().isoformat(), 'from': old, 'to': self._current_regime, 'trigger': judgment.get('trigger', '')})
            logger.warning(f'  ⚠️ Intraday regime shift: {old} → {self._current_regime} (trigger: {judgment.get('trigger', '')})')
        return judgment

    def measure(self) -> Dict:
        """현재 장중 상태 측정."""
        if len(self._returns_buffer) < 3:
            return {'realized_vol': 0, 'annualized_vol': 0, 'cusum_signal': False, 'volume_surge': False, 'cumulative_return': 0}
        recent = list(self._returns_buffer)[-self._vol_window:]
        mean_r = sum(recent) / len(recent)
        var_r = sum(((r - mean_r) ** 2 for r in recent)) / len(recent)
        realized_vol = math.sqrt(var_r) if var_r > 0 else 0
        last_return = self._returns_buffer[-1]
        self._cusum_pos = max(0, self._cusum_pos + last_return - self._baseline_vol * 0.5)
        self._cusum_neg = max(0, self._cusum_neg - last_return - self._baseline_vol * 0.5)
        cusum_signal = self._cusum_pos > self._cusum_threshold or self._cusum_neg > self._cusum_threshold
        volume_surge = False
        if len(self._volume_buffer) >= self._vol_window:
            recent_vol = list(self._volume_buffer)[-self._vol_window:]
            avg_vol = sum(recent_vol) / len(recent_vol)
            if avg_vol > 0 and self._volume_buffer[-1] > avg_vol * self._surge_threshold:
                volume_surge = True
        cumulative = sum(list(self._returns_buffer)[-self._vol_window:])
        return {'realized_vol': round(realized_vol, 6), 'annualized_vol': round(realized_vol * math.sqrt(252 * 78), 4), 'cusum_pos': round(self._cusum_pos, 6), 'cusum_neg': round(self._cusum_neg, 6), 'cusum_signal': cusum_signal, 'volume_surge': volume_surge, 'cumulative_return': round(cumulative, 6), 'n_observations': len(self._returns_buffer)}

    def judge(self, measurement: Dict) -> Dict:
        """레짐 판정 (급락 + V자 급등 양방향).

        Returns:
            {'regime': str, 'exposure_adjustment': float, ...}
        """
        vol = measurement.get('annualized_vol', 0)
        cusum = measurement.get('cusum_signal', False)
        cum_ret = measurement.get('cumulative_return', 0)
        vol_surge = measurement.get('volume_surge', False)
        recovery = self._detect_recovery(measurement)
        trigger = ''
        crisis_threshold = _cfg.get('regime.intraday_crisis_cum_ret', -0.02) if _cfg else -0.02
        stress_threshold = _cfg.get('regime.intraday_stress_cum_ret', -0.01) if _cfg else -0.01
        if cum_ret < crisis_threshold and (cusum or vol_surge):
            regime = 'crisis'
            trigger = f'cum_ret={cum_ret:.3f}, cusum={cusum}, vol_surge={vol_surge}'
        elif vol > (_cfg.get('regime.intraday_high_vol', 0.3) if _cfg else 0.3) or cusum:
            regime = 'high_vol'
            trigger = f'vol={vol:.2f}, cusum={cusum}'
        elif cum_ret < stress_threshold:
            regime = 'stress'
            trigger = f'cum_ret={cum_ret:.3f}'
        elif recovery['detected']:
            regime = 'recovery'
            trigger = f'v_recovery: strength={recovery['strength']:.2f}'
        else:
            regime = 'normal'
        exposure_adj_map = {'normal': 1.0, 'recovery': _cfg.get('regime.intraday_recovery_exposure', 1.15) if _cfg else 1.15, 'stress': _cfg.get('regime.intraday_stress_exposure', 0.7) if _cfg else 0.7, 'high_vol': _cfg.get('regime.intraday_highvol_exposure', 0.5) if _cfg else 0.5, 'crisis': _cfg.get('regime.intraday_crisis_exposure', 0.2) if _cfg else 0.2}
        result = {'regime': regime, 'exposure_adjustment': exposure_adj_map.get(regime, 1.0), 'trigger': trigger, 'current_regime': self._current_regime, 'n_transitions': len(self._regime_history), 'recovery': recovery, 'timestamp': datetime.now().isoformat()}
        try:
            atomic_write_json((_RESULTS / 'intraday_regime.json'),  result, indent=2, default=str)
        except Exception as _e0:
            logger.critical(f'  [intraday_regime] 장중 레짐 결과 저장: {_e0}', exc_info=True)
        try:
            _ps_file = _RESULTS / 'pipeline_state.json'
            if _ps_file.exists():
                _ps = json.loads(_ps_file.read_text())
                _ps['intraday_regime'] = regime
                _ps['intraday_updated_at'] = datetime.now().isoformat()
                atomic_write_json(_ps_file, _ps, indent=2, ensure_ascii=False, default=str)
        except Exception as _e1:
            logger.critical(f'  [intraday_regime] L221: {_e1}', exc_info=True)
        return result

    def _detect_recovery(self, measurement: Dict) -> Dict:
        """V자 반등(급등) 감지.

        조건:
          1. 이전 누적 수익률이 음(-) → 양(+) 전환
          2. 최근 수익률 가속 (연속 양수)
          3. 거래량 서지 동반

        Returns:
            {'detected': bool, 'strength': float}
        """
        if len(self._returns_buffer) < self._vol_window:
            return {'detected': False, 'strength': 0}
        recent = list(self._returns_buffer)[-self._vol_window:]
        mid = len(recent) // 2
        early = recent[:mid]
        late = recent[mid:]
        early_sum = sum(early)
        late_sum = sum(late)
        reversal = early_sum < 0 and late_sum > 0
        positive_ratio = sum((1 for r in late if r > 0)) / max(len(late), 1)
        vol_surge = measurement.get('volume_surge', False)
        w_reversal = _cfg.get('regime.recovery_w_reversal', 0.4) if _cfg else 0.4
        w_positive = _cfg.get('regime.recovery_w_positive', 0.3) if _cfg else 0.3
        positive_threshold = _cfg.get('regime.recovery_positive_threshold', 0.6) if _cfg else 0.6
        w_volume = _cfg.get('regime.recovery_w_volume', 0.2) if _cfg else 0.2
        strength = 0.0
        if reversal:
            strength += w_reversal
        if positive_ratio > positive_threshold:
            strength += w_positive * positive_ratio
        if vol_surge:
            strength += w_volume
        threshold = _cfg.get('regime.recovery_strength_threshold', 0.5) if _cfg else 0.5
        return {'detected': strength >= threshold, 'strength': round(strength, 3), 'early_sum': round(early_sum, 6), 'late_sum': round(late_sum, 6), 'positive_ratio': round(positive_ratio, 3), 'reversal': reversal}

    def reset(self):
        """일일 리셋."""
        self._cusum_pos = 0.0
        self._cusum_neg = 0.0
        self._returns_buffer.clear()
        self._volume_buffer.clear()
        self._current_regime = 'normal'
        logger.info('  IntradayRegimeDetector: 일일 리셋 완료')

    def get_regime_history(self) -> List[Dict]:
        """금일 레짐 전환 이력."""
        return self._regime_history

    def detect(self, market_data: Dict=None) -> Dict:
        """스트림에서 호출 가능한 래퍼 — 현재 레짐 판정 반환.

        market_data에 intraday_returns가 있으면 자동 update.
        없으면 현재 버퍼 기반 판정.

        Args:
            market_data: {'intraday_returns': [pct, ...], 'volumes': [...]}

        Returns:
            regime judgment dict
        """
        if market_data:
            intraday_ret = market_data.get('intraday_returns', [])
            volumes = market_data.get('volumes', [])
            for i, ret in enumerate(intraday_ret[-5:]):
                vol = volumes[i] if i < len(volumes) else 0
                self.update(ret, vol)
        measurement = self.measure()
        return self.judge(measurement)