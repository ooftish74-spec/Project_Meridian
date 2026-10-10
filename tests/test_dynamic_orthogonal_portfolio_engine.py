"""
Comprehensive Unit Tests for InstitutionalAlphaPortfolioEngine.
100% Dynamic Math, Zero Hardcoded Constants Verification.
"""

import pytest
import numpy as np
import math
from src.allocation.dynamic_orthogonal_portfolio_engine import (
    DynamicAlphaOrthogonalizer,
    DynamicConstrainedPortfolioOptimizer,
    FactorRiskParityDampener,
    CrossStreamCovariancePenaltyEngine,
    InstitutionalAlphaPortfolioEngine
)


def test_dynamic_alpha_orthogonalization():
    """Verify that Pillar 1 eliminates common factor projection."""
    orthogonalizer = DynamicAlphaOrthogonalizer()
    np.random.seed(42)

    # 10 assets, 2 common factors (e.g. Market Beta, Semiconductor Sector)
    N = 10
    factor_exposures = np.zeros((N, 2))
    factor_exposures[:, 0] = np.random.uniform(0.8, 1.2, N) # Market beta
    factor_exposures[:5, 1] = 1.0  # First 5 assets are Tech/Semis
    factor_exposures[5:, 1] = 0.0  # Rest are non-tech

    # Raw alphas have high sector momentum bias
    raw_alphas = 2.5 * factor_exposures[:, 1] + np.random.normal(0, 0.2, N)

    pure_alphas = orthogonalizer.orthogonalize(raw_alphas, factor_exposures)

    assert len(pure_alphas) == N
    # The correlation between pure alphas and the sector factor should be near 0
    sector_corr = np.corrcoef(pure_alphas, factor_exposures[:, 1])[0, 1]
    assert abs(sector_corr) < 0.15  # Orthogonalization successfully extracted residual alpha


def test_dynamic_sector_bounds_and_optimization():
    """Verify that Pillar 2 enforces dynamic bounds without hardcoded limits."""
    optimizer = DynamicConstrainedPortfolioOptimizer()
    np.random.seed(42)

    # 4 sectors, 10 assets
    sectors = ['Tech', 'Tech', 'Tech', 'Tech', 'Bio', 'Bio', 'Energy', 'Energy', 'Finance', 'Finance']
    market_caps = [1000.0, 900.0, 800.0, 700.0, 300.0, 200.0, 250.0, 150.0, 200.0, 100.0]

    bounds = optimizer.compute_dynamic_sector_bounds(sectors, market_caps)
    assert 'Tech' in bounds
    assert 'Bio' in bounds
    # Bounds should be dynamically derived (strictly between 1/K and 0.95)
    for sec, cap in bounds.items():
        assert 0.20 <= cap <= 0.95

    # Run convex optimization
    pure_alphas = np.array([1.5, 1.4, 1.3, 1.2, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3])
    cov_matrix = np.eye(10) * 0.04
    # Tech assets have high cross-correlation
    cov_matrix[:4, :4] += 0.03

    weights = optimizer.optimize_weights(
        pure_alphas=pure_alphas,
        cov_matrix=cov_matrix,
        sector_ids=sectors,
        market_caps=market_caps,
        market_volatility=0.18,
        vol_surface_ratio=1.1
    )

    assert len(weights) == 10
    assert np.all(weights >= -1e-6)
    assert np.isclose(np.sum(weights), 1.0, atol=1e-3)

    # Tech sector total weight should not exceed dynamic cap
    tech_weight = np.sum(weights[:4])
    assert tech_weight <= bounds['Tech'] + 1e-3


def test_factor_risk_parity_dampening():
    """Verify that Pillar 3 dampens concentrated factor risk."""
    dampener = FactorRiskParityDampener()
    np.random.seed(42)

    N = 6
    weights = np.array([0.4, 0.3, 0.1, 0.1, 0.05, 0.05])
    cov_matrix = np.eye(N) * 0.05

    # Assets 0 and 1 have high exposure to factor 0 (Semiconductors)
    factor_loadings = np.zeros((N, 2))
    factor_loadings[:2, 0] = 1.0
    factor_loadings[2:, 1] = 1.0

    damped_w, factor_contribs = dampener.decompose_and_dampen(weights, cov_matrix, factor_loadings)

    assert len(damped_w) == N
    assert np.isclose(np.sum(damped_w), 1.0, atol=1e-3)
    # The overconcentrated assets (0 and 1) should have their weights reduced
    assert damped_w[0] < weights[0]
    assert damped_w[1] < weights[1]
    # The diversified assets should receive higher relative allocation
    assert damped_w[2] > weights[2]


