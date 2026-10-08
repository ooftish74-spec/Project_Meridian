"""
Project Meridian — Cornish-Fisher VaR & Expected Shortfall (CVaR) Engine
========================================================================
정규분포 가정을 탈피하고 왜도(Skewness, S) 및 첨도(Kurtosis, K)를 반영한
Cornish-Fisher Expansion VaR 및 Expected Shortfall(CVaR / 꼬리 손실 평가) 모듈.

수식:
  z_CF = z_alpha + (S/6)*(z_alpha^2 - 1) + (K/24)*(z_alpha^3 - 3*z_alpha) - (S^2/36)*(2*z_alpha^3 - 5*z_alpha)
  VaR_CF = NAV * (-mu_p + z_CF * sigma_p)
  CVaR_99 = NAV * (-mu_p + sigma_p * (phi(z_CF) / (1 - alpha)))
"""

import math
import numpy as np
import scipy.stats as stats
import logging
from typing import Dict, List, Any, Tuple, Optional

logger = logging.getLogger(__name__)

class EVTCVaRCalculator:
    """Cornish-Fisher VaR 및 Expected Shortfall(CVaR) 계산기."""

    def __init__(self, confidence_level: float = 0.99):
        """
        Args:
            confidence_level: VaR / CVaR 신뢰 수준 (기본값: 0.99)
        """
        self.confidence_level = confidence_level
        self.z_alpha = stats.norm.ppf(confidence_level)  # 99% -> 2.32635

    def calculate_cf_var_and_cvar(
        self,
        returns_history: np.ndarray,
        portfolio_value: float,
        portfolio_weights: np.ndarray,
        asset_returns_matrix: np.ndarray
    ) -> Dict[str, float]:
        """
        포트폴리오 수익률 타임시리즈로부터 Cornish-Fisher VaR 및 CVaR 계산.

        Args:
            returns_history: (T,) 과거 포트폴리오 수익률 배열
            portfolio_value: 현재 포트폴리오 평가액 (NAV, 원)
            portfolio_weights: (K,) 종목별 비중
            asset_returns_matrix: (T, K) 종목별 수익률 행렬

        Returns:
            Dict containing:
                - cf_var_krw: Cornish-Fisher 99% VaR (원)
                - cf_var_pct: Cornish-Fisher 99% VaR (%)
                - cvar_krw: Expected Shortfall 99% CVaR (원)
                - cvar_pct: Expected Shortfall 99% CVaR (%)
                - skewness: 포트폴리오 수익률 왜도
                - kurtosis: 포트폴리오 수익률 잉여 첨도 (Excess Kurtosis)
        """
        if returns_history is None or len(returns_history) < 10:
            # 데이터 부족 시 정규분포 가우시안 Fallback
            mu = 0.0
            sigma = 0.015
            skew = 0.0
            kurt = 0.0
        else:
            r = np.asarray(returns_history, dtype=np.float64)
            mu = float(np.mean(r))
            sigma = float(np.std(r, ddof=1))
            if sigma < 1e-8:
                sigma = 0.001

            skew = float(stats.skew(r))
            # scipy.stats.kurtosis default is Fisher (excess kurtosis = kurt - 3)
            kurt = float(stats.kurtosis(r, fisher=True))

        # Cornish-Fisher Expansion z_CF 계산
        z_a = self.z_alpha
        z_cf = (
            z_a +
            (skew / 6.0) * (z_a**2 - 1.0) +
            (kurt / 24.0) * (z_a**3 - 3.0 * z_a) -
            ((skew**2) / 36.0) * (2.0 * z_a**3 - 5.0 * z_a)
        )

        # Cornish-Fisher VaR (%)
        cf_var_pct = float(max(0.001, -mu + z_cf * sigma))
        cf_var_krw = float(portfolio_value * cf_var_pct)

        # Expected Shortfall (CVaR) (%)
        # phi(z_cf) = standard normal PDF at z_cf
        phi_z = stats.norm.pdf(z_cf)
        cvar_factor = phi_z / (1.0 - self.confidence_level + 1e-9)
        cvar_pct = float(max(cf_var_pct, -mu + sigma * cvar_factor))
        cvar_krw = float(portfolio_value * cvar_pct)

        logger.info(
            f"[EVTCVaRCalculator] Cornish-Fisher VaR(99%)={cf_var_pct*100:.2f}% (₩{cf_var_krw:,.0f}), "
            f"CVaR(99%)={cvar_pct*100:.2f}% (₩{cvar_krw:,.0f}) [Skew={skew:.2f}, Kurt={kurt:.2f}]"
        )

        return {
            'cf_var_krw': cf_var_krw,
            'cf_var_pct': cf_var_pct,
            'cvar_krw': cvar_krw,
            'cvar_pct': cvar_pct,
            'skewness': skew,
            'kurtosis': kurt,
            'z_cf': z_cf
        }
