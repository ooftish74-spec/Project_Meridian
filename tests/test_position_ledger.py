"""
Unit tests for PositionLedgerManager and Position entry date preservation.
"""

import os
import tempfile
import pytest
from pathlib import Path
from src.execution.position_ledger import PositionLedgerManager
from src.execution._kis_adapter import Position

def test_ledger_initialization_and_backfill():
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = Path(tmpdir) / "position_entry_ledger.json"
        mgr = PositionLedgerManager(ledger_path=str(ledger_path))

        # Clean dynamic ledger: no hardcoded ghost backfill
        assert mgr.get_entry_date("069500") is None
        assert mgr.get_entry_date("091160") is None
        assert mgr.get_entry_date("NON_EXISTENT") is None

def test_sync_live_positions():
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = Path(tmpdir) / "position_entry_ledger.json"
        mgr = PositionLedgerManager(ledger_path=str(ledger_path))

        mgr.record_entry("069500", entry_date="2026-08-25")
        mgr.record_entry("NVDA", entry_date="2026-08-25")

        # Active tickers including one new stock
        active = ["069500", "NVDA", "AAPL"]
        result = mgr.sync_live_positions(active, current_date_str="2026-09-16")

        assert result["069500"] == "2026-08-25"
        assert result["NVDA"] == "2026-08-25"
        assert result["AAPL"] == "2026-09-16"

        # Check that tickers not in active (e.g. QQQ) were removed from ledger
        assert mgr.get_entry_date("QQQ") is None
        assert mgr.get_entry_date("AAPL") == "2026-09-16"

def test_position_dataclass_to_dict():
    pos = Position(
        ticker="069500",
        quantity=52,
        avg_price=35000.0,
        current_price=36000.0,
        entry_date="2026-08-25"
    )
    d = pos.to_dict()
    assert d["ticker"] == "069500"
    assert d["quantity"] == 52
    assert d["entry_date"] == "2026-08-25"

def test_multiday_immutability():
    """Verify that entry_date remains locked across multiple consecutive sync days."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = Path(tmpdir) / "position_entry_ledger.json"
        mgr = PositionLedgerManager(ledger_path=str(ledger_path))

        mgr.record_entry("NVDA", entry_date="2026-08-25")
        active = ["NVDA", "QQQ"]
        # Day 1: 2026-09-16
        res1 = mgr.sync_live_positions(active, current_date_str="2026-09-16")
        assert mgr.get_entry_date("NVDA") == "2026-08-25"

        # Record fresh entry for TSLA
        mgr.record_entry("TSLA", entry_date="2026-09-16")
        active.append("TSLA")

        # Day 2: 2026-09-17 (syncing on next day)
        res2 = mgr.sync_live_positions(active, current_date_str="2026-09-17")
        assert res2["TSLA"] == "2026-09-16"  # MUST remain 2026-09-16, NOT updated to 2026-09-17

        # Day 5: 2026-09-20 (syncing 4 days later)
        res5 = mgr.sync_live_positions(active, current_date_str="2026-09-20")
        assert res5["TSLA"] == "2026-09-16"  # STILL remains 2026-09-16

def test_hwm_tracking_and_trail_activation():
    """Verify that peak_price and peak_pnl_pct are preserved and updated across sessions without ghost backfill."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = Path(tmpdir) / "position_entry_ledger.json"
        mgr = PositionLedgerManager(ledger_path=str(ledger_path))

        # Freshly bought 091160 starts with clean HWM (0.0), not ghost 19.93%
        mgr.record_entry("091160", entry_date="2026-10-16", entry_price=115100.0)
        hwm_091160 = mgr.get_hwm("091160")
        assert hwm_091160["peak_price"] == 115100.0
        assert hwm_091160["peak_pnl_pct"] == 0.0
        assert hwm_091160["trail_activated"] is False

        # When a stock reaches high and falls
        mgr.record_entry("TEST", entry_date="2026-10-01", entry_price=100.0)
        mgr.update_hwm("TEST", current_price=120.0, current_pnl_pct=20.0, date_str="2026-10-05")
        assert mgr.get_hwm("TEST")["peak_price"] == 120.0
        assert mgr.get_hwm("TEST")["peak_pnl_pct"] == 20.0
        assert mgr.get_hwm("TEST")["trail_activated"] is True

        # Price drops to 105 (+5%) -> Peak must remain 120 / 20%
        mgr.update_hwm("TEST", current_price=105.0, current_pnl_pct=5.0, date_str="2026-10-10")
        assert mgr.get_hwm("TEST")["peak_price"] == 120.0
        assert mgr.get_hwm("TEST")["peak_pnl_pct"] == 20.0

        # Same-Day Whipsaw Lockout recording
        mgr.record_daily_exit("091160", reason="Stop-Loss", date_str="2026-10-16", realized_pnl_pct=-1.25)
        assert "091160" in mgr.get_daily_exited_tickers("2026-10-16")
        assert mgr.get_daily_exit_details("2026-10-16")["091160"]["realized_pnl_pct"] == -1.25
        assert "091160" not in mgr.get_daily_exited_tickers("2026-10-17")


