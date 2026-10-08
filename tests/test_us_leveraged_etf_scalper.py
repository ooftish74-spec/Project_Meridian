"""
Unit tests for USLeveragedETFScalper module.
"""

import pytest
from src.streams.s1_edge.us_leveraged_etf_scalper import USLeveragedETFScalper


def test_tqqq_entry_approved_bull_regime():
    scalper = USLeveragedETFScalper()
    approved, reason, signal = scalper.evaluate_entry(
        ticker="TQQQ",
        regime="bull_strong",
        vix=18.5,
        live_price=100.0,
        vwap_15m=99.5,
        volume_power=1.35,
        momentum_z=2.5,
        atr_pct=0.02
    )
    assert approved is True
    assert "3X ETF Scalp Approved" in reason
    assert signal['action'] == "BUY_LONG_3X"
    assert signal['target_profit_price'] is None  # Uncapped upside!
    assert signal['exit_rule'] == 'SAME_DAY_MARKET_CLOSE'


def test_same_day_market_close_exit():
    scalper = USLeveragedETFScalper()
    is_expired, reason = scalper.check_same_day_market_close_exit(current_time_kst="04:50")
    assert is_expired is True
    assert "당일 100% 청산" in reason

    is_active, _ = scalper.check_same_day_market_close_exit(current_time_kst="23:30")
    assert is_active is False
