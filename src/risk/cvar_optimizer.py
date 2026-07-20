#!/usr/bin/env python3
"""
CVaR Optimizer — 포트폴리오 Conditional VaR 최적화
====================================================

Medallion Upgrade Phase 2-B-1.

CVaR (Expected Shortfall):
  - VaR보다 꼬리 위험을 정확히 포착
  - 손실 분포의 조건부 기대값 (worst α% 평균 손실)

기능:
  1. compute_cvar: 현재 포트폴리오 CVaR 계산
  2. optimize_weights: CVaR 최소화 비중 최적화 (gradient-free)
  3. marginal_cvar: 각 자산의 한계 CVaR 기여도
  4. component_cvar: 각 자산의 CVaR 분해

모든 파라미터 DynamicConfig 동적 로드.
"""

import logging
import math
import random
from typing import Dict, List, Optional, Tuple

from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class CVaROptimizer:
    """포트폴리오 CVaR 계산 및 최적화."""

    def compute_cvar(self, returns: List[List[float]],
                      weights: List[float],
                      confidence: float = None) -> Dict:
        """포트폴리오 CVaR 계산.

        Args:
            returns: 자산별 일별 수익률 [[r1_d1, r1_d2, ...], [r2_d1, ...]]
            weights: 자산별 비중 [w1, w2, ...]
            confidence: VaR 신뢰수준 (기본: DynamicConfig)

        Returns:
            VaR, CVaR, 꼬리 비율 등
        """
        if confidence is None:
            confidence = cfg.get('risk.var_confidence', 0.95)

        # 포트폴리오 수익률 계산
        port_returns = self._portfolio_returns(returns, weights)
        n = len(port_returns)
        if n < 10:
            return {'var_pct': 0, 'cvar_pct': 0, 'n_obs': n,
                    'sufficient': False}

        # 정렬 (오름차순 → 왼쪽이 최악)
        sorted_r = sorted(port_returns)
        cutoff_idx = max(1, int(n * (1 - confidence)))

        # VaR: α% 분위수
        var_pct = sorted_r[cutoff_idx - 1]

        # CVaR: VaR 이하 수익률의 평균 (Expected Shortfall)
        tail = sorted_r[:cutoff_idx]
        cvar_pct = sum(tail) / len(tail) if tail else var_pct

        # 꼬리 비율: CVaR / VaR (> 1이면 fat tail)
        tail_ratio = abs(cvar_pct / var_pct) if abs(var_pct) > 1e-8 else 1.0

        return {
            'var_pct': round(var_pct, 6),
            'cvar_pct': round(cvar_pct, 6),
            'tail_ratio': round(tail_ratio, 3),
            'confidence': confidence,
            'n_obs': n,
            'n_tail': len(tail),
            'sufficient': True,
        }

    def optimize_weights(self, returns: List[List[float]],
                          current_weights: List[float],
                          target_cvar: float = None) -> Dict:
        """CVaR 최소화 비중 최적화 (gradient-free random search).

        scipy 의존 없이 순수 Python으로 구현.

        Args:
            returns: 자산별 수익률
            current_weights: 현재 비중
            target_cvar: 목표 CVaR (None → 단순 최소화)

        Returns:
            최적화된 비중 + CVaR 비교
        """
        if target_cvar is None:
            target_cvar = cfg.get('risk.target_cvar_pct', -0.03)

        n_assets = len(current_weights)
        n_iter = cfg.get('risk.cvar_optimization_iterations', 1000)
        min_w = cfg.get('risk.cvar_min_weight', 0.02)
        max_w = cfg.get('risk.cvar_max_weight', 0.40)

        # 현재 CVaR
        current_cvar = self.compute_cvar(returns, current_weights)

        best_weights = list(current_weights)
        best_cvar = current_cvar.get('cvar_pct', 0)

        random.seed(42)
        for _ in range(n_iter):
            # 랜덤 비중 생성 (Dirichlet-like)
            raw = [random.random() for _ in range(n_assets)]
            total = sum(raw)
            candidate = [max(min_w, min(max_w, r / total)) for r in raw]
            # 재정규화
            c_total = sum(candidate)
            candidate = [w / c_total for w in candidate]

            result = self.compute_cvar(returns, candidate)
            cvar = result.get('cvar_pct', 0)

            if cvar > best_cvar:  # CVaR is negative, higher = better
                best_cvar = cvar
                best_weights = list(candidate)

        best_weights = [round(w, 4) for w in best_weights]

        return {
            'optimized_weights': best_weights,
            'cvar_before': current_cvar.get('cvar_pct', 0),
            'cvar_after': round(best_cvar, 6),
            'improvement_pct': round(
                (best_cvar - current_cvar.get('cvar_pct', 0)) * 100, 2),
            'target_cvar': target_cvar,
            'target_met': best_cvar >= target_cvar,
            'n_iterations': n_iter,
        }

    def marginal_cvar(self, returns: List[List[float]],
                        weights: List[float]) -> List[float]:
        """각 자산의 한계 CVaR 기여도.

        δCVaR/δw_i ≈ (CVaR(w+ε_i) - CVaR(w)) / ε
        """
        epsilon = 0.01
        base = self.compute_cvar(returns, weights)
        base_cvar = base.get('cvar_pct', 0)

        marginals = []
        for i in range(len(weights)):
            perturbed = list(weights)
            perturbed[i] += epsilon
            # 재정규화
            total = sum(perturbed)
            perturbed = [w / total for w in perturbed]

            result = self.compute_cvar(returns, perturbed)
            delta = (result.get('cvar_pct', 0) - base_cvar) / epsilon
            marginals.append(round(delta, 6))

        return marginals

    def component_cvar(self, returns: List[List[float]],
                         weights: List[float]) -> Dict:
        """CVaR 분해: 각 자산의 총 CVaR 기여도.

        Component CVaR_i = w_i × Marginal CVaR_i
        """
        marginals = self.marginal_cvar(returns, weights)
        components = [round(w * m, 6) for w, m in zip(weights, marginals)]
        total = sum(components)

        pct_contributions = [
            round(c / total * 100, 2) if abs(total) > 1e-8 else 0
            for c in components]

        return {
            'components': components,
            'pct_contributions': pct_contributions,
            'total_cvar': round(total, 6),
            'marginals': marginals,
        }

    @staticmethod
    def _portfolio_returns(returns: List[List[float]],
                             weights: List[float]) -> List[float]:
        """가중 포트폴리오 수익률 시계열."""
        if not returns or not weights:
            return []
        n_days = min(len(r) for r in returns) if returns else 0
        port_r = []
        for d in range(n_days):
            daily = sum(w * returns[i][d]
                         for i, w in enumerate(weights)
                         if d < len(returns[i]))
            port_r.append(daily)
        return port_r
