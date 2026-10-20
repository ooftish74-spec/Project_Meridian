"""Hermetic tests for US Trailing Stop exit ledger recording, cross-midnight session lookback,
and closed-form Sunk-Cost Exponential Decay Re-Entry Hurdle (SQQQ whip-saw prevention).
"""

import json
import math
from datetime import datetime, timedelta
import pytest

from src.execution.position_ledger import PositionLedgerManager
from src.utils.dynamic_quant_engine import DynamicQuantEngine


def test_purge_closed_positions_auto_records_daily_exit(tmp_path):
    """When a held position (e.g. SQQQ) is closed and purged on balance sync,
    it must be automatically recorded in daily_exited_tickers.json if not yet present.
    """
    ledger_file = tmp_path / "position_entry_ledger.json"
    mgr = PositionLedgerManager(ledger_path=str(ledger_file))

    mgr.record_entry("SQQQ", entry_date="2026-10-19", entry_price=37.38)
    mgr.update_hwm("SQQQ", current_price=37.58, current_pnl_pct=0.535, trail_activate_threshold=2.0)

    # Simulate SQQQ sold and removed from active KIS positions
    mgr.purge_closed_positions(active_tickers={"KO", "TLT", "TMUS", "XLV"})

    exited = mgr.get_daily_exited_tickers()
    assert "SQQQ" in exited
    details = mgr.get_daily_exit_details()
    assert "SQQQ" in details
    assert details["SQQQ"]["realized_pnl_pct"] == pytest.approx(0.535, rel=1e-6)


