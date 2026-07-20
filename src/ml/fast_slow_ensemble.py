import logging
import joblib
import numpy as np
from pathlib import Path
from sklearn.linear_model import Ridge
logger = logging.getLogger(__name__)

class FastCorrector:
    """
    [Phase 2] Fast-Slow 앙상블 아키텍처.
    메인 앙상블(Slow 모델)의 추론 오차(Residual)를 가벼운 Ridge Regression(Fast 모델)이 
    단기 데이터(Val Set)로 학습하여 예측 시 편향(Bias)을 실시간에 가깝게 보정합니다.
    """

    def __init__(self, alpha: float=1.0):
        self.alpha = alpha
        self.model = Ridge(alpha=self.alpha)
        self.is_fitted = False

    def fit(self, X: np.ndarray, y_true: np.ndarray, y_slow_pred: np.ndarray):
        """
        검증셋(Val)에서 Slow 모델의 잔차를 학습합니다.
        residual = y_true - y_slow_pred
        """
        residuals = y_true - y_slow_pred
        self.model.fit(X, residuals)
        self.is_fitted = True
        pred_res = self.model.predict(X)
        mse = np.mean((residuals - pred_res) ** 2)
        logger.info(f'  [FastCorrector] 훈련 완료: 잔차 피팅 MSE={mse:.5f}')

    def predict_correction(self, X: np.ndarray) -> np.ndarray:
        """
        Slow 모델의 예측값에 더할 보정치(Residual Prediction)를 반환합니다.
        """
        if not self.is_fitted:
            return np.zeros(len(X))
        return self.model.predict(X)

    def save(self, path: Path):
        if self.is_fitted:
            joblib.dump(self, path)
            logger.info(f'  [FastCorrector] 모델 저장 완료: {path.name}')

    @classmethod
    def load(cls, path: Path):
        if path.exists():
            try:
                model = joblib.load(path)
                logger.info(f'  [FastCorrector] 로드 완료: {path.name}')
                return model
            except Exception as e:
                logger.warning(f'  ⚠️ [FastCorrector] 로드 실패: {e}')
        return cls()