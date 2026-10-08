"""
Unit tests for DynamicCapEngine module.
"""

import pytest
from src.allocation.dynamic_cap_engine import DynamicCapEngine


def test_dynamic_cap_computation():
    engine = DynamicCapEngine()
    # High active EV (1.5% = 0.015) -> cap shrinks near 0.15
    cap_high_alpha = engine.compute_dynamic_anchor_cap(active_ev_pct=0.015)
    assert cap_high_alpha < 0.25

    # Zero active EV (0.0) -> cap stays at base 0.50
    cap_zero_alpha = engine.compute_dynamic_anchor_cap(active_ev_pct=0.0)
    assert cap_zero_alpha == 0.50


def test_evaluate_kodex200_release_scale_up():
    engine = DynamicCapEngine()
    # moderate active_ev_pct = 0.001 (dynamic_cap ~ 0.40)
    sell_qty, release_krw, reason, metrics = engine.evaluate_kodex200_release(
        total_nav=18170000.0,
        kodex200_price=106795.0,
        kodex200_qty=103,
        active_ev_pct=0.001,
        krx_leverage_demanded_krw=4000000.0
    )
    assert sell_qty > 40
    assert release_krw > 4000000.0
    assert "Dynamic Release" in reason
    assert metrics['target_cap'] <= metrics['dynamic_cap']
