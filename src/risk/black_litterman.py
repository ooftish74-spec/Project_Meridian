#!/usr/bin/env python3
"""
Black-Litterman Portfolio Optimization (Moonshot 1)
======================================================

단순 시가총액/동일 비중 대신 시장 균형 수익률(Equilibrium Returns)과 
알파 모델(S2/S4)의 예측 뷰(Investor Views)를 결합하여 최적의 투자 비중을 산출.

사용법:
    from src.risk.black_litterman import BlackLittermanOptimizer
    blo = BlackLittermanOptimizer()
    optimal_weights = blo.optimize(market_caps, cov_matrix, views, confidences)
"""

import logging
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

class BlackLittermanOptimizer:
    def __init__(self, tau: float = 0.05, risk_aversion: float = 2.5):
        """
        Args:
            tau: 사전 분포 불확실성 (일반적으로 0.01 ~ 0.05)
            risk_aversion: 시장 위험 회피 계수 (델타)
        """
        self.tau = tau
        self.risk_aversion = risk_aversion

    def optimize(
        self, 
        market_caps: pd.Series, 
        cov_matrix: pd.DataFrame, 
        views: Dict[str, float], 
        confidences: Dict[str, float]
    ) -> pd.Series:
        """
        Black-Litterman 모델을 이용해 최적 포트폴리오 비중 계산.
        
        Args:
            market_caps: {ticker: market_cap}
            cov_matrix: 종목간 공분산 행렬 (일일/월간 수익률 기반)
            views: 퀀트 모델이 예측한 절대 수익률 뷰 {ticker: predicted_return}
            confidences: 뷰에 대한 확신도 (0~1) {ticker: confidence_score}
            
        Returns:
            optimal_weights: {ticker: weight (0.0 ~ 1.0)}
        """
        tickers = list(market_caps.index)
        n = len(tickers)
        if n == 0:
            return pd.Series(dtype=float)

        # 1. 시가총액 비율 (Market Weights)
        mcap_weights = market_caps / market_caps.sum()
        W_m = mcap_weights.values.reshape(-1, 1)

        # 2. 공분산 행렬 (Sigma)
        Sigma = cov_matrix.loc[tickers, tickers].values

        # 3. 시장 균형 수익률 (Implied Equilibrium Returns: Pi)
        # Pi = risk_aversion * Sigma * W_m
        Pi = self.risk_aversion * np.dot(Sigma, W_m)

        # 4. 투자자 뷰 구성 (P, Q, Omega)
        k = len(views)
        if k == 0:
            # 뷰가 없으면 시장 균형 수익률 비중 반환
            return mcap_weights

        P = np.zeros((k, n))
        Q = np.zeros((k, 1))
        Omega = np.zeros((k, k))

        for idx, (ticker, predicted_ret) in enumerate(views.items()):
            if ticker in tickers:
                t_idx = tickers.index(ticker)
                P[idx, t_idx] = 1.0
                Q[idx, 0] = predicted_ret
                
                # 오메가 (불확실성): tau * P * Sigma * P^T / confidence
                conf = max(0.01, min(1.0, confidences.get(ticker, 0.5)))
                var = np.dot(np.dot(P[idx], Sigma), P[idx].T)
                Omega[idx, idx] = (self.tau * var) / conf

        # 5. 사후 결합 수익률 (Posterior Expected Returns: E[R])
        # E[R] = [(tau * Sigma)^-1 + P^T * Omega^-1 * P]^-1 * [(tau * Sigma)^-1 * Pi + P^T * Omega^-1 * Q]
        tau_Sigma_inv = np.linalg.inv(self.tau * Sigma)
        try:
            Omega_inv = np.linalg.inv(Omega)
        except np.linalg.LinAlgError:
            logger.critical("Omega matrix is singular. Falling back to market weights.", exc_info=True)
            return mcap_weights

        term1 = np.linalg.inv(tau_Sigma_inv + np.dot(np.dot(P.T, Omega_inv), P))
        term2 = np.dot(tau_Sigma_inv, Pi) + np.dot(np.dot(P.T, Omega_inv), Q)
        posterior_returns = np.dot(term1, term2)

        # 6. 최적 포트폴리오 비중 산출 (W_opt)
        # W_opt = (1 / risk_aversion) * Sigma^-1 * E[R]
        Sigma_inv = np.linalg.inv(Sigma)
        optimal_weights_array = (1 / self.risk_aversion) * np.dot(Sigma_inv, posterior_returns)

        # 음수 비중(공매도 불가 가정) 제거 후 정규화
        W_opt = optimal_weights_array.flatten()
        W_opt = np.maximum(W_opt, 0.0)
        
        total_weight = W_opt.sum()
        if total_weight > 0:
            W_opt = W_opt / total_weight
        else:
            # 모든 뷰가 비관적일 경우 시총 비중으로 Fallback
            W_opt = mcap_weights.values

        optimal_weights = pd.Series(W_opt, index=tickers)
        logger.info(f"Black-Litterman optimization complete for {n} assets.")
        
        return optimal_weights
