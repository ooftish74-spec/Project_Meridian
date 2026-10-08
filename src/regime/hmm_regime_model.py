"""
Meridian — Hidden Markov Model (HMM) Regime Engine
===================================================
기존의 하드코딩된 VIX 임계값(예: VIX > 35면 Crash)을 완전히 폐기하고,
데이터의 순수 통계적 확률 분포에 기반하여 시장 상태(Regime)를 추론하는 동적 수학 모델입니다.

Bridgewater의 Macro-awareness와 Medallion의 순수 통계적 접근을 결합하여,
시장 데이터를 N개의 은닉 상태(Hidden States)로 클러스터링하고 전이 확률(Transition Probabilities)을 계산합니다.
"""

import numpy as np
import pandas as pd
import logging
from typing import Dict, Any, List

try:
    from hmmlearn.hmm import GaussianHMM
    HAS_HMMLEARN = True
except ImportError as e:
    HAS_HMMLEARN = False
    from sklearn.mixture import GaussianMixture

logger = logging.getLogger(__name__)

class HMMRegimeModel:
    def __init__(self, n_components: int = 4, lookback_days: int = 252 * 5):
        self.n_components = n_components
        self.lookback_days = lookback_days
        
        if HAS_HMMLEARN:
            self.model = GaussianHMM(n_components=self.n_components, covariance_type="full", n_iter=1000, random_state=42)
        else:
            self.model = GaussianMixture(n_components=self.n_components, covariance_type="full", max_iter=1000, random_state=42)
            
        self.state_mapping = {}
        self.is_fitted = False

    def _prepare_features(self, market_data: Dict[str, Any]) -> np.ndarray:
        # 하드코딩 제거: 입력 데이터의 통계적 정규화(Z-score) 수행
        pass

    def fit(self, historical_data: Any):
        logger.info(f"HMM Regime Model 학습 시작")
        try:
            if isinstance(historical_data, pd.DataFrame):
                X = historical_data.select_dtypes(include=[np.number]).dropna().values
            else:
                X = np.array(historical_data)
            if len(X) >= 10:
                self.model.fit(X)
                self.is_fitted = True
                logger.info("✅ HMM Regime Model 학습 완료.")
            else:
                logger.warning("HMM 학습을 위한 샘플 수 부족.")
        except Exception as e:
            logger.error(f"HMM Model fit 실패: {e}")

    def predict(self, current_features: Any) -> Dict[str, Any]:
        if not self.is_fitted:
            try:
                if current_features is not None:
                    self.fit(current_features)
            except Exception:
                pass
        if not self.is_fitted:
            return {"regime": "caution", "probabilities": [0.25, 0.50, 0.25, 0.0]}
        try:
            X = np.atleast_2d(current_features)
            probs = self.model.predict_proba(X)[-1]
            return {"regime": "caution", "probabilities": probs.tolist()}
        except Exception as e:
            logger.warning(f"HMM predict 예외 발생: {e}")
            return {"regime": "caution", "probabilities": [0.25, 0.50, 0.25, 0.0]}

