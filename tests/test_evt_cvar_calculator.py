"""
Tests for EVTCVaRCalculator (Cornish-Fisher VaR & CVaR)
"""

import pytest
import numpy as np
from src.risk.evt_cvar_calculator import EVTCVaRCalculator

def test_evt_cvar_calculator_normal_dist():
    calc = EVTCVaRCalculator(confidence_level=0.99)
    np.random.seed(42)
    returns = np.random.randn(200) * 0.015  # Normal returns with 1.5% std

    result = calc.calculate_cf_var_and_cvar(
        returns_history=returns,
        portfolio_value=100_000_000.0,
        portfolio_weights=np.array([1.0]),
        asset_returns_matrix=returns.reshape(-1, 1)
    )

    assert result['cf_var_pct'] > 0
    assert result['cvar_pct'] >= result['cf_var_pct']  # CVaR is always >= VaR
    assert result['cf_var_krw'] > 0
    assert result['cvar_krw'] > 0

def test_evt_cvar_calculator_fat_tailed():
    calc = EVTCVaRCalculator(confidence_level=0.99)
    np.random.seed(42)
    # Fat tailed returns (Student-t with df=3)
    returns = np.random.standard_t(df=3, size=300) * 0.015

    result = calc.calculate_cf_var_and_cvar(
        returns_history=returns,
        portfolio_value=100_000_000.0,
        portfolio_weights=np.array([1.0]),
        asset_returns_matrix=returns.reshape(-1, 1)
    )

    assert result['kurtosis'] > 0  # Fat tail excess kurtosis
    assert result['cf_var_pct'] > 0
    assert result['cvar_pct'] >= result['cf_var_pct']
