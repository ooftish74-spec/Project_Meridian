"""
Tests for Renaissance-Style QP Portfolio Optimizer
"""

import pytest
import numpy as np
import pandas as pd
from src.allocation.qp_portfolio_optimizer import QPortfolioOptimizer

def test_qp_optimizer_allocation():
    optimizer = QPortfolioOptimizer(base_lambda_risk=1.0, max_single_weight=0.35)

    alphas = {
        'S1': 0.05,
        'S2': 0.03,
        'S3': 0.01,
        'S5': 0.02
    }

    cov_matrix = np.array([
        [0.0004, 0.0001, 0.0000, 0.0001],
        [0.0001, 0.0003, 0.0000, 0.0001],
        [0.0000, 0.0000, 0.0002, 0.0000],
        [0.0001, 0.0001, 0.0000, 0.0005]
    ])

    current_weights = {'S1': 0.1, 'S2': 0.1, 'S3': 0.1, 'S5': 0.1}

    result = optimizer.optimize_allocation(
        alphas=alphas,
        cov_matrix=cov_matrix,
        current_weights=current_weights,
        vix_spot=18.0,
        max_total_exposure=0.80
    )

    assert 'weights' in result
    assert result['status'] == 'success'
    weights = result['weights']
    assert sum(weights.values()) <= 0.80 + 1e-4
    assert weights['S1'] >= weights['S3']  # Higher alpha gets equal or higher weight

def test_qp_optimizer_vix_lambda_scaling():
    optimizer = QPortfolioOptimizer(base_lambda_risk=1.0)
    lambda_normal = optimizer.compute_dynamic_lambda_risk(vix_spot=18.0)
    lambda_panic = optimizer.compute_dynamic_lambda_risk(vix_spot=33.0)

    assert lambda_panic > lambda_normal
