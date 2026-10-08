"""
Tests for BayesianBlackLittermanMetaUpgrader
"""

import pytest
import numpy as np
from src.allocation.bayesian_bl_meta_upgrader import BayesianBlackLittermanMetaUpgrader

def test_kalman_alpha_filtering():
    upgrader = BayesianBlackLittermanMetaUpgrader()
    ticker = '005930'

    # Raw alpha sequence with noise
    raw_alphas = [0.05, 0.04, 0.08, -0.02, 0.06]
    filtered_alphas = []

    for a in raw_alphas:
        filt = upgrader.update_kalman_alpha_state(ticker, a, observation_noise_scale=1.0)
        filtered_alphas.append(filt)

    # Kalman filter should smooth out violent spikes
    assert len(filtered_alphas) == 5
    assert filtered_alphas[0] == 0.05
    assert abs(filtered_alphas[3] - (-0.02)) > 0.001  # Smoothed out

def test_black_litterman_posterior_computation(tmp_path):
    overrides_file = tmp_path / 'dynamic_overrides.json'
    upgrader = BayesianBlackLittermanMetaUpgrader(overrides_path=str(overrides_file))

    asset_names = ['005930', '000660']
    cov_matrix = np.array([[0.0004, 0.0001], [0.0001, 0.0009]])
    market_weights = np.array([0.60, 0.40])

    research_views = {'005930': 0.05, '000660': 0.08}
    shap_convictions = {'005930': 0.85, '000660': 0.40}  # 000660 has lower conviction
    execution_gaps = {'005930': 0.001, '000660': 0.008}    # 000660 has higher gap error

    mu_bl, sigma_bl, telemetry = upgrader.compute_black_litterman_posterior(
        asset_names=asset_names,
        cov_matrix=cov_matrix,
        market_weights=market_weights,
        research_views=research_views,
        shap_convictions=shap_convictions,
        execution_gaps=execution_gaps
    )

    assert len(mu_bl) == 2
    assert sigma_bl.shape == (2, 2)
    assert telemetry['status'] == 'success'

    # High uncertainty on 000660 should cause its view weight to be downweighted relative to equilibrium
    upgraded = upgrader.auto_upgrade_strategy_parameters(
        posterior_returns=dict(zip(asset_names, mu_bl)),
        uncertainty_diag=dict(zip(asset_names, telemetry['uncertainty_diag']))
    )

    assert 'bayesian_bl_alpha_multipliers' in upgraded
    assert '005930' in upgraded['bayesian_bl_alpha_multipliers']
