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


def test_s3_track_c_blocks_inverse_in_caution_or_negative_1d_return():
    """S3CrossAssetSleeve must never emit SQQQ/SOXS in 'caution' regime or when 1d return is negative."""
    from src.streams.s3_active_macro.cross_asset_sleeve import S3CrossAssetSleeve

    sleeve = S3CrossAssetSleeve()
    # 1) Caution regime -> SQQQ blocked even if 5d return is positive
    sigs_caution = sleeve.generate_track_c_signals(
        "caution",
        {"spy_return_5d": -0.02, "sqqq_return_5d": 0.09, "sqqq_return_1d": 0.02},
    )
    assert all(s["ticker"] not in ("SQQQ", "SOXS") for s in sigs_caution)

    # 2) Bear regime but 1d return is negative -> SQQQ blocked
    sigs_neg_1d = sleeve.generate_track_c_signals(
        "bear",
        {"spy_return_5d": -0.02, "sqqq_return_5d": 0.09, "sqqq_return_1d": -0.015},
    )
    assert all(s["ticker"] != "SQQQ" for s in sigs_neg_1d)

    # 3) Bear regime and both 5d and 1d returns are positive -> SQQQ allowed with tp_pct/sl_pct
    sigs_bear_pos = sleeve.generate_track_c_signals(
        "bear",
        {"spy_return_5d": -0.02, "sqqq_return_5d": 0.09, "sqqq_return_1d": 0.025},
    )
    sqqq_sigs = [s for s in sigs_bear_pos if s["ticker"] == "SQQQ"]
    assert len(sqqq_sigs) == 1
    assert sqqq_sigs[0]["tp_pct"] > 0
    assert sqqq_sigs[0]["sl_pct"] > 0


def test_compute_dynamic_tp_sl_atr_unit_invariance():
    """Passing atr_pct as decimal (0.015) or percentage (1.5) must produce identical closed-form barriers."""
    import pytest
    from src.utils.dynamic_quant_engine import get_quant_engine

    dqe = get_quant_engine()
    res_dec = dqe.compute_optimal_stopping_params("005930", regime="caution", price=70000.0, atr_pct=0.015)
    res_pct = dqe.compute_optimal_stopping_params("005930", regime="caution", price=70000.0, atr_pct=1.5)
    assert res_dec["sigma_daily_pct"] == pytest.approx(1.5, rel=1e-9)
    assert res_pct["sigma_daily_pct"] == pytest.approx(1.5, rel=1e-9)
    assert res_dec["min_holding_minutes"] == pytest.approx(res_pct["min_holding_minutes"], rel=1e-9)
    assert res_dec["hard_sl_limit_pct"] == pytest.approx(res_pct["hard_sl_limit_pct"], rel=1e-9)


def test_realtime_data_bus_skips_krx_endpoints_for_us_tickers():
    """US tickers must immediately return None on KRX-only endpoints without tripping circuit breakers."""
    from src.data_collection.realtime_data_bus import RealtimeDataBus

    bus = RealtimeDataBus()
    assert bus.get_orderbook("SQQQ") is None
    assert bus.get_foreign_flow("NVDA") is None
    assert bus.get_institutional_flow("SOXS") is None


def test_universal_exit_normalizes_true_range_atr_by_sqrt_8_over_pi():
    """UniversalExitEngine must normalize True Range ATR by sqrt(8/pi) before computing optimal stopping barriers."""
    import math
    import numpy as np
    import pytest
    from src.risk.universal_exit_engine import UniversalExitEngine
    from src.utils.dynamic_quant_engine import get_quant_engine

    ue = UniversalExitEngine()
    ue._atr_cache["305720"] = 0.0614911218724778
    dqe = get_quant_engine()
    sigma_from_atr = 0.0614911218724778 / math.sqrt(8.0 / math.pi)
    expected_opt = dqe.compute_optimal_stopping_params(
        ticker="305720",
        regime="crash",
        vix_level=20.47,
        order_value_krw=1_139_880.0,
        price=13570.0,
        atr_pct=sigma_from_atr,
    )
    vq = dqe.get_vix_regime_quantiles()
    v_base = max(vq["baseline"], 10.0)
    kappa_v = math.sqrt(float(np.clip(20.47 / v_base, 0.65, 2.25)))
    kappa_r = math.exp(-0.60)
    c_rt = dqe.compute_roundtrip_friction_pct("305720", order_value_krw=1_139_880.0, price=13570.0)
    closed_form_sl = round(-(c_rt + 1.95996 * (sigma_from_atr * 100.0) * kappa_v * kappa_r), 4)
    assert expected_opt["hard_sl_limit_pct"] == pytest.approx(closed_form_sl, rel=1e-9)

    res = ue.evaluate_all_positions(
        {
            "LIVE_SSOT:305720": {
                "ticker": "305720",
                "stream_id": "LIVE_SSOT",
                "quantity": 84,
                "entry_price": 14704.119,
                "current_price": 13570.0,
                "unrealized_pnl_pct": -7.71,
                "peak_pnl_pct": -7.71,
                "holding_minutes": 1440.0,
                "market": "KR",
            }
        },
        market_data={"vix": 20.47, "signal_cache": {"vix": 20.47}},
        regime="crash",
    )
    assert len(res["exit_orders"]) == 1
    assert res["exit_orders"][0]["ticker"] == "305720"
    assert res["exit_orders"][0]["action"] == "sell"
    assert res["exit_orders"][0]["quantity"] == 84


def test_volume_weighted_hwm_conservation_on_scale_in(tmp_path):
    """When scaling into an existing position (e.g. 114800 from 12 shares @ 1209 to 358 shares @ 1263.12),
    peak_pnl_pct and peak_price must be diluted by w_old = 12/358 so UniversalExitEngine does not see
    a phantom 6.95% Chandelier drawdown 19 seconds after the buy.
    """
    ledger_file = tmp_path / "position_entry_ledger.json"
    mgr = PositionLedgerManager(ledger_path=str(ledger_file))

    # Initial 12-share position @ 1209 reaching peak 1293 (+6.95%)
    mgr.record_entry("114800", entry_date="2026-10-19", entry_price=1209.0, quantity=12.0)
    h1 = mgr.update_hwm(
        "114800",
        current_price=1293.0,
        current_pnl_pct=6.95,
        trail_activate_threshold=2.0,
        avg_price=1209.0,
        quantity=12.0,
    )
    assert h1["trail_activated"] is True
    assert h1["peak_pnl_pct"] == pytest.approx(6.95, rel=1e-9)

    # Scale in: +346 shares @ 1265 -> total 358 shares, new avg_price = 1263.12, current_price = 1263.12 (0.0% PnL)
    h2 = mgr.update_hwm(
        "114800",
        current_price=1263.12,
        current_pnl_pct=0.0,
        trail_activate_threshold=2.0,
        avg_price=1263.12,
        quantity=358.0,
    )
    w_old = 12.0 / 358.0
    expected_blended_peak_px = round(w_old * 1293.0 + (1.0 - w_old) * 1263.12, 6)
    expected_blended_peak_pnl = round(((expected_blended_peak_px - 1263.12) / 1263.12) * 100.0, 6)

    assert h2["peak_pnl_pct"] == pytest.approx(expected_blended_peak_pnl, rel=1e-9)
    assert h2["peak_price"] == pytest.approx(expected_blended_peak_px, rel=1e-9)
    assert h2["trail_activated"] is False
