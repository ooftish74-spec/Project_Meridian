#!/usr/bin/env python3
"""
Online Learner — 실시간 온라인 학습 프레임워크
================================================

Medallion Upgrade Phase 3-A-1.

기능:
  1. EWA (Exponentially Weighted Average) 기반 파라미터 실시간 갱신
  2. 증분 모델 래퍼 (warm-start 기반 재학습)
  3. Forgetting Curve — 오래된 데이터 자동 감쇠
  4. 안전 경계 + 최대 스텝 제한

기존 SelfLearning(배치 IC 기반)과의 차이:
  - SelfLearning: 일 1회, IC 기반 → DynamicConfig 조정
  - OnlineLearner: 매 체결 피드백마다 실시간 EWA 갱신

모든 파라미터 DynamicConfig 동적 로드.
"""

import logging
import math
import json
from datetime import datetime, timedelta
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ONLINE_STATE = _PROJECT_ROOT / 'results' / 'online_learner_state.json'


class ForgettingCurve:
    """시간 감쇠 가중치 (Exponential Forgetting)."""

    def compute_weights(self, n_observations: int,
                          reference_date_idx: int = None) -> List[float]:
        """각 관측치의 감쇠 가중치 계산.

        Args:
            n_observations: 관측치 수
            reference_date_idx: 기준점 (None이면 마지막)

        Returns:
            가중치 리스트 (최신이 가장 높음)
        """
        lam = cfg.get('online.forgetting_lambda', 0.995)
        if reference_date_idx is None:
            reference_date_idx = n_observations - 1

        weights = []
        for i in range(n_observations):
            age = reference_date_idx - i
            w = lam ** max(0, age)
            weights.append(w)

        # 정규화
        total = sum(weights)
        if total > 0:
            weights = [w / total for w in weights]

        return weights

    def weighted_mean(self, values: List[float]) -> float:
        """감쇠 가중 평균."""
        if not values:
            return 0
        weights = self.compute_weights(len(values))
        return sum(v * w for v, w in zip(values, weights))

    def weighted_std(self, values: List[float]) -> float:
        """감쇠 가중 표준편차."""
        if len(values) < 2:
            return 0
        weights = self.compute_weights(len(values))
        mean = sum(v * w for v, w in zip(values, weights))
        var = sum(w * (v - mean) ** 2 for v, w in zip(values, weights))
        return math.sqrt(max(0, var))


