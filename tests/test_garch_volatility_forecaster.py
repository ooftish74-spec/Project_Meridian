"""
Tests for GARCHVolatilityForecaster
"""

import pytest
import numpy as np
from src.risk.garch_volatility_forecaster import GARCHVolatilityForecaster

def test_garch_forecaster_volatile_shock():
    forecaster = GARCHVolatilityForecaster(omega=1e-5, alpha=0.15, beta=0.80)
    np.random.seed(42)
    returns = np.random.randn(100) * 0.01
    # Add a huge shock at the end (-5%)
    returns[-1] = -0.05

    result = forecaster.forecast_next_volatility(returns)

    assert result['forecasted_daily_vol'] > 0.015  # Volatility should spike after shock
    assert result['forecasted_annual_vol'] > 0.20
    assert result['persistence'] == pytest.approx(0.95, rel=1e-3)

def test_garch_forecaster_empty():
    forecaster = GARCHVolatilityForecaster()
    result = forecaster.forecast_next_volatility(np.array([]))

    assert result['forecasted_daily_vol'] == 0.015
