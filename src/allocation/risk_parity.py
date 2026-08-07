"""
Meridian — Risk Parity Optimizer
================================
브릿지워터(Bridgewater)의 철학인 All-Weather 포트폴리오 기법에 입각하여,
모든 자산/스트림이 전체 포트폴리오 리스크(Volatility)에 기여하는 바가 동일해지도록
최적의 투자 비중을 수학적으로 탐색합니다.
"""

import numpy as np
import scipy.optimize as sco
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

class RiskParityOptimizer:
    def __init__(self, tolerance: float = 1e-6, max_weight_per_asset: float = 0.3):
        self.tolerance = tolerance
        self.max_weight_per_asset = max_weight_per_asset

    def _risk_contribution(self, weights: np.ndarray, cov_matrix: np.ndarray) -> np.ndarray:
        """
        주어진 가중치(weights)와 공분산 행렬(cov_matrix)에 대해
        각 자산/스트림의 리스크 기여도를 계산합니다.
        """
        portfolio_volatility = np.sqrt(weights.T @ cov_matrix @ weights)
        # Marginal Risk Contribution
        mrc = (cov_matrix @ weights) / portfolio_volatility
        # Risk Contribution
        rc = weights * mrc
        return rc, portfolio_volatility

    def _risk_parity_objective(self, weights: np.ndarray, cov_matrix: np.ndarray, previous_weights: np.ndarray = None, turnover_penalty: float = 0.0) -> float:
        """
        각 자산의 리스크 기여도가 1/N 에 수렴하도록 하는 목적 함수(SSD).
        """
        N = len(weights)
        rc, port_vol = self._risk_contribution(weights, cov_matrix)
        target_rc = port_vol / N
        
        # Sum of Squared Differences
        ssd = np.sum(np.square(rc - target_rc))
        
        # [Fix C] Turnover Penalty (자전적 폭락 방어용 댐 역할)
        if previous_weights is not None and turnover_penalty > 0:
            penalty = turnover_penalty * np.sum(np.square(weights - previous_weights))
            ssd += penalty
            
        return ssd

    def optimize(self, cov_matrix: np.ndarray, allow_short: bool = False, custom_bounds: List[Tuple[float, float]] = None, previous_weights: np.ndarray = None, current_aum: float = 1.5e8) -> np.ndarray:
        """
        리스크 패리티 가중치를 산출합니다.
        :param cov_matrix: 수익률 공분산 행렬
        :param allow_short: True일 경우 롱숏 혼합 (-1.0 ~ 1.0) 허용 (크립토 숏 등)
        :param custom_bounds: 자산별 개별 Bounds 리스트 (우선 적용)
        :param previous_weights: 직전 포트폴리오 비중 배열 (Turnover 계산용)
        :param current_aum: 운용 자산 규모 (원화). 규모가 클수록 회전율 페널티가 강화됨.
        """
        N = cov_matrix.shape[0]
        init_weights = np.repeat(1 / N, N)
        
        # AUM 기반 동적 Turnover Penalty 프레임워크
        base_penalty = 0.0
        if current_aum > 1000e8:     # 1000억 이상 (강력한 마찰 방지)
            base_penalty = 0.15
        elif current_aum > 100e8:    # 100억 이상
            base_penalty = 0.05
        # 1.5억 수준의 소자본은 페널티 0 (무한대의 회전율과 기동성 확보)
        
        # [Fix C] Constraint Collision 방지: 자산 수(N)가 적을 때 상한을 유연하게 늘려 수학적 붕괴 방지
        dynamic_max_cap = max(self.max_weight_per_asset, 1.0 / max(N * 0.8, 1.0))
        
        # 제약 조건: 가중치의 절대값 합이 1 (Gross Exposure = 100%)
        if allow_short or custom_bounds:
            constraints = ({'type': 'eq', 'fun': lambda x: np.sum(np.abs(x)) - 1.0})
        else:
            # 기본 Long-Only 제약 (Net = Gross = 100%)
            constraints = ({'type': 'eq', 'fun': lambda x: np.sum(x) - 1.0})
            
        # Bounds 설정 (Cash Drag 방지를 위한 dynamic_max_cap 상한 적용)
        if custom_bounds:
            bounds = tuple((cb[0], min(cb[1], dynamic_max_cap)) for cb in custom_bounds)
        elif allow_short:
            bounds = tuple((-1.0, dynamic_max_cap) for _ in range(N))
        else:
            bounds = tuple((0.0, dynamic_max_cap) for _ in range(N))
        
        result = sco.minimize(
            self._risk_parity_objective,
            init_weights,
            args=(cov_matrix, previous_weights, base_penalty),
            method='SLSQP',
            bounds=bounds,
            constraints=constraints,
            options={'ftol': self.tolerance, 'disp': False}
        )
        
        if not result.success:
            logger.critical("Risk Parity Optimization failed to converge.")
            raise RuntimeError(f"Risk Parity Optimization failed: {result.message}")
            
        return result.x

