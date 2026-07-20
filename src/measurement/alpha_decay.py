#!/usr/bin/env python3
"""
Alpha Decay Tracker — 스트림별 알파 반감기 추적
=================================================

Medallion Upgrade Phase 2-A-2.

알파 감쇠 (Alpha Decay) 측정:
  - AR(1) 계수 기반 반감기(half-life) 추정
  - 스트림별 알파 잔존량 모니터링
  - 자동 가중치 조정 권고

반감기 계산:
  half_life = -ln(2) / ln(β)
  where β = AR(1) 자기상관 계수

모든 파라미터 DynamicConfig 동적 로드.
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
_DECAY_HISTORY = _PROJECT_ROOT / 'results' / 'alpha_decay_history.json'


class AlphaDecayTracker:
    """스트림별 알파 감쇠 추적."""

    def __init__(self):
        self._history: Dict[str, List[Dict]] = self._load_history()

    def track(self, stream_id: str,
               daily_alphas: List[float]) -> Dict:
        """스트림별 알파 감쇠 추적.

        Args:
            stream_id: 스트림 ID ('S1', 'S2', ...)
            daily_alphas: 일별 알파 (초과수익률) 시계열

        Returns:
            반감기, 현재 알파, 감쇠율, 경고 여부
        """
        min_obs = cfg.get('alpha.min_observations', 30)
        if len(daily_alphas) < min_obs:
            return {
                'stream_id': stream_id,
                'half_life_days': None,
                'sufficient': False,
                'n_observations': len(daily_alphas),
            }

        half_life = self._compute_half_life(daily_alphas)
        current_alpha = daily_alphas[-1] if daily_alphas else 0

        # 감쇠율: β per day
        if half_life and half_life > 0:
            decay_rate = 1 - 2 ** (-1.0 / half_life)
        else:
            decay_rate = 0

        # 알파 소멸까지 남은 시간 추정
        if abs(current_alpha) > 1e-6 and decay_rate > 0:
            # 0.001 수준까지 감쇠하는 데 걸리는 시간
            time_to_zero = (-math.log(0.001 / abs(current_alpha)) /
                              math.log(1 - decay_rate)
                              if decay_rate < 1 else 0)
        else:
            time_to_zero = float('inf')

        # 경고 판단
        min_half = cfg.get('alpha.min_half_life_days', 30)
        warn_half = cfg.get('alpha.warning_half_life_days', 60)

        if half_life is not None and half_life < min_half:
            warning = 'critical'
            action = 'reduce_weight'
        elif half_life is not None and half_life < warn_half:
            warning = 'caution'
            action = 'monitor'
        else:
            warning = 'normal'
            action = 'maintain'

        # Rolling 알파 통계
        window = cfg.get('alpha.rolling_window', 60)
        recent = daily_alphas[-window:]
        alpha_mean = sum(recent) / len(recent) if recent else 0
        alpha_positive_ratio = (
            sum(1 for a in recent if a > 0) / len(recent) if recent else 0)

        result = {
            'stream_id': stream_id,
            'half_life_days': round(half_life, 1) if half_life else None,
            'current_alpha': round(current_alpha, 6),
            'decay_rate': round(decay_rate, 6),
            'time_to_zero_days': (round(time_to_zero, 0)
                                    if time_to_zero != float('inf') else None),
            'alpha_mean_60d': round(alpha_mean, 6),
            'alpha_positive_ratio': round(alpha_positive_ratio, 3),
            'warning': warning,
            'action': action,
            'sufficient': True,
            'n_observations': len(daily_alphas),
            'timestamp': datetime.now().isoformat(),
        }

        # 히스토리 저장
        if stream_id not in self._history:
            self._history[stream_id] = []
        self._history[stream_id].append({
            'date': datetime.now().isoformat(),
            'half_life': result['half_life_days'],
            'alpha_mean': result['alpha_mean_60d'],
        })
        # 최근 365일 유지
        self._history[stream_id] = self._history[stream_id][-365:]
        self._save_history()

        return result

    def compare_streams(self, stream_alphas: Dict[str, List[float]]) -> Dict:
        """모든 스트림 알파 비교 + 가중치 조정 권고.

        Args:
            stream_alphas: {stream_id: daily_alphas_list}

        Returns:
            스트림별 알파 현황 + 권고 가중치
        """
        results = {}
        half_lives = {}

        for stream_id, alphas in stream_alphas.items():
            result = self.track(stream_id, alphas)
            results[stream_id] = result
            hl = result.get('half_life_days')
            if hl and hl > 0:
                half_lives[stream_id] = hl

        # 가중치 권고: 반감기가 긴 스트림에 더 많은 가중치
        if half_lives:
            total_hl = sum(half_lives.values())
            recommended_weights = {
                sid: round(hl / total_hl, 3)
                for sid, hl in half_lives.items()}
        else:
            n = len(stream_alphas)
            recommended_weights = {
                sid: round(1.0 / n, 3) for sid in stream_alphas}

        return {
            'stream_results': results,
            'recommended_weights': recommended_weights,
            'best_alpha_stream': (
                max(half_lives, key=half_lives.get)
                if half_lives else None),
            'worst_alpha_stream': (
                min(half_lives, key=half_lives.get)
                if half_lives else None),
        }

    def _compute_half_life(self, series: List[float]) -> Optional[float]:
        """AR(1) 자기상관 기반 반감기 계산.

        y_t = β × y_{t-1} + ε
        half_life = -ln(2) / ln(β)

        순수 Python OLS 구현 (scipy 불요).
        """
        n = len(series)
        if n < 10:
            return None

        # AR(1): y[t] = α + β × y[t-1]
        x = series[:-1]  # y_{t-1}
        y = series[1:]   # y_t
        m = len(x)

        mean_x = sum(x) / m
        mean_y = sum(y) / m

        cov_xy = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(m)) / m
        var_x = sum((x[i] - mean_x) ** 2 for i in range(m)) / m

        if var_x < 1e-12:
            return None

        beta = cov_xy / var_x

        # β must be in (0, 1) for mean-reverting process
        if beta <= 0 or beta >= 1:
            return None

        half_life = -math.log(2) / math.log(beta)
        return max(1.0, half_life)

    def _load_history(self) -> Dict[str, List[Dict]]:
        if _DECAY_HISTORY.exists():
            try:
                return json.loads(_DECAY_HISTORY.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        return {}

    def _save_history(self):
        try:
            _DECAY_HISTORY.parent.mkdir(parents=True, exist_ok=True)
            _DECAY_HISTORY.write_text(
                json.dumps(self._history, indent=2, ensure_ascii=False))
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
