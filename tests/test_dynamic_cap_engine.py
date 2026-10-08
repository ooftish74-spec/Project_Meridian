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
    assert metrics['target_cap'] <= metrics['dynamic_cap']


def test_dynamic_cap_reaches_zero_floor():
    """고정 15% 하한선 없이 알파가 강력할 때 0.0%까지 완전 감쇄 검증."""
    engine = DynamicCapEngine()
    # Very high active EV (3.0% = 0.03) -> cap shrinks completely to 0.0%
    cap_dominant_alpha = engine.compute_dynamic_anchor_cap(active_ev_pct=0.030)
    assert cap_dominant_alpha == 0.0, f"Expected 0.0, got {cap_dominant_alpha}"


def test_negative_market_trend_accelerates_liquidation():
    """시장 하향 추세(KOSPI 역배열) 시 069500 앵커의 신속한 전량 매도 해제 검증."""
    engine = DynamicCapEngine()
    # Moderate alpha (0.5%) under negative trend -> drops to 0.0%
    cap_downtrend = engine.compute_dynamic_anchor_cap(active_ev_pct=0.005, market_trend_positive=False)
    assert cap_downtrend == 0.0

    # Total liquidation test (all 52 shares released)
    sell_qty, release_krw, reason, metrics = engine.evaluate_kodex200_release(
        total_nav=20911598.0,
        kodex200_price=105650.0,
        kodex200_qty=52,
        active_ev_pct=0.015,
        krx_leverage_demanded_krw=0.0,
        market_trend_positive=False
    )
    assert sell_qty == 52, f"Expected all 52 shares to be released, got {sell_qty}"
    assert metrics['target_cap'] == 0.0

