"""
Tests for HistoricalScenarioStressTester
"""

import pytest
from src.risk.historical_scenario_stress_tester import HistoricalScenarioStressTester

def test_stress_tester_normal_portfolio():
    tester = HistoricalScenarioStressTester(max_stress_loss_limit_pct=0.15)
    weights = {'005930': 0.30, '000660': 0.30}
    portfolio_value = 100_000_000.0

    result = tester.simulate_scenario_shocks(weights, portfolio_value)

    assert result['stress_test_passed'] is True
    assert result['worst_scenario_name'] == '2020_COVID_PANIC'
    assert result['worst_loss_pct'] < 0.15
    assert result['suggested_scaling_factor'] == 1.0

def test_stress_tester_leveraged_exceeds_limit():
    tester = HistoricalScenarioStressTester(max_stress_loss_limit_pct=0.10)
    # High exposure and high beta
    weights = {'005930': 0.80, '000660': 0.70}
    betas = {'005930': 1.2, '000660': 1.3}
    portfolio_value = 100_000_000.0

    result = tester.simulate_scenario_shocks(weights, portfolio_value, betas)

    assert result['stress_test_passed'] is False
    assert result['worst_loss_pct'] > 0.10
    assert result['suggested_scaling_factor'] < 1.0
