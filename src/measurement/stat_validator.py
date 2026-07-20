#!/usr/bin/env python3
"""
Statistical Significance Validator — 전략 통계적 유의성 검증
==============================================================

Medallion Upgrade Phase 2-A-4.

검증 기법:
  1. Sharpe Ratio 유의성 (t-test)
  2. Bootstrap 신뢰구간
  3. Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014)
  4. Minimum Track Record Length

모든 파라미터 DynamicConfig 동적 로드.
"""

import logging
import math
import random
from typing import Dict, List, Optional, Tuple

from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class StatValidator:
    """전략 통계적 유의성 검증."""

    def validate_sharpe(self, returns: List[float],
                         benchmark_returns: List[float] = None) -> Dict:
        """Sharpe Ratio 통계적 유의성 검증.

        H0: True Sharpe = 0 (또는 benchmark Sharpe)
        H1: True Sharpe > 0

        Args:
            returns: 전략 일별 수익률
            benchmark_returns: 벤치마크 수익률 (excess return 계산)

        Returns:
            Sharpe, SE, t-stat, p-value, 유의 여부
        """
        min_obs = cfg.get('stat.min_observations', 60)
        alpha = cfg.get('stat.significance_level', 0.05)
        n = len(returns)

        if n < min_obs:
            return {
                'sharpe': None,
                'sufficient': False,
                'n_observations': n,
                'min_required': min_obs,
            }

        # Excess returns (벤치마크 대비)
        if benchmark_returns and len(benchmark_returns) == n:
            excess = [r - b for r, b in zip(returns, benchmark_returns)]
        else:
            excess = returns

        mean_r = sum(excess) / n
        var = sum((r - mean_r) ** 2 for r in excess) / (n - 1)
        std = math.sqrt(var) if var > 0 else 0

        ann = cfg.get('common.annualization_factor', 252)
        sharpe = (mean_r / std) * math.sqrt(ann) if std > 0 else 0

        # Sharpe Ratio SE (Lo 2002)
        # SE(SR) ≈ sqrt((1 + 0.5 * SR²) / n)
        sharpe_se = math.sqrt((1 + 0.5 * sharpe ** 2) / n)

        # t-statistic: SR / SE
        t_stat = sharpe / sharpe_se if sharpe_se > 0 else 0

        # p-value 근사 (정규분포 CDF)
        p_value = 1 - self._normal_cdf(t_stat)

        # Minimum Track Record Length (Bailey & Lopez de Prado)
        # MinTRL = 1 + (1 + 0.5 * SR²) * (z_α / SR)²
        z_alpha = self._normal_quantile(1 - alpha)
        min_trl = (1 + (1 + 0.5 * sharpe ** 2) *
                     (z_alpha / sharpe) ** 2 / ann
                     if abs(sharpe) > 1e-6 else float('inf'))

        return {
            'sharpe': round(sharpe, 4),
            'sharpe_se': round(sharpe_se, 4),
            't_stat': round(t_stat, 4),
            'p_value': round(p_value, 4),
            'is_significant': p_value < alpha,
            'significance_level': alpha,
            'min_track_record_months': round(min_trl * 12, 1),
            'n_observations': n,
            'sufficient': True,
        }

    def bootstrap_test(self, returns: List[float],
                         n_bootstrap: int = None) -> Dict:
        """Bootstrap 신뢰구간.

        비모수적 bootstrap으로 Sharpe/Return의 95% CI 추정.
        """
        if n_bootstrap is None:
            n_bootstrap = cfg.get('stat.n_bootstrap', 10000)

        n = len(returns)
        min_obs = cfg.get('stat.min_observations', 60)

        if n < min_obs:
            return {'sufficient': False, 'n_observations': n}

        ann = cfg.get('common.annualization_factor', 252)
        random.seed(42)

        boot_sharpes = []
        boot_returns = []

        for _ in range(n_bootstrap):
            # 복원 추출
            sample = [returns[random.randint(0, n - 1)] for _ in range(n)]
            mean_s = sum(sample) / n
            var_s = sum((r - mean_s) ** 2 for r in sample) / (n - 1)
            std_s = math.sqrt(var_s) if var_s > 0 else 0

            sharpe_s = ((mean_s / std_s) * math.sqrt(ann)
                          if std_s > 0 else 0)
            boot_sharpes.append(sharpe_s)
            boot_returns.append(mean_s * ann)

        # 정렬 → 백분위
        boot_sharpes.sort()
        boot_returns.sort()

        ci_lo = int(n_bootstrap * 0.025)
        ci_hi = int(n_bootstrap * 0.975)

        return {
            'sharpe_ci_95': (
                round(boot_sharpes[ci_lo], 4),
                round(boot_sharpes[ci_hi], 4)),
            'mean_return_ci_95': (
                round(boot_returns[ci_lo], 4),
                round(boot_returns[ci_hi], 4)),
            'sharpe_median': round(
                boot_sharpes[n_bootstrap // 2], 4),
            'prob_positive_sharpe': round(
                sum(1 for s in boot_sharpes if s > 0) / n_bootstrap, 4),
            'n_bootstrap': n_bootstrap,
            'n_observations': n,
            'sufficient': True,
        }

    def deflated_sharpe(self, returns: List[float],
                          n_strategies_tested: int = 1) -> Dict:
        """Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014).

        다중 전략 테스트에 따른 Sharpe 인플레이션 보정.

        DSR = p-value of observed SR after adjusting for:
          1. Number of strategies tested
          2. Data length
          3. Return skewness & kurtosis

        Args:
            returns: 전략 수익률
            n_strategies_tested: 테스트한 전략 수 (다중 비교 보정)
        """
        n = len(returns)
        min_obs = cfg.get('stat.min_observations', 60)

        if n < min_obs:
            return {'sufficient': False}

        ann = cfg.get('common.annualization_factor', 252)
        mean_r = sum(returns) / n
        var = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
        std = math.sqrt(var) if var > 0 else 0
        sharpe = (mean_r / std) * math.sqrt(ann) if std > 0 else 0

        # Skewness
        skew = (sum((r - mean_r) ** 3 for r in returns) /
                  (n * (std ** 3 + 1e-12)))

        # Kurtosis (excess)
        kurt = (sum((r - mean_r) ** 4 for r in returns) /
                  (n * (std ** 4 + 1e-12)) - 3)

        # Expected maximum Sharpe under null (Euler-Mascheroni)
        euler_gamma = 0.5772156649
        if n_strategies_tested > 1:
            expected_max_sr = math.sqrt(2 * math.log(n_strategies_tested))
            expected_max_sr -= (
                (math.log(math.pi) + euler_gamma) /
                (2 * math.sqrt(2 * math.log(n_strategies_tested))))
        else:
            expected_max_sr = 0

        # Sharpe Ratio SE adjusted for skewness & kurtosis
        se_inner = (1 - skew * sharpe + (kurt - 1) / 4 * sharpe ** 2) / max(n - 1, 1)
        se_adj = math.sqrt(max(1e-12, se_inner))

        # DSR = p-value
        if se_adj > 0:
            dsr_stat = (sharpe - expected_max_sr) / se_adj
            dsr_pvalue = 1 - self._normal_cdf(dsr_stat)
        else:
            dsr_stat = 0
            dsr_pvalue = 0.5

        alpha = cfg.get('stat.significance_level', 0.05)

        return {
            'sharpe': round(sharpe, 4),
            'deflated_sharpe_pvalue': round(dsr_pvalue, 4),
            'is_significant_after_deflation': dsr_pvalue < alpha,
            'expected_max_sharpe_null': round(expected_max_sr, 4),
            'n_strategies_tested': n_strategies_tested,
            'skewness': round(skew, 4),
            'excess_kurtosis': round(kurt, 4),
            'n_observations': n,
            'sufficient': True,
        }

    # ═══════════════════════════════════════
    # Statistical Utilities (no scipy)
    # ═══════════════════════════════════════

    @staticmethod
    def _normal_cdf(x: float) -> float:
        """표준정규분포 CDF 근사 (Abramowitz & Stegun)."""
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    @staticmethod
    def _normal_quantile(p: float) -> float:
        """표준정규분포 분위수 근사 (Beasley-Springer-Moro).

        Rational approximation for 0.5 < p < 1.
        """
        if p <= 0.5:
            return -StatValidator._normal_quantile(1 - p)

        t = math.sqrt(-2 * math.log(1 - p))
        # Horner form coefficients
        c0, c1, c2 = 2.515517, 0.802853, 0.010328
        d1, d2, d3 = 1.432788, 0.189269, 0.001308
        return t - (c0 + c1 * t + c2 * t ** 2) / (
            1 + d1 * t + d2 * t ** 2 + d3 * t ** 3)
