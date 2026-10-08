"""
Project Meridian — Factor Orthogonalizer Engine
=================================================
Gram-Schmidt 및 SVD / PCA 기반 알파 팩터 직교화 및 다중공선성 제거 모듈.

수식:
  기존 알파 기저 공간 V = [f_1, f_2, ..., f_k] 에 대해 신규 알파 f_new를 직교 투영:
  f_residual = f_new - V @ inv(V.T @ V) @ V.T @ f_new
"""

import numpy as np
import logging
from typing import Dict, List, Any, Tuple, Optional

logger = logging.getLogger(__name__)

class FactorOrthogonalizer:
    """PCA/Gram-Schmidt 알파 팩터 직교화 엔진."""

    def __init__(self, max_correlation_threshold: float = 0.70):
        """
        Args:
            max_correlation_threshold: 직교화 트리거를 위한 최대 용인 상관계수
        """
        self.max_correlation_threshold = max_correlation_threshold

    def orthogonalize_factor(
        self,
        new_factor_series: np.ndarray,
        existing_base_matrix: np.ndarray
    ) -> Tuple[np.ndarray, float]:
        """
        신규 알파 팩터를 기존 알파 기저 행렬에 대해 Gram-Schmidt 직교화.

        Args:
            new_factor_series: (N,) 신규 알파 팩터 수치 배열
            existing_base_matrix: (N, K) K개의 기존 알파 팩터 기저 행렬

        Returns:
            (residual_factor, max_correlation):
               residual_factor: (N,) 기존 팩터와 상관계수가 0에 가까운 순수 잉여 알파
               max_correlation: 직교화 전 기존 팩터들과의 최대 피어슨 상관계수
        """
        new_f = np.asarray(new_factor_series, dtype=np.float64)

        if existing_base_matrix is None or existing_base_matrix.size == 0 or existing_base_matrix.shape[1] == 0:
            return new_f, 0.0

        V = np.asarray(existing_base_matrix, dtype=np.float64)
        if V.shape[0] != new_f.shape[0]:
            raise ValueError(f"샘플 수 불일치: new_f={new_f.shape[0]}, V={V.shape[0]}")

        # 1. 상관계수 측정
        max_corr = 0.0
        for col_idx in range(V.shape[1]):
            base_col = V[:, col_idx]
            std_prod = np.std(new_f) * np.std(base_col)
            if std_prod > 1e-12:
                corr = abs(float(np.corrcoef(new_f, base_col)[0, 1]))
                if np.isnan(corr):
                    corr = 0.0
                max_corr = max(max_corr, corr)

        # 2. 직교 투영 (Projection)
        # f_residual = f_new - V @ pinv(V.T @ V) @ V.T @ f_new
        try:
            # SVD 기반 의사역행렬 사용으로 다중공선성 안정화
            proj_weights = np.linalg.pinv(V) @ new_f
            projection = V @ proj_weights
            residual = new_f - projection
        except Exception as e:
            logger.warning(f"[FactorOrthogonalizer] 직교화 수치 계산 예외 (원본 유지): {e}")
            residual = new_f

        # 표준화 (Mean 0, Std 1)
        res_std = np.std(residual)
        if res_std > 1e-8:
            residual = (residual - np.mean(residual)) / res_std
        else:
            residual = np.zeros_like(residual)

        logger.info(f"[FactorOrthogonalizer] 직교화 완료 (최대 기존 상관계수={max_corr:.3f})")
        return residual, max_corr
