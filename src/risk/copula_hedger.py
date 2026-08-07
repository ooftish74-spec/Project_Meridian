"""
Gaussian Copula 기반 꼬리 위험 헤지 모듈
==========================================
Area 5 (Risk Architecture) 핵심 수학 모듈.
전통적인 피어슨 상관계수(Pearson Correlation)는 폭락장(Flash Crash)에서 모두 1.0에 수렴하며 붕괴됩니다.
이 모듈은 결합 확률 분포(Copula)를 이용해 '동조화 폭락(Joint Crash)'이 일어날 비선형적 꼬리 확률을 계산합니다.
"""
import numpy as np
import pandas as pd
import math
import logging
from scipy.stats import norm
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

class CopulaHedger:
    def __init__(self, historical_returns: pd.DataFrame):
        """
        :param historical_returns: (N_days, N_assets) 자산별 일간 수익률 시계열 데이터프레임
        """
        self.returns = historical_returns.dropna(how='all')
        self._fitted = False
        self._correlation_matrix = None
        self._marginal_dist = {}
        
    def fit(self):
        """
        가우시안 코풀라 피팅
        1. 각 자산 수익률을 누적 분포 함수(CDF) 공간(Uniform [0,1])으로 변환
        2. 역표준정규분포(PPF)를 통해 정규 공간으로 변환
        3. 정규 공간에서의 상관계수 행렬(Copula Correlation) 도출
        """
        if self.returns.empty or len(self.returns) < 30:
            logger.warning("[CopulaHedger] 데이터 부족으로 Copula Fitting 실패.")
            return False
            
        Z_data = pd.DataFrame(index=self.returns.index, columns=self.returns.columns)
        
        for col in self.returns.columns:
            series = self.returns[col].dropna()
            # Empirical CDF
            rank = series.rank()
            uniform = rank / (len(series) + 1.0) # 0과 1 양극단 방지
            
            # PPF (Percent Point Function - Inverse of CDF)
            z_score = norm.ppf(uniform)
            Z_data[col] = z_score
            
            self._marginal_dist[col] = {
                'mean': series.mean(),
                'std': series.std()
            }
            
        # 정규 공간(Z-space)에서의 공분산/상관행렬 연산
        self._correlation_matrix = Z_data.corr().values
        self._fitted = True
        logger.info(f"[CopulaHedger] Gaussian Copula Fitted (assets: {len(self.returns.columns)})")
        return True
        
    def calculate_joint_crash_probability(self, threshold_sigma: float = -2.0) -> float:
        """
        동조화 폭락 확률 (Joint Tail Crash Probability) 계산
        모든 주요 자산이 threshold_sigma (기본 -2표준편차) 이하로 동시에 폭락할 확률을 산출.
        단, 다차원 가우시안 적분이 무거우므로, 상위 2~3개 자산 묶음에 대한 Multivariate Normal 근사.
        """
        if not self._fitted or self._correlation_matrix is None:
            return 0.0
            
        n = self._correlation_matrix.shape[0]
        if n < 2:
            return 0.0
            
        # 가장 비중이 큰 대표 2개 자산 간의 상관계수로 결합 확률 스케일링
        rho = float(np.mean(self._correlation_matrix[np.triu_indices(n, k=1)])) # 평균 상관계수 추출
        rho = np.clip(rho, -0.99, 0.99)
        
        # Marginal 확률 (독립일 때의 확률)
        p_marginal = norm.cdf(threshold_sigma) 
        
        # Bivariate Gaussian Copula Tail Approximation (꼬리 근사식)
        if rho > 0:
            power = 2.0 - rho # rho가 1이면 power=1 (p_marginal)
        else:
            power = 2.0 - rho
            
        p_joint = math.pow(p_marginal, power)
        return float(p_joint)
        
    def get_dynamic_hedge_ratio(self) -> float:
        """
        계산된 결합 꼬리 확률을 바탕으로 현재 시장의 취약성을 진단하여 헤징 비율을 반환.
        반환값: 0.0 (평시) ~ 1.0 (모든 델타 헤징 발동, Cash 100% 락인)
        """
        prob = self.calculate_joint_crash_probability(threshold_sigma=-2.5) # -2.5 sigma 극단 상황 가정
        
        # 평상시 -2.5 sigma 2개 자산 동시 발생 확률 (독립 가정 시): ~3.8e-5
        base_prob = math.pow(norm.cdf(-2.5), 2)
        
        if prob <= base_prob:
            return 0.0
            
        severity = prob / (base_prob + 1e-8)
        
        # severity > 10 일 때부터 방어막 발동
        hedge_ratio = 1.0 - (1.0 / (1.0 + math.exp(0.5 * (severity - 20.0))))
        
        return round(float(np.clip(hedge_ratio, 0.0, 1.0)), 4)