def test_mathematical_anti_churn_models():
    """Verify the 5 institutional mathematical models in DynamicQuantEngine."""
    from src.utils.dynamic_quant_engine import get_quant_engine
    dqe = get_quant_engine()

    # 1. Almgren-Chriss + statutory tax roundtrip friction: stock (005930, 18bps tax) > ETF (069500, 0bps tax)
    f_etf = dqe.compute_roundtrip_friction_pct("069500", order_value_krw=2_500_000.0, price=89600.0)
    f_stk = dqe.compute_roundtrip_friction_pct("005930", order_value_krw=2_500_000.0, price=70000.0)
    assert f_etf > 0.03
    assert f_stk > f_etf  # Stock includes 0.18% KRX transaction tax

    # 2. Drift-to-Noise & Friction Break-Even minimum holding time
    t_min = dqe.compute_optimal_min_holding_minutes("069500", order_value_krw=2_500_000.0, price=89600.0, regime="caution")
    assert 5.0 <= t_min <= 180.0

    # 3. First-Passage Optimal Stopping parameters
    opt = dqe.compute_optimal_stopping_params("091160", regime="caution", price=116000.0)
    assert opt["break_even_floor_pct"] > opt["roundtrip_friction_pct"]
    assert opt["profit_lock_trigger_pct"] > opt["trailing_trigger_pct"] > opt["break_even_floor_pct"]
    assert opt["hard_sl_limit_pct"] < 0.0

    # 4. Constantinides-Leland Cube-Root No-Trade Band
    nt_band = dqe.compute_no_trade_band_ratio("069500", order_value_krw=2_500_000.0, price=89600.0)
    assert 0.08 <= nt_band <= 0.45

    # 5. Sunk-Cost Exponential Decay Re-Entry Hurdle decays over time
    h_0m = dqe.compute_reentry_hurdle_pct("091160", elapsed_minutes=0.0, prev_realized_pnl_pct=-1.5)
    h_180m = dqe.compute_reentry_hurdle_pct("091160", elapsed_minutes=180.0, prev_realized_pnl_pct=-1.5)
    assert h_0m > h_180m > f_etf

    # 6. Grinold-Kahn Discrete-Lot Marginal Utility & Constantinides No-Trade Band
    # Tiny 1-share top-up on a 20-share position (5% change < no-trade band) is rejected
    u_tiny = dqe.compute_discrete_lot_marginal_utility(
        "091160", delta_qty=1, current_qty=20, price_krw=116000.0, nav_krw=25_000_000.0, target_value_krw=2_436_000.0
    )
    assert u_tiny["is_optimal_trade"] is False
    assert u_tiny["reason"] == "in_no_trade_band"

    # 7. Bayesian State-Divergence Trigger
    st_same = dqe.compute_state_divergence_trigger(
        {"regime": "caution", "regime_probs": {"caution": 0.8, "bull": 0.2}, "vkospi": 50.0},
        {"regime": "caution", "regime_probs": {"caution": 0.79, "bull": 0.21}, "vkospi": 50.2},
        max_unheld_signal_z=1.5,
    )
    assert st_same["should_reoptimize"] is False

    st_shift = dqe.compute_state_divergence_trigger(
        {"regime": "caution", "regime_probs": {"caution": 0.8, "bull": 0.2}, "vkospi": 50.0},
        {"regime": "bull", "regime_probs": {"caution": 0.2, "bull": 0.8}, "vkospi": 35.0},
        max_unheld_signal_z=1.5,
    )
    assert st_shift["should_reoptimize"] is True

