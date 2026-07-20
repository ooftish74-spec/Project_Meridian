"""
Adaptive Conformal Predictor — 분포 무관 예측 구간
====================================================
ML 예측에 신뢰 구간을 추가하여 신호 품질 정량화.

Usage:
    from src.intelligence.conformal_predictor import AdaptiveConformalPredictor
    cp = AdaptiveConformalPredictor()
    cp.calibrate(y_pred_cal, y_true_cal)
    result = cp.predict_interval(y_pred_new)
"""

import logging
import numpy as np
from collections import deque
from typing import Dict, Optional

logger = logging.getLogger(__name__)

try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _cfg = None


class AdaptiveConformalPredictor:
    """적응적 Conformal Prediction."""

    def __init__(self, target_coverage: float = 0.90,
                 adaptation_rate: float = 0.01,
                 window_size: int = 100):
        if _cfg:
            target_coverage = _cfg.get('conformal.target_coverage', target_coverage)
        self.target_coverage = target_coverage
        self.adaptation_rate = adaptation_rate
        self.window_size = window_size
        self.conf_high_threshold = _cfg.get('conformal.width_high', 0.15) if _cfg else 0.15
        self.conf_low_threshold = _cfg.get('conformal.width_low', 0.30) if _cfg else 0.30

        self.calibration_scores = []
        self.quantile_level = target_coverage
        self.coverage_history = deque(maxlen=window_size)
        self.width_history = deque(maxlen=window_size)

    def calibrate(self, y_pred: np.ndarray, y_true: np.ndarray):
        """Calibration Set으로 비적합 점수 계산.

        Score = |y_true - y_pred| (분류: |p - actual|)
        """
        self.calibration_scores = sorted(np.abs(y_true - y_pred).tolist())
        logger.debug(f"  Conformal 보정: {len(self.calibration_scores)}건")

    def predict_interval(self, y_pred: np.ndarray,
                         alpha: Optional[float] = None) -> Dict:
        """적응적 예측 구간 생성.

        Args:
            y_pred: 예측 확률 배열
            alpha: 유의 수준 (default: 1-target_coverage)

        Returns:
            {'lower': array, 'upper': array, 'width': float,
             'quantile': float, 'method': str}
        """
        if not self.calibration_scores:
            std = np.std(y_pred) if len(y_pred) > 1 else 0.1
            return {
                'lower': (y_pred - 1.645 * std).tolist(),
                'upper': (y_pred + 1.645 * std).tolist(),
                'width': float(2 * 1.645 * std),
                'quantile': 0.90,
                'method': 'fallback_gaussian',
            }

        effective_alpha = alpha or (1 - self.quantile_level)
        n = len(self.calibration_scores)

        # Quantile of nonconformity scores
        q_idx = int(np.ceil((1 - effective_alpha) * (n + 1))) - 1
        q_idx = max(0, min(q_idx, n - 1))
        q_val = self.calibration_scores[q_idx]

        lower = np.clip(y_pred - q_val, 0, 1)
        upper = np.clip(y_pred + q_val, 0, 1)
        width = float(2 * q_val)

        self.width_history.append(width)

        return {
            'lower': lower.tolist() if isinstance(lower, np.ndarray) else [lower],
            'upper': upper.tolist() if isinstance(upper, np.ndarray) else [upper],
            'width': width,
            'quantile': round(q_val, 4),
            'method': 'adaptive_conformal',
            'n_calibration': n,
        }

    def update(self, y_pred: float, y_true: float):
        """실시간 커버리지 추적 + 양자 자동 조정."""
        score = abs(y_true - y_pred)
        n = len(self.calibration_scores)
        q_idx = int(np.ceil(self.quantile_level * (n + 1))) - 1
        q_idx = max(0, min(q_idx, n - 1))
        q_val = self.calibration_scores[q_idx] if self.calibration_scores else 1.0

        covered = score <= q_val
        self.coverage_history.append(1 if covered else 0)

        # 적응적 조정
        if len(self.coverage_history) >= 20:
            actual_coverage = np.mean(list(self.coverage_history))
            gap = actual_coverage - self.target_coverage

            if gap < -0.05:
                # 커버리지 부족 → 구간 넓히기
                self.quantile_level = min(self.quantile_level + self.adaptation_rate, 0.99)
            elif gap > 0.05:
                # 커버리지 과다 → 구간 좁히기
                self.quantile_level = max(self.quantile_level - self.adaptation_rate, 0.50)

        # Calibration 갱신
        import bisect
        bisect.insort(self.calibration_scores, score)

    def get_confidence(self, y_pred: float) -> Dict:
        """단일 예측의 신뢰도 평가.

        Returns:
            {'confidence': 'high'|'medium'|'low', 'interval_width': float}
        """
        result = self.predict_interval(np.array([y_pred]))
        width = result['upper'][0] - result['lower'][0]
        # 동적 임계값: 최근 width_history의 분포 기준 (percentile)
        if len(self.width_history) >= 20:
            hist = np.array(self.width_history)
            p33 = np.percentile(hist, 33)
            p66 = np.percentile(hist, 66)
            
            if width <= p33:
                conf = 'high'
            elif width <= p66:
                conf = 'medium'
            else:
                conf = 'low'
        else:
            # Fallback
            if width < self.conf_high_threshold:
                conf = 'high'
            elif width < self.conf_low_threshold:
                conf = 'medium'
            else:
                conf = 'low'

        return {
            'confidence': conf,
            'interval_width': width,
            'lower': result['lower'][0],
            'upper': result['upper'][0],
        }


