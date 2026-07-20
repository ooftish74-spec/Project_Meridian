"""
Meridian — Prediction vs. Actual Gap Analysis & Continuous Learning
=====================================================================
모델의 예측 시그널(Prediction)과 실제 시장 체결 결과(Actual) 사이의 오차(Gap)를 분석합니다.
이 오차 데이터는 단순히 리포팅용이 아니라, 앙상블 모델의 각 Base Learner들의
실시간 신뢰도(Weight)를 동적으로 조절하는 '강화 피드백 루프'로 직접 사용됩니다.
"""

import pandas as pd
import numpy as np
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class GapAnalyzer:
    def __init__(self, decay_factor: float = 0.94):
        """
        :param decay_factor: 최근 오차에 더 큰 가중치를 주기 위한 지수 감쇠 계수(EWMA)
        """
        self.decay_factor = decay_factor
        
    def calculate_gap(self, predictions: pd.DataFrame, actual_trades: pd.DataFrame) -> pd.DataFrame:
        """
        예측 데이터와 체결 데이터를 조인하여 모델별, 전략별 오차(Gap)를 계산합니다.
        
        predictions: ['ticker', 'predicted_return', 'confidence', 'model_id', 'timestamp']
        actual_trades: ['ticker', 'entry_price', 'exit_price', 'actual_return', 'timestamp']
        """
        logger.info("모델 예측치와 실제 체결 결과 간의 Gap 분석 시작")
        
        # 실제 환경에서는 시간축(timestamp)과 ticker를 기준으로 exact match 혹은 as-of join 수행
        merged = pd.merge(predictions, actual_trades, on=['ticker'], suffixes=('_pred', '_actual'))
        
        # 1. Slippage Gap: 진입 시그널 시점 가격 vs 실제 체결가
        merged['slippage_gap'] = merged['entry_price'] - merged['predicted_entry_price']
        
        # 2. Return Gap: 모델 예측 수익률 vs 실제 수익률
        merged['return_gap'] = merged['actual_return'] - merged['predicted_return']
        
        # 3. Directional Accuracy (Hit/Miss)
        merged['is_hit'] = np.sign(merged['predicted_return']) == np.sign(merged['actual_return'])
        
        return merged

    def update_model_weights(self, gap_df: pd.DataFrame, current_weights: Dict[str, float]) -> Dict[str, float]:
        """
        계산된 오차를 바탕으로 각 모델(GBR, XGB, RF 등) 혹은 스트림의 동적 가중치를 업데이트합니다.
        오차가 지속적으로 큰 모델은 가중치가 삭감(Penalty)되며, 정확한 모델은 부스팅(Boosting)됩니다.
        """
        logger.info("Gap 분석 결과에 기반한 앙상블 모델 실시간 가중치 업데이트 (Continuous Learning)")
        
        new_weights = current_weights.copy()
        
        # 모델별 평균 오차(MSE 혹은 MAE) 계산
        if 'model_id' in gap_df.columns:
            model_errors = gap_df.groupby('model_id')['return_gap'].apply(lambda x: np.mean(np.abs(x)))
            
            for model_id, error in model_errors.items():
                if model_id in new_weights:
                    # 에러가 클수록 가중치 감소 (Inverse Error)
                    penalty = 1.0 / (1.0 + error)
                    # 지수 이동 평균(EWMA) 방식으로 기존 가중치와 결합
                    new_weights[model_id] = (self.decay_factor * new_weights[model_id]) + ((1 - self.decay_factor) * penalty)
                    
            # 가중치 정규화 (합이 1이 되도록)
            total_weight = sum(new_weights.values())
            for m in new_weights:
                new_weights[m] /= total_weight
                
        return new_weights
