"""
Unit tests for MathematicalAddOnFilter module with dynamic parameters.
"""

import pytest
from src.execution.mathematical_add_on_filter import MathematicalAddOnFilter


def test_dynamic_parameters_scaling():
    filt = MathematicalAddOnFilter()
    # High volatility regime: atr_ratio = 2.0 (0.04 / 0.02)
    params_high = filt.compute_dynamic_parameters(atr_14_pct=0.04, atr_20d_avg_pct=0.02, volume_z_score=2.0)
    assert params_high['dynamic_vwap_margin_bps'] == 20.0
    assert params_high['dynamic_max_stretch_pct'] == 0.05
    assert params_high['dynamic_min_volume_power'] == 1.20

    # Low volatility regime: atr_ratio = 0.5 (0.01 / 0.02)
    params_low = filt.compute_dynamic_parameters(atr_14_pct=0.01, atr_20d_avg_pct=0.02, volume_z_score=0.0)
    assert params_low['dynamic_vwap_margin_bps'] == 5.0
    assert params_low['dynamic_max_stretch_pct'] == 0.015
    assert params_low['dynamic_min_volume_power'] == 1.10


def test_evaluate_add_on_with_dynamic_params_approved():
    filt = MathematicalAddOnFilter()
    approved, reason, metrics = filt.evaluate_add_on(
        ticker="NVDA",
        live_price=215.0,
        vwap_15m=214.5,
        current_volume=1250.0,
        volume_ma15=1000.0,
        confidence=0.53,
        atr_14_pct=0.02,
        atr_20d_avg_pct=0.02,
        volume_z_score=1.0
    )
    assert approved is True
    assert "Dynamic Add-On Approved" in reason
    assert metrics['dynamic_vwap_margin_bps'] == 10.0


def test_evaluate_add_on_with_dynamic_params_suppressed():
    filt = MathematicalAddOnFilter()
    # live_price below dynamic threshold
    approved, reason, metrics = filt.evaluate_add_on(
        ticker="NVDA",
        live_price=214.0,
        vwap_15m=214.5,
        current_volume=1000.0,
        volume_ma15=1000.0,
        confidence=0.53,
        atr_14_pct=0.02,
        atr_20d_avg_pct=0.02,
        volume_z_score=1.0
    )
    assert approved is False
    assert "Dynamic Add-On Suppressed" in reason
