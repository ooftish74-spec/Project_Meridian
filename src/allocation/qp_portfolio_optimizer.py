"""
Project Meridian — Renaissance-Style QP Portfolio Optimizer
==============================================================
고정 예산 분할을 폐지하고, 공분산 행렬(Σ), VIX Z-Score 연동 동적 위험회피 계수(λ_risk),
수수료/슬리피지 마찰 모델 기반 이차 계획법(Quadratic Programming) 최적 자원 배분기.

수식:
    max_w { w^T alpha - (lambda_risk / 2) * w^T Sigma w - C * |w - w0| }
    s.t.  sum(w_i) <= max_exposure,  0 <= w_i <= max_single_weight
"""

import logging
import numpy as np
import scipy.optimize as sco
import pandas as pd
from typing import Dict, List, Optional, Tuple, Any
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class QPortfolioOptimizer:
    """Renaissance 스타일 Quadratic Programming (QP) 자본 배분 최적화기."""

    def __init__(
        self,
        base_lambda_risk: float = 1.0,
        tx_cost_rate: float = 0.0015,
        max_single_weight: float = 0.35,
        lookback_days: int = 60
    ):
        self.base_lambda_risk = cfg.get('optimizer.base_lambda_risk', base_lambda_risk)
        self.tx_cost_rate = cfg.get('optimizer.tx_cost_rate', tx_cost_rate)
        self.max_single_weight = cfg.get('optimizer.max_single_weight', max_single_weight)
        self.lookback_days = cfg.get('optimizer.lookback_days', lookback_days)

    def compute_dynamic_lambda_risk(self, vix_spot: float, vix_mean: float = 18.0, vix_std: float = 5.0) -> float:
        """
        VIX Standardized Z-Score 기반 동적 위험 회피 계수 산출:
        Z_vix = (VIX - VIX_mean) / VIX_std
        lambda_risk = lambda_0 * exp(Z_vix)
        """
        if vix_std <= 0:
            vix_std = 5.0
        z_vix = (vix_spot - vix_mean) / vix_std
        z_vix_clamped = max(-2.0, min(3.0, z_vix))  # Exploding exp 방지
        lambda_risk = self.base_lambda_risk * np.exp(z_vix_clamped)
        logger.debug(f"[QPOptimizer] VIX={vix_spot:.2f} (Z={z_vix:.2f}) → lambda_risk={lambda_risk:.4f}")
        return float(lambda_risk)

    def compute_ewma_covariance(self, returns_df: pd.DataFrame, decay_factor: float = 0.94) -> np.ndarray:
        """
        Rolling 60일 데이터 기반 EWMA 공분산 행렬(Σ) 계산.
        """
        if returns_df.empty or len(returns_df) < 5:
            n_assets = len(returns_df.columns) if not returns_df.empty else 1
            return np.eye(n_assets) * 0.0004  # Fallback 2% daily vol

        # EWMA 가중치 계산
        n_obs = len(returns_df)
        weights = (1 - decay_factor) * (decay_factor ** np.arange(n_obs)[::-1])
        weights /= weights.sum()

        mean_returns = np.average(returns_df.values, axis=0, weights=weights)
        demeaned = returns_df.values - mean_returns
        weighted_demeaned = demeaned * np.sqrt(weights[:, np.newaxis])
        cov_matrix = weighted_demeaned.T @ weighted_demeaned

        # 수치적 안정성 확보 (Positive semi-definite 보장)
        min_eig = np.min(np.real(np.linalg.eigvals(cov_matrix)))
        if min_eig < 1e-8:
            cov_matrix += np.eye(len(cov_matrix)) * (1e-8 - min_eig)

        return cov_matrix

    def compute_tri_factor_alphas(
        self,
        alphas: Dict[str, float],
        cov_matrix: np.ndarray,
        crowding_z_scores: Optional[Dict[str, float]] = None,
        decay_gamma: float = 0.5
    ) -> np.ndarray:
        """
        Tri-Factor Signal Engine:
          1. Factor Crowding Decay: alpha_adj = alpha_raw * exp(-gamma * max(0, Z_crowding - 1.5))
          2. Risk-Adjusted Sharpe Normalization: alpha_sharpe = alpha_adj / sigma_i
          3. MCR (Marginal Contribution to Risk) naturally handled in QP Covariance Matrix.
        """
        asset_names = list(alphas.keys())
        n_assets = len(asset_names)
        adj_alphas = []

        for i, k in enumerate(asset_names):
            raw_alpha = float(alphas[k])
            # Asset daily vol from covariance matrix diagonal
            asset_vol = np.sqrt(max(1e-6, cov_matrix[i, i])) if i < cov_matrix.shape[0] else 0.02

            # 1. Factor Crowding Decay
            z_crowd = crowding_z_scores.get(k, 0.0) if crowding_z_scores else 0.0
            decay_mult = np.exp(-decay_gamma * max(0.0, z_crowd - 1.5))
            decayed_alpha = raw_alpha * decay_mult

            # 3. Risk-Adjusted Sharpe Normalization
            sharpe_alpha = decayed_alpha / max(1e-4, asset_vol)
            adj_alphas.append(sharpe_alpha)

        return np.array(adj_alphas, dtype=float)

    def optimize_allocation(
        self,
        alphas: Dict[str, float],
        cov_matrix: np.ndarray,
        current_weights: Dict[str, float],
        vix_spot: float = 18.0,
        max_total_exposure: float = 1.0,
        custom_bounds: Optional[List[Tuple[float, float]]] = None,
        crowding_z_scores: Optional[Dict[str, float]] = None
    ) -> Dict[str, Any]:
        """
        이차 계획법(QP) 기반 100% 신호/리스크 주도 자본 배분 최적화 실행.
        """
        asset_names = list(alphas.keys())
        n_assets = len(asset_names)

        if n_assets == 0:
            return {'weights': {}, 'lambda_risk': 1.0, 'expected_portfolio_alpha': 0.0, 'portfolio_volatility': 0.0, 'turnover': 0.0, 'status': 'empty'}

        # ── Phase 2 Solution: Natural FX Hedge Matrix Integration ──
        cov_matrix_hedged = np.array(cov_matrix, copy=True)
        for i, name_i in enumerate(asset_names):
            if any(us_tag in name_i.upper() for us_tag in ['US', 'S5', 'S6', 'TQQQ', 'SOXL', 'NVDA']):
                for j, name_j in enumerate(asset_names):
                    if i == j:
                        cov_matrix_hedged[i, j] = max(1e-6, cov_matrix_hedged[i, j] * 0.82)
                    elif any(us_tag in name_j.upper() for us_tag in ['US', 'S5', 'S6', 'TQQQ', 'SOXL', 'NVDA']):
                        cov_matrix_hedged[i, j] *= 0.85

        cov_matrix = cov_matrix_hedged

        if cov_matrix.shape != (n_assets, n_assets):
            logger.warning(f"[QPOptimizer] Covariance matrix shape mismatch {cov_matrix.shape} vs ({n_assets}, {n_assets}) → Using diagonal")
            cov_matrix = np.eye(n_assets) * 0.0004

        # Compute Tri-Factor Alphas (Crowding Decay + Sharpe Normalization)
        alpha_vec = self.compute_tri_factor_alphas(alphas, cov_matrix, crowding_z_scores)
        w0_vec = np.array([current_weights.get(k, 0.0) for k in asset_names], dtype=float)

        lambda_risk = self.compute_dynamic_lambda_risk(vix_spot)

        # Objective Function: Minimize - (w^T alpha - (lambda_risk/2) * w^T Sigma w - C * |w - w0|)
        def objective(w: np.ndarray) -> float:
            utility = w @ alpha_vec
            variance_penalty = 0.5 * lambda_risk * (w @ cov_matrix @ w)
            turnover_cost = self.tx_cost_rate * np.sum(np.abs(w - w0_vec))
            return -(utility - variance_penalty - turnover_cost)

        # Constraints & Bounds
        constraints = [
            {'type': 'ineq', 'fun': lambda w: max_total_exposure - np.sum(w)}  # sum(w) <= max_total_exposure
        ]

        if custom_bounds is None:
            bounds = [(0.0, min(self.max_single_weight, max_total_exposure)) for _ in range(n_assets)]
        else:
            bounds = custom_bounds

        # Initial Guess: Equal weight capped at max_total_exposure
        w_init = np.full(n_assets, max_total_exposure / n_assets)

        try:
            res = sco.minimize(
                objective,
                w_init,
                method='SLSQP',
                bounds=bounds,
                constraints=constraints,
                options={'maxiter': 200, 'ftol': 1e-7}
            )

            if res.success:
                opt_w = np.clip(res.x, 0.0, 1.0)
                # Max exposure clamping
                total_w = np.sum(opt_w)
                if total_w > max_total_exposure and total_w > 0:
                    opt_w = opt_w * (max_total_exposure / total_w)
                status = 'success'
            else:
                logger.warning(f"[QPOptimizer] SLSQP optimization warning: {res.message} → Fallback to w0")
                opt_w = np.clip(w0_vec, 0.0, max_total_exposure)
                status = f'fallback ({res.message})'
        except Exception as e:
            logger.error(f"[QPOptimizer] Optimization exception: {e} → Equal weight fallback", exc_info=True)
            opt_w = np.full(n_assets, max_total_exposure / n_assets)
            status = f'exception ({e})'

        optimized_weights = {asset_names[i]: round(float(opt_w[i]), 4) for i in range(n_assets)}
        port_alpha = float(opt_w @ alpha_vec)
        port_vol = float(np.sqrt(max(1e-9, opt_w @ cov_matrix @ opt_w)))
        turnover = float(np.sum(np.abs(opt_w - w0_vec)))

        logger.info(f"[QPOptimizer] 최적화 완료 ({status}): Alpha={port_alpha*100:.2f}%, Vol={port_vol*100:.2f}%, Turnover={turnover:.3f}, Lambda={lambda_risk:.2f}")

        return {
            'weights': optimized_weights,
            'lambda_risk': round(lambda_risk, 4),
            'expected_portfolio_alpha': round(port_alpha, 6),
            'portfolio_volatility': round(port_vol, 6),
            'turnover': round(turnover, 4),
            'status': status
        }
