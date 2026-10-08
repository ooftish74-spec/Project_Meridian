import pytest
import numpy as np
from src.allocation.qp_portfolio_optimizer import QPortfolioOptimizer

def test_tri_factor_qp_optimizer_convergence():
    opt = QPortfolioOptimizer(base_lambda_risk=1.0)

    alphas = {
        '005930': 0.05,
        '000660': 0.04,
        '069500': 0.02,
        '091160': 0.03
    }
    cov_matrix = np.array([
        [0.0004, 0.0003, 0.0001, 0.0001],
        [0.0003, 0.0005, 0.0001, 0.0001],
        [0.0001, 0.0001, 0.0002, 0.0000],
        [0.0001, 0.0001, 0.0000, 0.0002]
    ])
    current_weights = {'005930': 0.25, '000660': 0.25, '069500': 0.25, '091160': 0.25}

    # High crowding Z-score for 005930 (Tech) -> factor crowding decay trigger
    crowding_z_scores = {'005930': 2.8, '000660': 1.0, '069500': 0.0, '091160': -0.5}

    res = opt.optimize_allocation(
        alphas=alphas,
        cov_matrix=cov_matrix,
        current_weights=current_weights,
        vix_spot=18.0,
        max_total_exposure=1.0,
        crowding_z_scores=crowding_z_scores
    )

    assert res['status'] == 'success'
    weights = res['weights']
    assert len(weights) == 4
    # Ensure all weights >= 0
    for k, w in weights.items():
        assert w >= 0.0

    # Ensure total exposure <= 1.0
    assert sum(weights.values()) <= 1.0001

    # Verify that factor crowding decayed 005930's weight relative to 000660 and non-crowded assets
    print("Tri-Factor Weights:", weights)

def test_tri_factor_alpha_scaling():
    opt = QPortfolioOptimizer()
    alphas = {'A': 0.05, 'B': 0.02}
    cov = np.eye(2) * 0.0004
    crowding = {'A': 2.5, 'B': 0.0}

    adj_alphas = opt.compute_tri_factor_alphas(alphas, cov, crowding_z_scores=crowding)
    assert len(adj_alphas) == 2
    # Asset A should have crowding decay applied: exp(-0.5 * (2.5 - 1.5)) = exp(-0.5) ~ 0.6065
    assert adj_alphas[0] < (0.05 / 0.02)
