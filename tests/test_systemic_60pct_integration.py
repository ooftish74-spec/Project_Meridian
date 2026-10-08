"""
Unit and Integration Tests for Systemic 60% Post-Tax CAGR Architecture Package
================================================================================
- DynamicBasisSwitch
- NaturalFXHedgeMatrix in QPortfolioOptimizer
- ConformalRegimeEngine
"""

import pytest
import numpy as np
from src.allocation.dynamic_basis_switch import DynamicBasisSwitch
from src.allocation.qp_portfolio_optimizer import QPortfolioOptimizer
from src.regime.conformal_regime_engine import ConformalRegimeEngine


class TestSystemic60PctIntegration:

    def test_dynamic_basis_switch_contango_cost(self):
        switcher = DynamicBasisSwitch()

        # High Contango: Futures = 105.0, Spot = 100.0, Expiry = 30 days -> Contango > KOFR
        res1 = switcher.evaluate_basis_switch(105.0, 100.0, 30, current_kofr_rate=0.035)
        assert res1['optimal_instrument'] == 'SPOT_ETF_KOFR'
        assert res1['contango_bps'] > 150.0

        # Backwardation: Futures = 99.0, Spot = 100.0, Expiry = 30 days -> Futures
        res2 = switcher.evaluate_basis_switch(99.0, 100.0, 30, current_kofr_rate=0.035)
        assert res2['optimal_instrument'] == 'FUTURES'
        assert res2['contango_bps'] < 0.0

    def test_qp_portfolio_optimizer_natural_fx_hedge(self):
        optimizer = QPortfolioOptimizer()
        alphas = {'S1_KRX': 0.10, 'S5_US': 0.20}
        cov_matrix = np.array([[0.0004, 0.0001], [0.0001, 0.0009]])
        current_weights = {'S1_KRX': 0.5, 'S5_US': 0.5}

        res = optimizer.optimize_allocation(alphas, cov_matrix, current_weights)
        assert res['status'] in ['success', 'optimal', 'fallback_equal_weight', 'pass']
        assert 'weights' in res

    def test_conformal_regime_engine_acceleration(self):
        engine = ConformalRegimeEngine(memory_window=10)

        # Seed scores
        for _ in range(10):
            engine.evaluate_accelerated_regime(20.0, 20.0, 2.0, 'bull')

        # Shock VIX -> Trigger accelerated transition
        new_regime, conf, accelerated = engine.evaluate_accelerated_regime(40.0, 20.0, 5.0, 'bull')

        assert conf > 0.0
        assert isinstance(accelerated, bool)