class OnlineLearner:
    """실시간 온라인 학습 엔진.

    매 체결 결과 피드백마다 EWA 기반으로
    전략 파라미터를 점진적으로 갱신합니다.
    """

    def __init__(self):
        self._forgetting = ForgettingCurve()
        self._observation_buffer: Dict[str, List[Dict]] = {}
        self._param_history: Dict[str, List[float]] = {}
        self._update_count = 0
        self._load_state()

    def observe(self, stream_id: str, observation: Dict) -> Optional[Dict]:
        """새 관측치(체결 결과) 수집 + 학습 트리거.

        Args:
            stream_id: 스트림 ID (S1~S4)
            observation: {
                'signal_confidence': float,  # 신호 시점 confidence
                'actual_return': float,       # 실제 수익률
                'hold_minutes': int,          # 보유 시간
                'regime': str,                # 체결 시점 레짐
            }

        Returns:
            파라미터 갱신 내역 (없으면 None)
        """
        if stream_id not in self._observation_buffer:
            self._observation_buffer[stream_id] = []

        self._observation_buffer[stream_id].append({
            'timestamp': datetime.now().isoformat(),
            **observation,
        })

        # 최근 N건 유지
        max_buffer = cfg.get('online.max_buffer_size', 500)
        if len(self._observation_buffer[stream_id]) > max_buffer:
            self._observation_buffer[stream_id] = \
                self._observation_buffer[stream_id][-max_buffer:]

        # 최소 관측치 도달 시 학습
        min_obs = cfg.get('online.min_observations_per_update', 10)
        if len(self._observation_buffer[stream_id]) >= min_obs:
            return self._update_params(stream_id)

        return None

    def _update_params(self, stream_id: str) -> Dict:
        """EWA 기반 파라미터 갱신.

        θ_new = α × θ_observed + (1-α) × θ_old
        Safety: |θ_new - θ_old| < max_step × |θ_old|
        """
        alpha = cfg.get('online.learning_rate', 0.05)
        max_step = cfg.get('online.max_step', 0.10)

        observations = self._observation_buffer[stream_id]
        changes = {}

        # 1) Confidence 정확도 → confidence 스케일 조정
        conf_key = f'online.{stream_id.lower()}.confidence_scale'
        current_scale = cfg.get(conf_key, 1.0)

        returns = [o.get('actual_return', 0) for o in observations]
        confidences = [o.get('signal_confidence', 0.5) for o in observations]

        if returns and confidences:
            # 감쇠 가중 IC (confidence와 return의 상관)
            weighted_ic = self._weighted_rank_ic(confidences, returns)

            # IC 기반 스케일 조정
            if weighted_ic > 0.05:
                observed_scale = current_scale * (1 + weighted_ic * 0.5)
            elif weighted_ic < -0.05:
                observed_scale = current_scale * (1 + weighted_ic * 0.3)
            else:
                observed_scale = current_scale

            # EWA
            new_scale = alpha * observed_scale + (1 - alpha) * current_scale

            # Safety clamp
            delta = abs(new_scale - current_scale)
            if delta > max_step * abs(current_scale):
                direction = 1 if new_scale > current_scale else -1
                new_scale = current_scale + direction * max_step * abs(current_scale)

            # 합리적 범위 제한
            new_scale = max(0.5, min(2.0, new_scale))

            if abs(new_scale - current_scale) > 1e-6:
                changes[conf_key] = {
                    'old': round(current_scale, 6),
                    'new': round(new_scale, 6),
                    'ic': round(weighted_ic, 4),
                }

        # 2) Win Rate → 포지션 크기 스케일 조정
        size_key = f'online.{stream_id.lower()}.size_scale'
        current_size = cfg.get(size_key, 1.0)

        if returns:
            win_rate = sum(1 for r in returns if r > 0) / len(returns)
            weighted_wr = self._forgetting.weighted_mean(
                [1.0 if r > 0 else 0.0 for r in returns])

            if weighted_wr > 0.55:
                observed_size = current_size * (1 + (weighted_wr - 0.5) * 0.5)
            elif weighted_wr < 0.45:
                observed_size = current_size * (1 - (0.5 - weighted_wr) * 0.5)
            else:
                observed_size = current_size

            new_size = alpha * observed_size + (1 - alpha) * current_size
            new_size = max(0.3, min(1.5, new_size))

            if abs(new_size - current_size) > 1e-6:
                changes[size_key] = {
                    'old': round(current_size, 6),
                    'new': round(new_size, 6),
                    'win_rate': round(weighted_wr, 4),
                }

        # 적용
        if changes:
            self._apply_changes(changes)
            self._update_count += 1
            self._save_state()

        return {
            'stream_id': stream_id,
            'n_observations': len(observations),
            'changes': changes,
            'update_count': self._update_count,
            'timestamp': datetime.now().isoformat(),
        }

    def _weighted_rank_ic(self, predictions: List[float],
                            actuals: List[float]) -> float:
        """감쇠 가중 Rank IC (Spearman).

        순수 Python 구현 — 감쇠 가중치 적용.
        """
        n = len(predictions)
        if n < 5:
            return 0

        weights = self._forgetting.compute_weights(n)

        # 순위 계산
        def _rank(values):
            indexed = sorted(enumerate(values), key=lambda x: x[1])
            ranks = [0.0] * n
            for rank_pos, (orig_idx, _) in enumerate(indexed):
                ranks[orig_idx] = rank_pos + 1
            return ranks

        pred_ranks = _rank(predictions)
        actual_ranks = _rank(actuals)

        # 가중 Spearman
        mean_pr = sum(r * w for r, w in zip(pred_ranks, weights))
        mean_ar = sum(r * w for r, w in zip(actual_ranks, weights))

        cov = sum(w * (pr - mean_pr) * (ar - mean_ar)
                    for w, pr, ar in zip(weights, pred_ranks, actual_ranks))
        std_p = math.sqrt(max(1e-12,
            sum(w * (pr - mean_pr) ** 2
                for w, pr in zip(weights, pred_ranks))))
        std_a = math.sqrt(max(1e-12,
            sum(w * (ar - mean_ar) ** 2
                for w, ar in zip(weights, actual_ranks))))

        return cov / (std_p * std_a) if std_p * std_a > 1e-12 else 0

    def _apply_changes(self, changes: Dict) -> None:
        """변경 적용."""
        for key, detail in changes.items():
            cfg.set(key, detail['new'])
            logger.info(
                f"  🧠 OnlineLearner: {key} "
                f"{detail['old']:.4f} → {detail['new']:.4f}")

    def batch_update(self, stream_results: Dict[str, List[Dict]]) -> Dict:
        """배치 업데이트 (일 마감 시).

        Args:
            stream_results: {stream_id: [observations]}
        """
        all_changes = {}
        for sid, observations in stream_results.items():
            for obs in observations:
                result = self.observe(sid, obs)
                if result and result.get('changes'):
                    all_changes[sid] = result
        return all_changes

    def get_status(self) -> Dict:
        """학습 상태 요약."""
        return {
            'update_count': self._update_count,
            'buffer_sizes': {
                sid: len(buf)
                for sid, buf in self._observation_buffer.items()
            },
            'learning_rate': cfg.get('online.learning_rate', 0.05),
            'forgetting_lambda': cfg.get('online.forgetting_lambda', 0.995),
        }

    def _save_state(self) -> None:
        """상태 저장."""
        try:
            state = {
                'update_count': self._update_count,
                'buffer_sizes': {
                    s: len(b) for s, b in self._observation_buffer.items()},
                'last_updated': datetime.now().isoformat(),
            }
            _ONLINE_STATE.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(_ONLINE_STATE, state, indent=2, ensure_ascii=False)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass

    def _load_state(self) -> None:
        """상태 복원."""
        try:
            if _ONLINE_STATE.exists():
                state = json.loads(_ONLINE_STATE.read_text())
                self._update_count = state.get('update_count', 0)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
