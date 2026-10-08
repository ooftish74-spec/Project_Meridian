"""
Project Meridian — Wall Street Non-Parametric Rolling ECDF Normalizer
======================================================================
월가 상위 퀀트펀드(Two Sigma, Renaissance Technologies) 방식의 비파라미터 정규화 엔진.

정적 하드코딩 임계값(예: VIX 18.0, VKOSPI 18.0, OFI 0.40 등)을 100% 제거하고,
최근 N일(기본 252일) 실시간 경험적 누적분포함수(Empirical CDF) 수식으로 변환하여
인간의 초기 고정관념 편향(Prior Bias)을 전면 소거.

수식:
    F_n(x) = (1 / n) * sum(I(X_i <= x))
    Z_percentile = F_n(x)  in [0.0, 1.0]
"""

import numpy as np
import pandas as pd
from typing import Union, List, Optional
import logging

logger = logging.getLogger(__name__)

class NonParametricECDF:
    """롤링 252일 비파라미터 ECDF 백분위수 정규화기."""

    def __init__(self, window: int = 252):
        self.window = window

    def compute_percentile(self, current_val: float, historical_series: Union[List[float], np.ndarray, pd.Series]) -> float:
        """현재값 current_val이 히스토리컬 시리즈 내에서 차지하는 비파라미터 ECDF 백분위수 [0.0, 1.0] 반환.
        
        Zero Fallback Protection: 히스토리 데이터 부족 시 0.50(중앙값) 안전 반환.
        """
        if current_val is None or np.isnan(current_val):
            return 0.50

        clean_data = np.array([x for x in historical_series if x is not None and not np.isnan(x)])
        if len(clean_data) == 0:
            return 0.50

        # 최근 window 개수로 제한
        if len(clean_data) > self.window:
            clean_data = clean_data[-self.window:]

        # ECDF 수식: F_n(x) = count(X_i <= x) / n
        n = len(clean_data)
        count_less_equal = np.sum(clean_data <= current_val)
        percentile = float(count_less_equal) / float(n)

        # [0.0001, 0.9999] 범위 클리핑 (Epsilon 보호)
        return float(np.clip(percentile, 0.0001, 0.9999))

    def compute_z_score(self, current_val: float, historical_series: Union[List[float], np.ndarray, pd.Series]) -> float:
        """비파라미터 Z-Score 계산 (std == 0 영분모 수식 방어 포함)."""
        if current_val is None or np.isnan(current_val):
            return 0.0

        clean_data = np.array([x for x in historical_series if x is not None and not np.isnan(x)])
        if len(clean_data) < 5:
            return 0.0

        if len(clean_data) > self.window:
            clean_data = clean_data[-self.window:]

        mean_val = np.mean(clean_data)
        std_val = np.std(clean_data)

        if std_val < 1e-8:
            return 0.0

        z_score = (current_val - mean_val) / std_val
        return float(np.clip(z_score, -4.0, 4.0))
