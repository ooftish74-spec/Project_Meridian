"""
Unit Tests for Flawless Quant Invariance Engine (Project Meridian 3.0)
"""

import pytest
import numpy as np
from src.intelligence.flawless_quant_invariance_engine import (
    QuantumJumpParticleFilter,
    SelfFundingAntifragileOverlay,
    OrderbookSlippageInversionEngine,
    NonParametricTopologyAdapter,
    FlawlessQuantInvarianceEngine,
)


def test_quantum_jump_particle_filter_normal():
    qf = QuantumJumpParticleFilter(num_particles=50)
    res = qf.update(
        log_likelihood_shock=0.5,
        log_likelihood_steady=0.4,
        z_vix=0.1,
        z_skew=0.0,
    )
    assert "ess" in res
    assert "ess_ratio" in res
    assert "jump_triggered" in res
    assert isinstance(res["jump_triggered"], bool)
    assert 0.0 <= res["posterior_crash_prob"] <= 1.0


def test_quantum_jump_particle_filter_shock_trigger():
    qf = QuantumJumpParticleFilter(num_particles=50)
    # Force extreme log-likelihood shock shift and high VIX
    res = qf.update(
        log_likelihood_shock=5.0,
        log_likelihood_steady=0.1,
        z_vix=3.0,
        z_skew=2.0,
    )
    assert res["jump_triggered"] is True
    assert res["delta_ll"] > 2.5


def test_self_funding_antifragile_overlay():
    overlay = SelfFundingAntifragileOverlay()
    res = overlay.evaluate(
        z_vix=1.5,
        z_skew=1.0,
        vol_decay_rate=0.03,
        spot_price=100.0,
        portfolio_value=18800000.0,
    )
    assert res["vol_decay_revenue"] > 0.0
    assert res["funding_budget"] > 0.0
    assert res["is_antifragile"] is True
    assert res["min_convexity_2nd_deriv"] >= 0.0
    assert len(res["payoff_shocks"]) == 9


def test_orderbook_slippage_inversion_engine():
    engine = OrderbookSlippageInversionEngine()
    # Buy order with high bid volume (omega > 0)
    res_buy = engine.calculate_execution(
        bid_volume=8000.0,
        ask_volume=2000.0,
        mid_price=50000.0,
        intraday_volatility=50.0,
        order_side="BUY",
        z_volatility=0.2,
        z_trend=0.1,
    )
    assert res_buy["orderbook_imbalance"] > 0.5
    assert res_buy["micro_price_offset"] > 0.0
    assert res_buy["optimal_limit_price"] > 50000.0
    assert res_buy["recommended_routing"] == "MAKER_LIMIT"

    # Sell order with high ask volume
    res_sell = engine.calculate_execution(
        bid_volume=1000.0,
        ask_volume=9000.0,
        mid_price=50000.0,
        intraday_volatility=50.0,
        order_side="SELL",
        z_volatility=0.2,
        z_trend=0.1,
    )
    assert res_sell["orderbook_imbalance"] < -0.5
    # For SELL order when ask volume is heavy, seller places maker limit order slightly above mid to capture spread
    assert res_sell["micro_price_offset"] > 0.0
    assert res_sell["optimal_limit_price"] > 50000.0


def test_non_parametric_topology_adapter_1d():
    adapter = NonParametricTopologyAdapter()
    recent = np.random.normal(0.002, 0.01, 40)
    historic = np.random.normal(-0.005, 0.02, 100)
    res = adapter.compute_manifold_adaptation(recent, historic)
    assert res["wasserstein_distance"] > 0.0
    assert 0.0 <= res["adaptation_weight"] <= 1.0
    assert 0.0 <= res["topology_decay_index"] <= 1.0


def test_non_parametric_topology_adapter_2d():
    adapter = NonParametricTopologyAdapter()
    recent = np.random.normal(0.001, 0.01, (40, 3))
    historic = np.random.normal(0.000, 0.015, (100, 3))
    res = adapter.compute_manifold_adaptation(recent, historic)
    assert res["wasserstein_distance"] > 0.0
    assert isinstance(res["dynamic_covariance"], list)
    assert len(res["dynamic_covariance"]) == 3


def test_flawless_quant_invariance_engine_integration():
    master_engine = FlawlessQuantInvarianceEngine(num_particles=40)
    market_data = {
        "log_likelihood_shock": 2.0,
        "log_likelihood_steady": 0.5,
        "z_vix": 1.2,
        "z_skew": 0.8,
        "vol_decay_rate": 0.025,
        "spot_price": 75000.0,
        "portfolio_value": 18800000.0,
        "bid_volume": 6000.0,
        "ask_volume": 2000.0,
        "intraday_volatility": 100.0,
        "order_side": "BUY",
        "z_trend": 0.3,
        "recent_returns": np.random.normal(0.001, 0.01, 50),
        "historic_returns": np.random.normal(0.0005, 0.015, 150),
    }
    res = master_engine.evaluate_system_invariance(market_data)
    assert "invariance_score" in res
    assert 0.0 <= res["invariance_score"] <= 1.0
    assert "quantum_jump_filter" in res
    assert "antifragile_overlay" in res
    assert "slippage_inversion" in res
    assert "topology_adapter" in res
