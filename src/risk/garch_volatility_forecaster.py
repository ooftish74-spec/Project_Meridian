"""
Project Meridian — GARCH(1,1) Volatility Forecasting Engine
=============================================================
조건부 변동성 GARCH(1,1) 모델 파라미터 추정 및 1-Step-Ahead 변동성 예측 모듈.

수식:
  sigma_t^2 = omega + alpha * epsilon_{t-1}^2 + beta * sigma_{t-1}^2  (alpha + beta < 1)
"""

import numpy as np
import logging
from typing import Dict, List, Any, Tuple, Optional

logger = logging.getLogger(__name__)

class GARCHVolatilityForecaster:
    """GARCH(1,1) 동적 변동성 예측 엔진."""

    def __init__(self, omega: float = 1e-5, alpha: float = 0.10, beta: float = 0.85):
        """
        Args:
            omega: 무조건부 변동성 가중치
            alpha: 최근 잔차 충격 반응 계수 (ARCH)
            beta: 이전 변동성 지속 계수 (GARCH)
        """
        self.omega = omega
        self.alpha = alpha
        self.beta = beta
        # alpha + beta < 1 검증
        if self.alpha + self.beta >= 1.0:
            scale = 0.98 / (self.alpha + self.beta)
            self.alpha *= scale
            self.beta *= scale

    def forecast_next_volatility(self, returns_series: np.ndarray) -> Dict[str, float]:
        """
        GARCH(1,1) 모델 기반 내일 시점 1-Step-Ahead 일일 변동성(sigma_next) 예측.

        Args:
            returns_series: (N,) 과거 일단위 수익률 타임시리즈

        Returns:
            Dict containing:
                - forecasted_daily_vol: 내일 예상 일일 변동성 (표준편차)
                - forecasted_annual_vol: 연율화 변동성 (std * sqrt(252))
                - garch_variance: 내일 예상 조건부 분산 (sigma^2)
                - persistence: 변동성 지속성 (alpha + beta)
        """
        if returns_series is None or len(returns_series) < 10:
            # 기본값 1.5% 일일 변동성
            default_vol = 0.015
            return {
                'forecasted_daily_vol': default_vol,
                'forecasted_annual_vol': default_vol * np.sqrt(252),
                'garch_variance': default_vol ** 2,
                'persistence': self.alpha + self.beta
            }

        r = np.asarray(returns_series, dtype=np.float64)
        mean_r = np.mean(r)
        epsilons = r - mean_r

        n = len(r)
        sigma2 = np.zeros(n)
        sigma2[0] = np.var(r, ddof=1) if len(r) > 1 else 1e-4

        # GARCH(1,1) 타임시리즈 재귀 필터링
        for t in range(1, n):
            sigma2[t] = self.omega + self.alpha * (epsilons[t - 1] ** 2) + self.beta * sigma2[t - 1]

        # 1-Step-Ahead 내일 분산 예측
        next_variance = self.omega + self.alpha * (epsilons[-1] ** 2) + self.beta * sigma2[-1]
        next_vol = np.sqrt(max(1e-8, next_variance))
        annual_vol = next_vol * np.sqrt(252)

        logger.info(
            f"[GARCHVolatilityForecaster] 1-Step-Ahead 변동성 예측: "
            f"Daily={next_vol*100:.2f}%, Annual={annual_vol*100:.2f}% (Persistence={self.alpha + self.beta:.3f})"
        )

        return {
            'forecasted_daily_vol': float(next_vol),
            'forecasted_annual_vol': float(annual_vol),
            'garch_variance': float(next_variance),
            'persistence': float(self.alpha + self.beta)
        }