class SignalMetaCalibrator:
    """[Phase 74] XGBoost/GBT 메타 보정기.

    1차 모델의 confidence를 HMM 레짐 + 실제 승패 데이터로 재보정.

    입력: (confidence, regime_index, rolling_vol)
    출력: 보정된 승률 0.0~1.0
    """

    _REGIME_IDX = {
        'bull': 0, 'correction': 1, 'whipsaw': 2, 'crash': 3,
        'caution': 1, 'bear': 3,
    }
    _MIN_SAMPLES = 30

    def __init__(self) -> None:
        import logging
        self._model   = None
        self._fitted  = False
        self._history: list = []
        self._log     = logging.getLogger(self.__class__.__name__)

    def record(
        self,
        confidence:  float,
        regime:      str,
        rolling_vol: float,
        actual_win:  bool,
    ) -> None:
        """실제 승패 결과 기록."""
        self._history.append((
            float(confidence),
            float(self._REGIME_IDX.get(regime, 1)),
            float(rolling_vol),
            1 if actual_win else 0,
        ))

    def fit(self) -> bool:
        """XGBoost 메타 모델 학습."""
        import numpy as np
        if len(self._history) < self._MIN_SAMPLES:
            return False
        X = np.array([[h[0], h[1], h[2]] for h in self._history])
        y = np.array([h[3] for h in self._history])
        try:
            import xgboost as xgb  # type: ignore[import]
            self._model = xgb.XGBClassifier(
                n_estimators=50, max_depth=3, learning_rate=0.1,
                eval_metric='logloss', random_state=42, verbosity=0,
            )
        except ImportError as e:
            from sklearn.ensemble import GradientBoostingClassifier
            self._model = GradientBoostingClassifier(
                n_estimators=50, max_depth=3, learning_rate=0.1, random_state=42,
            )
        try:
            self._model.fit(X, y)
            self._fitted = True
            self._log.info(f'[Phase 74 Meta] 학습 완료: {len(self._history)}샘플')
            return True
        except Exception as e:  # noqa: BLE001
            self._log.warning(f'[Phase 74 Meta] 학습 실패: {e}')
            return False

    def calibrate(
        self,
        confidence:  float,
        regime:      str,
        rolling_vol: float = 15.0,
    ) -> float:
        """[Phase 74] confidence → 레짐 보정 승률."""
        import numpy as np
        if not self._fitted or self._model is None:
            return confidence
        try:
            X = np.array([[
                float(confidence),
                float(self._REGIME_IDX.get(regime, 1)),
                float(rolling_vol),
            ]])
            cal = float(self._model.predict_proba(X)[0][1])
            self._log.debug(f'  [Phase 74 Meta] {regime}: {confidence:.3f} → {cal:.3f}')
            return round(cal, 4)
        except Exception as e:  # noqa: BLE001
            self._log.debug(f'  [Phase 74 Meta] calibrate 실패: {e}')
            return confidence