def test_cross_covariance_penalty():
    """Verify that Pillar 4 penalizes candidates correlated with existing portfolio."""
    engine = CrossStreamCovariancePenaltyEngine()
    np.random.seed(42)

    T = 60
    # Existing portfolio returns
    existing_returns = np.random.normal(0.001, 0.015, T)

    # Candidate 0 is highly correlated with existing portfolio (e.g. another Tech stock)
    # Candidate 1 is uncorrelated or negatively correlated (e.g. Gold/Defensive)
    candidate_0 = existing_returns * 1.5 + np.random.normal(0, 0.005, T)
    candidate_1 = -existing_returns * 0.5 + np.random.normal(0, 0.015, T)
    candidate_returns = np.column_stack([candidate_0, candidate_1])

    # Both have identical raw alpha scores initially
    pure_alphas = np.array([2.0, 2.0])

    adjusted = engine.apply_cross_covariance_penalty(
        pure_alphas=pure_alphas,
        candidate_returns=candidate_returns,
        existing_portfolio_returns=existing_returns
    )

    assert len(adjusted) == 2
    # Candidate 0 should be penalized heavily due to high co-movement
    assert adjusted[0] < adjusted[1]


def test_end_to_end_institutional_engine():
    """Verify full end-to-end execution of all 4 pillars."""
    engine = InstitutionalAlphaPortfolioEngine()
    np.random.seed(42)

    N = 8
    raw_alphas = np.array([2.5, 2.2, 1.8, 1.5, 1.0, 0.8, 0.5, 0.2])
    sector_ids = ['Semi', 'Semi', 'Tech', 'Tech', 'Bio', 'Bio', 'Finance', 'Finance']
    
    factor_exposures = np.zeros((N, 3))
    factor_exposures[:, 0] = 1.0  # Market
    factor_exposures[:4, 1] = 1.0 # Tech/Semi
    factor_exposures[4:6, 2] = 1.0 # Bio

    cov_matrix = np.eye(N) * 0.04
    cov_matrix[:4, :4] += 0.02

    result = engine.process_and_optimize(
        raw_alphas=raw_alphas,
        factor_exposures=factor_exposures,
        cov_matrix=cov_matrix,
        sector_ids=sector_ids,
        market_volatility=0.22,
        vol_surface_ratio=1.05
    )

    assert 'pure_alphas' in result
    assert 'final_weights' in result
    assert len(result['final_weights']) == N
    assert np.isclose(np.sum(result['final_weights']), 1.0, atol=1e-3)
    # The final weights should NOT be 100% concentrated in the first two Semis!
    assert result['final_weights'][0] < 0.50


def test_endogenous_cost_and_indivisibility_penalty():
    """Verify that Price/NAV naturally suppresses expensive stocks in small accounts."""
    optimizer = DynamicConstrainedPortfolioOptimizer()
    np.random.seed(42)

    # 2 assets with identical alpha and covariance
    pure_alphas = np.array([1.5, 1.5])
    cov_matrix = np.eye(2) * 0.04
    sectors = ['SectorA', 'SectorB']
    
    # Asset 0 is high-priced (1,000,000 KRW, e.g. Samsung Biologics / KODEX CD)
    # Asset 1 is low-priced (30,000 KRW, e.g. KODEX ETF)
    asset_prices = [1000000.0, 30000.0]

    # Case 1: Small Account NAV = 20,000,000 KRW
    # Asset 0 friction = 1,000,000 / 20,000,000 = 5% penalty
    # Asset 1 friction = 30,000 / 20,000,000 = 0.15% penalty
    weights_small = optimizer.optimize_weights(
        pure_alphas=pure_alphas,
        cov_matrix=cov_matrix,
        sector_ids=sectors,
        asset_prices=asset_prices,
        nav=20000000.0
    )
    # The low-priced asset should receive significantly more weight due to low friction
    assert weights_small[1] > weights_small[0]

    # Case 2: Institutional Scale NAV = 1,000,000,000 KRW (1 Billion KRW)
    # Price/NAV friction shrinks to near zero for both
    weights_large = optimizer.optimize_weights(
        pure_alphas=pure_alphas,
        cov_matrix=cov_matrix,
        sector_ids=sectors,
        asset_prices=asset_prices,
        nav=1000000000.0
    )
    # Weights should converge toward symmetric parity (50% / 50%)
    assert abs(weights_large[0] - weights_large[1]) < abs(weights_small[0] - weights_small[1])


def test_discrete_share_allocation():
    """Verify integer share allocation within NAV budget."""
    optimizer = DynamicConstrainedPortfolioOptimizer()
    
    target_weights = np.array([0.4, 0.3, 0.3])
    prices = [100000.0, 50000.0, 20000.0]
    nav = 20000000.0  # 20 million KRW

    alloc = optimizer.compute_discrete_shares(target_weights, prices, nav)
    shares = alloc['shares']
    realized_w = alloc['realized_weights']
    cash_rem = alloc['cash_remainder']

    assert len(shares) == 3
    assert all(isinstance(s, (int, np.integer)) for s in shares)
    # Total spent must not exceed NAV
    total_spent = np.sum(shares * np.array(prices))
    assert total_spent <= nav
    assert cash_rem >= 0.0
    assert np.isclose(total_spent + cash_rem, nav)
    assert alloc['tracking_error'] >= 0.0