def test_cross_midnight_continuous_us_session_lookback(tmp_path):
    """An exit at 23:07 KST on day D must remain visible in get_daily_exit_details()
    after midnight (e.g. 00:30 KST on day D+1) within the 16-hour lookback window.
    """
    ledger_file = tmp_path / "position_entry_ledger.json"
    mgr = PositionLedgerManager(ledger_path=str(ledger_file))

    now_dt = datetime.now()
    prev_day_str = (now_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    recent_exit_dt = now_dt - timedelta(hours=2)  # 2 hours ago, stored under yesterday's date key
    old_exit_dt = now_dt - timedelta(hours=20)    # 20 hours ago (> 16h lookback)

    payload = {
        prev_day_str: {
            "SQQQ": {
                "first_exited_at": recent_exit_dt.isoformat(),
                "exited_at": recent_exit_dt.isoformat(),
                "reason": "US Night Dynamic Trailing Stop (-3.03%)",
                "realized_pnl_pct": 0.53,
                "exit_count": 1,
            },
            "OLDTK": {
                "first_exited_at": old_exit_dt.isoformat(),
                "exited_at": old_exit_dt.isoformat(),
                "reason": "Old Exit",
                "realized_pnl_pct": -1.2,
                "exit_count": 1,
            },
        }
    }
    mgr.daily_exit_path.write_text(json.dumps(payload), encoding="utf-8")

    details = mgr.get_daily_exit_details(lookback_hours=16.0)
    assert "SQQQ" in details
    assert "OLDTK" not in details


def test_record_daily_exit_60s_idempotency(tmp_path):
    """Calling record_daily_exit twice within 60 seconds (e.g. USNightEngine + LiveTraderDaemon)
    must not double-count exit_count.
    """
    ledger_file = tmp_path / "position_entry_ledger.json"
    mgr = PositionLedgerManager(ledger_path=str(ledger_file))

    mgr.record_daily_exit("SQQQ", reason="Position closed on live balance sync", realized_pnl_pct=0.0)
    mgr.record_daily_exit("SQQQ", reason="US Night Dynamic Trailing Stop (-3.03%)", realized_pnl_pct=0.53)

    details = mgr.get_daily_exit_details()
    assert details["SQQQ"]["exit_count"] == 1
    assert details["SQQQ"]["reason"] == "US Night Dynamic Trailing Stop (-3.03%)"
    assert details["SQQQ"]["realized_pnl_pct"] == pytest.approx(0.53, rel=1e-9)


def test_closed_form_reentry_hurdle_blocks_18min_sqqq_rebuy(monkeypatch):
    """Verify exact closed-form Sunk-Cost Exponential Decay Re-Entry Hurdle formula:
      H_reentry(dt) = C_RT + (2 * C_RT + |min(0, PnL_prev)| + z_0.99 * sigma_d_pct) * exp(-dt / 390)
    """
    dqe = DynamicQuantEngine()
    monkeypatch.setattr(
        dqe,
        "get_asset_metrics",
        lambda tk: {"vol_daily": 0.042, "adt": 500_000_000.0, "ir": 0.1, "mu_ann": 0.0},
    )
    elapsed_m = 18.6
    prev_pnl = 0.53
    order_val_krw = 51_500.0
    price_usd = 37.30

    c_rt = dqe.compute_roundtrip_friction_pct("SQQQ", order_value_krw=order_val_krw, price=price_usd)
    sigma_d_pct = 4.2
    z_99 = 2.32635
    decay = math.exp(-elapsed_m / 390.0)
    expected_hurdle = round(c_rt + (2.0 * c_rt + 0.0 + z_99 * sigma_d_pct) * decay, 4)

    actual_hurdle = dqe.compute_reentry_hurdle_pct(
        "SQQQ",
        elapsed_minutes=elapsed_m,
        prev_realized_pnl_pct=prev_pnl,
        order_value_krw=order_val_krw,
        price=price_usd,
    )
    assert actual_hurdle == pytest.approx(expected_hurdle, rel=1e-9)
    # Typical S3 signal EV (~3.9%) must be strictly below the ~9.6% hurdle 18.6 minutes after exit
    assert 3.90 < actual_hurdle


def test_leverage_judge_preserves_profitable_inverse_in_crash_regime():
    """In crash/bear regime, a profitable inverse ETF (252670 at +7.69%) must NOT be
    prematurely liquidated even if US VIX is 18.4 (< 22.0).
    """
    from src.risk.leverage_judge import LeverageJudge

    judge = LeverageJudge()
    positions = {
        "252670": {
            "ticker": "252670",
            "name": "KODEX 200선물인버스2X",
            "quantity": 967,
            "avg_price": 104.0,
            "current_price": 112.0,
            "peak_price": 112.0,
            "entry_date": "2026-10-19",
        }
    }
    market_data = {
        "date": "2026-10-20",
        "signal_cache": {"vix": 18.4, "vkospi": 24.5},
    }
    exits = judge.check_etf_stops(positions, market_data, regime="crash")
    assert exits == []


def test_leverage_judge_trailing_stop_captures_inverse_profit_on_reversal():
    """When a profitable inverse ETF (252670) reverses from its peak (115 -> 109, -5.22% drawdown),
    check_etf_stops must trigger the ATR-based Trailing Stop.
    """
    from src.risk.leverage_judge import LeverageJudge

    judge = LeverageJudge()
    positions = {
        "252670": {
            "ticker": "252670",
            "name": "KODEX 200선물인버스2X",
            "quantity": 967,
            "avg_price": 104.0,
            "current_price": 109.0,
            "peak_price": 115.0,
            "entry_date": "2026-10-19",
        }
    }
    market_data = {
        "date": "2026-10-20",
        "signal_cache": {"vix": 18.4, "vkospi": 20.0},
    }
    exits = judge.check_etf_stops(positions, market_data, regime="crash")
    assert len(exits) == 1
    assert exits[0]["ticker"] == "252670"
    assert "Trailing Stop" in exits[0]["reason"]


def test_leverage_judge_macro_stabilization_exits_losing_inverse_in_bull_regime():
    """When regime has normalized to bull, both VIX and VKOSPI are below Q_caution,
    and the inverse position is losing (pnl_pct <= 0), Macro Stabilization exit fires.
    """
    from src.risk.leverage_judge import LeverageJudge

    judge = LeverageJudge()
    positions = {
        "252670": {
            "ticker": "252670",
            "name": "KODEX 200선물인버스2X",
            "quantity": 967,
            "avg_price": 104.0,
            "current_price": 102.5,
            "peak_price": 104.0,
            "entry_date": "2026-10-19",
        }
    }
    market_data = {
        "date": "2026-10-20",
        "signal_cache": {"vix": 13.5, "vkospi": 13.0, "vix_history": [18.0] * 25},
    }
    exits = judge.check_etf_stops(positions, market_data, regime="bull")
    assert len(exits) == 1
    assert exits[0]["ticker"] == "252670"
    assert "Macro 안정화" in exits[0]["reason"]

