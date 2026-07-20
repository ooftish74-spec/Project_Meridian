"""
Model Drift Detector — ML 모델 드리프트 감지
===============================================

S2 ML Alpha Stream의 모델 성능 열화를 실시간 감지.
정확도/캘리브레이션 에러가 임계값을 초과하면 confidence 감쇄.

Usage:
    from src.streams.s2_ml_alpha.drift_detector import ModelDriftDetector
    detector = ModelDriftDetector()
    detector.update(predicted=0.03, actual=0.01)
    result = detector.detect_drift()
"""

import logging
from typing import Dict, List

from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class ModelDriftDetector:
    """ML 모델 드리프트 감지기.

    방향 정확도와 캘리브레이션 에러를 모니터링하여
    모델 성능 열화 시 confidence를 감소시킴.
    """

    def __init__(self, window: int = None, threshold: float = None):
        """
        Args:
            window: 최근 N개 관측치로 드리프트 판단 (기본값 config 참조)
            threshold: 최소 정확도 마진 (0.5 + threshold, 기본값 config 참조)
        """
        self._predictions: List[float] = []
        self._actuals: List[float] = []
        self._window = window or cfg.get('s2.drift_window', 20)
        self._threshold = threshold if threshold is not None else cfg.get('s2.drift_threshold', 0.15)  # max tolerable accuracy degradation
        self._drift_history: List[Dict] = []

    def update(self, predicted: float, actual: float):
        """예측/실현 쌍 기록.

        Args:
            predicted: 모델 예측값 (수익률)
            actual: 실현 수익률
        """
        self._predictions.append(predicted)
        self._actuals.append(actual)
        # 최근 100개만 유지
        if len(self._predictions) > 100:
            self._predictions = self._predictions[-100:]
            self._actuals = self._actuals[-100:]

    def detect_drift(self) -> Dict:
        """드리프트 감지.

        Returns:
            {
                'drifted': bool,
                'accuracy': float,
                'calibration_error': float,
                'action': 'normal' | 'reduce_confidence',
                'confidence_multiplier': float,
            }
        """
        if len(self._predictions) < self._window:
            return {
                'drifted': False,
                'reason': 'insufficient_data',
                'n_samples': len(self._predictions),
                'action': 'normal',
                'confidence_multiplier': 1.0,
            }

        recent_preds = self._predictions[-self._window:]
        recent_actuals = self._actuals[-self._window:]

        # 방향 정확도
        correct = sum(
            1 for p, a in zip(recent_preds, recent_actuals)
            if (p > 0) == (a > 0))
        accuracy = correct / len(recent_preds)

        # 캘리브레이션 에러 (평균 예측 vs 평균 실현)
        mean_pred = sum(recent_preds) / len(recent_preds)
        mean_actual = sum(recent_actuals) / len(recent_actuals)
        calibration_error = abs(mean_pred - mean_actual)

        # 드리프트 판정
        cal_err_threshold = cfg.get('s2.drift_calibration_threshold', 0.05)
        drifted = (accuracy < (0.5 + self._threshold)
                   or calibration_error > cal_err_threshold)

        # confidence 감쇄 정도 결정 (DynamicConfig 기반)
        if drifted:
            severe_threshold = cfg.get('s2.drift_severe_accuracy', 0.45)
            moderate_threshold = cfg.get('s2.drift_moderate_accuracy', 0.55)
            severe_mult = cfg.get('s2.drift_severe_multiplier', 0.3)
            moderate_mult = cfg.get('s2.drift_moderate_multiplier', 0.5)
            mild_mult = cfg.get('s2.drift_mild_multiplier', 0.7)

            if accuracy < severe_threshold:
                multiplier = severe_mult
            elif accuracy < moderate_threshold:
                multiplier = moderate_mult
            else:
                multiplier = mild_mult
            action = 'reduce_confidence'
        else:
            multiplier = 1.0
            action = 'normal'

        result = {
            'drifted': drifted,
            'accuracy': round(accuracy, 4),
            'calibration_error': round(calibration_error, 6),
            'n_samples': len(recent_preds),
            'action': action,
            'confidence_multiplier': multiplier,
        }

        if drifted:
            self._drift_history.append(result)
            logger.warning(
                f"  ⚠️ 모델 드리프트 감지: accuracy={accuracy:.3f}, "
                f"cal_error={calibration_error:.5f} → "
                f"confidence ×{multiplier}")

        return result

    def get_drift_history(self) -> List[Dict]:
        """드리프트 감지 이력."""
        return self._drift_history

    def reset(self):
        """리셋 (재학습 후)."""
        self._predictions.clear()
        self._actuals.clear()
        logger.info("  ModelDriftDetector: 리셋 완료 (재학습 완료)")
