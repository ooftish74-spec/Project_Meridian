"""
Tests for Automated Alpha Incubator & Sigmoid Promotion Gate
"""

import pytest
from src.allocation.alpha_incubator import AlphaIncubator

def test_alpha_incubator_sandbox_under_obs_days():
    incubator = AlphaIncubator(min_observation_days=30)
    ic_history = [0.08] * 15  # Only 15 days

    result = incubator.evaluate_factor_promotion('new_alpha_factor_1', ic_history)

    assert result['status'] == 'SANDBOX'
    assert result['live_weight'] == 0.0
    assert result['n_observations'] == 15

def test_alpha_incubator_pruned_negative_ic():
    incubator = AlphaIncubator(min_observation_days=30)
    ic_history = [-0.05] * 35

    result = incubator.evaluate_factor_promotion('bad_alpha_factor', ic_history)

    assert result['status'] == 'PRUNED'
    assert result['live_weight'] == 0.0

def test_alpha_incubator_graduated_sigmoid_promotion():
    incubator = AlphaIncubator(min_observation_days=30, t_critical=1.699, max_live_weight=0.25)
    # Mean IC ~0.25 with small std
    ic_history = [0.25 + 0.01 * (i % 3 - 1) for i in range(35)]

    result = incubator.evaluate_factor_promotion('strong_alpha_factor', ic_history)

    assert result['status'] == 'GRADUATED'
    assert result['t_stat'] >= result['t_critical']
    assert 0.10 <= result['live_weight'] <= 0.25
