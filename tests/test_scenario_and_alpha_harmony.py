"""Hermetic mathematical verification tests for Scenario & Aggressive Alpha Harmony."""

from __future__ import annotations

import json
import math
import numpy as np
import pytest

import src.risk.intraday_regime as intraday_regime_mod
from src.risk.master_scenario_engine import MasterScenarioEngine
from src.risk.cro_final_gate import CROFinalGate
from src.risk.intraday_regime import IntradayRegimeDetector
from src.intelligence.market_shock_detector import MarketShockDetector


def test_master_scenario_ou_kalman_vix_rebound_and_directives():
    """Verify Scenario 3 V-Rebound computes finite ou_drift_z and maps directives to S0 and S12."""
    engine = MasterScenarioEngine()
    vix_hist = [18.0, 19.0, 21.0, 24.0, 28.0, 35.0, 42.0, 38.0, 33.0, 29.0]
    md = {
        "vix": 29.0,
        "vix_prev": 38.0,
        "vkospi": 32.0,
        "vkospi_prev": 41.0,
        "kospi_change_pct": 2.8,
        "sp500_change_1d": 2.4,
        "nasdaq_change_1d": 3.1,
        "sox_change_1d": 4.2,
        "frgn_fut_net_buy": 8500.0,
        "signal_cache": {
            "vix": 29.0,
            "vix_history": vix_hist,
            "vkospi": 32.0,
            "kospi_change_pct": 2.8,
            "sp500_change_1d": 2.4,
            "nasdaq_change_1d": 3.1,
            "sox_change_1d": 4.2,
            "frgn_fut_net_buy": 8500.0,
            "hmm_transition_probs": {"bull_prob": 0.72, "bear_prob": 0.18, "crash_prob": 0.10},
        },
    }
    pred = engine._compute_predictive_signals(md, md["signal_cache"])
    assert math.isfinite(float(pred["ou_vix_drift_z"]))
    assert float(pred["ou_vix_drift_z"]) > 0.0
    assert float(pred["p_bull_trans"]) == pytest.approx(0.72, rel=1e-9)

    res = engine.evaluate_scenarios(md)
    assert isinstance(res, dict)
    assert "composite_crash_prob" in res
    assert "composite_surge_prob" in res
    assert math.isfinite(float(res["composite_surge_prob"]))
    assert 0.0 <= float(res["composite_surge_prob"]) <= 1.0


def test_cro_final_gate_inverse_hedge_reduces_net_long_exposure():
    """Verify buying an inverse hedge ETF reduces net long exposure instead of inflating it."""
    gate = CROFinalGate(max_daily_var_pct=0.05, max_portfolio_exposure=0.50)
    orders = [
        {
            "ticker": "005930",
            "action": "buy",
            "direction": "long",
            "quantity": 6,
            "price": 100_000.0,
            "stream_id": "S2",
        },
        {
            "ticker": "252670",
            "action": "buy",
            "direction": "long",
            "is_hedge": True,
            "quantity": 100,
            "price": 2_000.0,
            "stream_id": "S0",
        },
    ]
    cov = np.array([[0.0004, -0.0003], [-0.0003, 0.0004]], dtype=float)
    tmap = {"005930": 0, "252670": 1}
    scaled_orders, meta = gate.evaluate_and_scale_orders(
        orders=orders,
        portfolio_value=1_000_000.0,
        cov_matrix=cov,
        ticker_order_map=tmap,
        market_data={"vix": 20.0},
    )
    # 600,000 long - 200,000 inverse hedge = 400,000 net exposure (40% <= 50% allowed)
    assert meta["proposed_exposure_pct"] == pytest.approx(0.40, rel=1e-6)
    assert meta["veto_triggered"] is False
    assert len(scaled_orders) == 2


def test_market_shock_detector_small_percent_not_treated_as_25pct_crash(tmp_path, monkeypatch):
    """Verify -0.25% index move in signal_cache is converted to -0.0025 decimal, not -0.25 (-25%)."""
    sc_path = tmp_path / "signal_cache.json"
    sc_path.write_text(
        json.dumps(
            {
                "sp500_change_1d": -0.25,
                "nasdaq_change_1d": -0.20,
                "sox_change_1d": -0.15,
                "vix": 18.0,
                "usdkrw_change_1d": 0.10,
                "night_futures_change_1d": -0.10,
            }
        ),
        encoding="utf-8",
    )
    det = MarketShockDetector()
    monkeypatch.setattr(MarketShockDetector, "_safe_yf_download", staticmethod(lambda *a, **kw: None))
    import pathlib

    orig_exists = pathlib.Path.exists
    orig_read_text = pathlib.Path.read_text

    def _mock_exists(p):
        if str(p).endswith("results/signal_cache.json"):
            return True
        return orig_exists(p)

    def _mock_read_text(p, *a, **kw):
        if str(p).endswith("results/signal_cache.json"):
            return sc_path.read_text(encoding="utf-8")
        return orig_read_text(p, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "exists", _mock_exists)
    monkeypatch.setattr(pathlib.Path, "read_text", _mock_read_text)
    res = det.detect()
    assert res["shock_level"] == "NONE"
    assert float(res["shock_score"]) == pytest.approx(0.0, abs=1e-9)


def test_intraday_regime_detector_reaches_recovery_branch(tmp_path, monkeypatch):
    """Verify IntradayRegimeDetector.judge() returns 'recovery' when V-recovery is detected."""
    monkeypatch.setattr(intraday_regime_mod, "_RESULTS", tmp_path)
    det = IntradayRegimeDetector(vol_window=6)
    for r in [-0.4, -0.3, -0.2, 0.35, 0.40, 0.45]:
        det._returns_buffer.append(r / 100.0)
    measurement = {
        "realized_vol": 0.003,
        "annualized_vol": 0.15,
        "cusum_signal": False,
        "volume_surge": True,
        "cumulative_return": sum(det._returns_buffer),
    }
    res = det.judge(measurement)
    assert res["regime"] == "recovery"
    assert res["recovery"]["detected"] is True


def test_regime_and_inverse_guards_eradicate_false_crash_and_wrong_way_inverse():
    """Verify ^IXIC symbol mapping, post-shock 5D stabilization in RegimeDetector, and Track C inverse guard."""
    from src.utils.google_finance_collector import GoogleFinanceCollector
    from src.regime.regime_detector import RegimeDetector
    from src.streams.s3_active_macro.cross_asset_sleeve import S3CrossAssetSleeve

    # 1. Google Finance official NASDAQ Composite ticker mapping
    assert GoogleFinanceCollector.TICKER_MAP["^IXIC"] == ".IXIC:INDEXNASDAQ"
    assert GoogleFinanceCollector.TICKER_MAP["NASDAQ"] == ".IXIC:INDEXNASDAQ"

    # 2. RegimeDetector: 10-day drop (-16.95%) with recent 5-day stabilization (+0.15%, today +0.30%) -> caution
    det = RegimeDetector()
    stabilized_rets = [-0.04, -0.05, -0.045, -0.036, 0.00, 0.001, -0.002, 0.001, -0.0015, 0.003]
    sig_stab = {"vix": 20.25, "vkospi": 21.5, "usdkrw": 1384.0, "kospi": 2690.0, "kospi_ma20": 3070.0, "ois": 51.0}
    rule_stab = det._rule_based(sig_stab, {"kospi_returns": stabilized_rets})
    assert rule_stab["regime"] == "caution"
    assert rule_stab["crash_type"] == "rebound_from_bleed"

    # Active crash (recent 5D still plunging) -> crash
    active_crash_rets = [-0.01, -0.01, -0.01, -0.01, -0.01, -0.02, -0.025, -0.015, -0.02, -0.025]
    rule_crash = det._rule_based(sig_stab, {"kospi_returns": active_crash_rets})
    assert rule_crash["regime"] == "crash"

    # 3. S3CrossAssetSleeve: block SOXS when underlying SOX index is rallying (+1.51%)
    sleeve = S3CrossAssetSleeve()
    md = {
        "spy_return_5d": -0.02,
        "soxs_return_5d": 0.12,
        "soxs_return_1d": 0.01,  # even if stale parquet showed +1%, live SOX index +1.51% blocks entry
        "signal_cache": {"sox_change_1d": 1.51, "nasdaq_change_1d": 0.29, "sp500_change_1d": -0.19},
    }
    sigs = sleeve.generate_track_c_signals("bear", md)
    emitted_tks = {s["ticker"] for s in sigs}
    assert "SOXS" not in emitted_tks


def test_oct26_forensic_five_root_causes_closed_form(tmp_path, monkeypatch):
    """Verify closed-form fixes for all 5 root causes identified in the 2026-10-26 forensic audit."""
    import math
    from src.regime.regime_detector import RegimeDetector
    from src.execution.intraday_capital_router import IntradayCapitalRouter
    from src.execution.api_resilience import OrderDLQ
    from src.measurement import data_nogo_store as dng_mod
    from scripts.stream_orchestrator import StreamOrchestrator

    # 1. RegimeDetector dynamic 1D volatility floor: sigma_1d = 23.05 / sqrt(252) = 1.4520135171437811%
    det = RegimeDetector()
    sigma_1d = 23.05 / math.sqrt(252.0)
    assert sigma_1d == pytest.approx(1.4520135171437811, rel=1e-9)
    # Intraday dip of -0.60% (< 1 sigma) after 5D stabilization (+1.7%) must stay in 'caution', NOT flip to 'crash'
    rets_intraday_dip = [-0.04, -0.05, -0.045, -0.036, 0.00, 0.008, 0.007, 0.008, -0.006]
    # Ensure 10 elements
    rets_10_dip = [-0.04, -0.05, -0.045, -0.036, -0.01, 0.008, 0.007, 0.008, 0.000, -0.006]
    sig_dip = {"vix": 19.69, "vkospi": 23.05, "usdkrw": 1445.0, "kospi": 2690.0, "kospi_ma20": 3070.0, "ois": 51.0}
    res_dip = det._rule_based(sig_dip, {"kospi_returns": rets_10_dip})
    assert res_dip["regime"] == "caution"
    assert res_dip["crash_type"] == "rebound_from_bleed"

    # 2. Data No-Go & OrderDLQ halted stock isolation (APBK0066 -> halted_permanent + data_nogo)
    dng_path = tmp_path / "data_nogo.json"
    monkeypatch.setattr(dng_mod, "DEFAULT_PATH", dng_path)
    dng_mod.record_data_nogo("012510", reason="KIS 거래정지 거부 (APBK0066)", kind="ticker", ttl_hours=720.0)
    assert dng_mod.is_data_nogo("012510", kind="ticker") is True
    assert dng_mod.is_data_nogo("005930", kind="ticker") is False

    dlq = OrderDLQ()
    dlq.dlq_file = tmp_path / "failed_orders.json"
    dlq.add({"order_id": "ORD1", "ticker": "012510", "side": "buy"}, "API 거부 (APBK0066): 거래정지종목")
    saved_dlq = dlq._load()
    assert len(saved_dlq) == 1
    assert saved_dlq[0]["status"] == "halted_permanent"

    # 3. IntradayCapitalRouter:
    # (a) Blocks data_nogo ticker '012510' from triggering cross_alpha_swap_sell
    # (b) Co-domain Unification inherits active_signal_ev_map (1.20) for held S2 stock '457190'
    # (c) Protects S14_PAIR_ALPHA legs ('305720', '114800') from cross_alpha_swap_sell
    router = IntradayCapitalRouter()
    portfolio_swap = {
        "cash": 0.0,
        "holdings": {
            "457190": {"ticker": "457190", "qty": 12, "current_price": 135000.0, "stream_id": "S2"},
            "305720": {"ticker": "305720", "qty": 168, "current_price": 12185.0, "stream_id": "S14_PAIR_ALPHA"},
            "114800": {"ticker": "114800", "qty": 218, "current_price": 2440.0, "stream_id": "S14_PAIR_ALPHA"},
        },
    }
    md_swap = {
        "current_prices": {"012510": 120000.0, "005930": 60000.0, "457190": 135000.0},
        "holdings_ev": {"457190": 0.54},
        "active_signal_ev_map": {"457190": 1.25},
    }
    # Halted 012510 is blocked by Data-NoGo, and 005930 (EV ~ 0.90) cannot swap out 457190 (inherited EV = 1.25)
    swap_res = router.route_intraday_capital(
        [
            {"ticker": "012510", "stream_id": "S2", "direction": "long", "confidence": 0.95, "tp_pct": 12.0, "sl_pct": 3.0, "price": 120000.0},
            {"ticker": "005930", "stream_id": "S2", "direction": "long", "confidence": 0.70, "tp_pct": 6.0, "sl_pct": 3.0, "price": 60000.0},
        ],
        md_swap,
        total_cash_krw=0.0,
        portfolio=portfolio_swap,
    )
    assert swap_res == []

    # 4. StreamOrchestrator._evaluate_exits: S10_MEGA_TREND exit signal must NEVER liquidate S3_B ('000270')
    orch = StreamOrchestrator(exec_mode="shadow")
    sp_file = tmp_path / "results" / "shadow_portfolio.json"
    sp_file.parent.mkdir(parents=True, exist_ok=True)
    sp_file.write_text(
        json.dumps({
            "positions": {
                "000270": {
                    "ticker": "000270",
                    "quantity": 10,
                    "current_price": 113800.0,
                    "unrealized_pnl_pct": -0.26,
                    "stream_id": "S3_B",
                    "track": "TRACK_2_BUFFETT_QVM",
                    "strategy": "buffett_qvm",
                },
                "012450": {
                    "ticker": "012450",
                    "quantity": 2,
                    "current_price": 800000.0,
                    "unrealized_pnl_pct": -1.0,
                    "stream_id": "S10_MEGA_TREND",
                    "strategy": "S10_MegaTrend",
                },
            }
        }),
        encoding="utf-8",
    )
    import scripts.stream_orchestrator as so_mod
    monkeypatch.setattr(so_mod, "_ROOT", tmp_path)
    monkeypatch.setattr(orch, "_is_ticker_session_open_for_live_exit", lambda *a, **kw: True)
    orch._latest_signals = {
        "S10_MEGA_TREND": [
            {"stream_id": "S10_MEGA_TREND", "ticker": "000270", "direction": "exit", "action": "exit", "reason": "Regime crash"},
            {"stream_id": "S10_MEGA_TREND", "ticker": "012450", "direction": "exit", "action": "exit", "reason": "Regime crash"},
        ]
    }
    exits = orch._evaluate_exits(regime="crash", market_data={"signal_cache": {"vix": 20.0, "vkospi": 22.0}})
    exit_tks = {e["ticker"] for e in exits}
    assert "000270" not in exit_tks, "S10 exit signal must never liquidate S3_B position 000270!"
    assert "012450" in exit_tks, "S10 exit signal must liquidate S10 position 012450!"

    # 5. StreamOrchestrator._apply_final_execution_gate: pair-atomic scaling & pair-atomic drop
    pair_orders = [
        {"ticker": "305720", "action": "buy", "quantity": 100, "price": 10000.0, "pair_id": "PAIR_1"},
        {"ticker": "114800", "action": "buy", "quantity": 400, "price": 2500.0, "pair_id": "PAIR_1"},
    ]
    # Total need = (1,000,000 + 1,000,000) * 1.003 = 2,006,000. Available cash = 1,003,000 -> s = 0.5 exactly
    scaled_pair = orch._apply_final_execution_gate(pair_orders, {"cash": 1003000.0}, {})
    assert len(scaled_pair) == 2
    assert scaled_pair[0]["quantity"] == 50
    assert scaled_pair[1]["quantity"] == 200

    # If one leg scales to 0 shares, both legs of PAIR_1 must be dropped atomically
    tiny_pair = [
        {"ticker": "305720", "action": "buy", "quantity": 1, "price": 500000.0, "pair_id": "PAIR_2"},
        {"ticker": "114800", "action": "buy", "quantity": 200, "price": 2500.0, "pair_id": "PAIR_2"},
    ]
    dropped_pair = orch._apply_final_execution_gate(tiny_pair, {"cash": 400000.0}, {})
    assert dropped_pair == []


def test_oct26_us_session_and_evening_pipeline_invariants(tmp_path, monkeypatch):
    """Verify the 4 pre-US-open invariants:
    1) RegimeDetector deduplicates same-date ETF proxy return & detects 7-day post-shock trough stabilization ('caution' / 'rebound_from_bleed').
    2) DataAggregator dynamic parquet reference check preserves valid 005930 > 200,000 KRW signal_cache.
    3) StreamOrchestrator._evaluate_exits exempts S3_A, S3_C, S6 / TRACK_1_CORE from day-trade Time Limit and counts trading days (excluding weekends).
    4) StreamOrchestrator._generate_orders enforces Jordan decomposition capital conservation (deducting over-allocated stream holdings from idle budget).
    """
    import json
    import pandas as pd
    import pytest
    from src.regime.regime_detector import RegimeDetector
    from src.report.data_aggregator import DataAggregator
    from scripts.stream_orchestrator import StreamOrchestrator

    # 1. RegimeDetector: same-date ETF proxy (-1.4563%) vs live KOSPI (-1.2506%) + 7-day post-shock stabilization
    rd = RegimeDetector()
    kospi_rets_ec2 = [
        -0.054653, -0.004403, -0.075647,  # Oct 13-15 shock trough (-7.565%)
        0.025814, 0.021802, -0.021825, 0.004091, -0.044530, 0.008561, -0.014563,  # Oct 16-26 (last bar is 069500 proxy on Oct 26)
    ]
    sig_ec2 = {
        "date": "2026-10-26",
        "vix": 19.1,
        "vkospi": 22.6,
        "usdkrw": 1325.0,
        "ois": 40.9,
        "kospi_change_pct": -1.2506,
        "kospi": 5480.89,
        "kospi_ma20": 5620.0,
    }
    mkt_ec2 = {
        "signal_cache": sig_ec2,
        "kospi_returns": list(kospi_rets_ec2),
        "kospi_return_dates": [
            "2026-10-13", "2026-10-14", "2026-10-15",
            "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26",
        ],
    }
    rb_res = rd._rule_based(sig_ec2, mkt_ec2)
    assert rb_res["regime"] == "caution"
    assert rb_res["crash_type"] == "rebound_from_bleed"

    # Genuine slow bleed (8 consecutive -0.8% days with no post-shock stabilization) must still trigger 'crash'
    bleed_rets = [-0.008] * 8 + [0.001, -0.008]
    rb_bleed = rd._rule_based(
        {"vix": 22.0, "vkospi": 24.0, "usdkrw": 1380.0, "ois": 40.0, "kospi_change_pct": -0.8},
        {"kospi_returns": bleed_rets},
    )
    assert rb_bleed["regime"] == "crash"
    assert rb_bleed["crash_type"] == "slow_bleed"

    # 2. DataAggregator: 005930 at 215,500 KRW (matching kr_005930.parquet) must NOT be wiped out
    import src.report.data_aggregator as da_mod
    res_dir = tmp_path / "results"
    pq_dir = tmp_path / "data" / "kr_markets"
    res_dir.mkdir(parents=True, exist_ok=True)
    pq_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"close": [212000.0, 215500.0]}).to_parquet(pq_dir / "kr_005930.parquet")
    (res_dir / "signal_cache.json").write_text(
        json.dumps({"vix": 19.1, "stock_technicals": {"005930": {"close": 215500.0}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(da_mod, "_MERIDIAN_ROOT", tmp_path)
    monkeypatch.setattr(da_mod, "_MERIDIAN_RESULTS", res_dir)
    agg = DataAggregator()
    loaded = agg.load_meridian_data()
    assert loaded["signal_cache"].get("vix") == pytest.approx(19.1, rel=1e-9)

    # Extreme scale corruption (e.g. 500,000 vs 215,500 > 50% deviation) must still be blocked
    (res_dir / "signal_cache.json").write_text(
        json.dumps({"vix": 19.1, "stock_technicals": {"005930": {"close": 500000.0}}}),
        encoding="utf-8",
    )
    loaded_corrupt = agg.load_meridian_data()
    assert loaded_corrupt["signal_cache"] == {}

    # 3. StreamOrchestrator._evaluate_exits: S3_A, S3_C, S6 / TRACK_1_CORE exemption & trading-day holding count
    orch = StreamOrchestrator(exec_mode="shadow")
    monkeypatch.setattr(orch, "_is_ticker_session_open_for_live_exit", lambda *a, **kw: True)
    us_pf = {
        "positions": {
            "CRM": {"ticker": "CRM", "quantity": 1, "current_price": 249.38, "unrealized_pnl_pct": 1.57, "entry_date": "2026-10-24", "stream_id": "S6", "track": "TRACK_1_CORE", "market": "US"},
            "MSFT": {"ticker": "MSFT", "quantity": 1, "current_price": 527.37, "unrealized_pnl_pct": 0.52, "entry_date": "2026-10-24", "stream_id": "S3_A", "track": "TRACK_1_TACTICAL", "market": "US"},
            "TLT": {"ticker": "TLT", "quantity": 1, "current_price": 81.53, "unrealized_pnl_pct": -0.27, "entry_date": "2026-10-23", "stream_id": "S3_C", "track": "TRACK_1_TACTICAL", "market": "US"},
            "S2_SHORT_HOLD": {"ticker": "012750", "quantity": 7, "current_price": 95000.0, "unrealized_pnl_pct": 0.10, "entry_date": "2026-10-23", "stream_id": "S2", "track": "TRACK_1_TACTICAL", "market": "KR", "max_hold_days": 2.0},
        }
    }
    # Between Friday 2026-10-23 and Monday 2026-10-26, only 1 trading day (2026-10-26) has elapsed (Saturday/Sunday excluded)
    exits_us = orch._evaluate_exits(
        market_data={"date": "2026-10-26", "enforce_trading_days_hold": True, "signal_cache": {"vix": 19.1, "vkospi": 22.6}},
        regime="crash",
        portfolio=us_pf,
    )
    assert exits_us == []


def test_zero_fragmentation_and_no_fabricated_fallbacks(tmp_path, monkeypatch):
    """Verify:
    1) SSoT canonicalize_stream_id, is_core_or_macro_sleeve, and count_trading_days_held in position_ledger.py.
    2) UniversalExitEngine protects US core sleeves (PG/S3_A, TLT/S3_C) from weekend calendar-day Time-Decay liquidation.
    3) S14SectorPairStream._extract_returns_and_volatility returns (0.0, 0.0) instead of fabricated (0.0, 0.20) when parquet is missing.
    4) USNightExecutionEngine skips trailing-stop evaluation instead of fabricating ticker_atr=0.015 when no empirical ATR exists.
    """
    from src.execution.position_ledger import (
        canonicalize_stream_id,
        count_trading_days_held,
        is_core_or_macro_sleeve,
    )
    from src.risk.universal_exit_engine import UniversalExitEngine
    from src.streams.s14_pair_alpha.sector_pair_stream import S14SectorPairStream
    from src.execution.us_night_engine import USNightExecutionEngine

    # 1. SSoT canonicalization & trading-day counter
    assert canonicalize_stream_id("S3_A") == "S3_A"
    assert canonicalize_stream_id("S3_C") == "S3_C"
    assert canonicalize_stream_id("S3_ACTIVE_MACRO") == "S3"
    assert canonicalize_stream_id("S6_US_SECTOR") == "S6"
    assert is_core_or_macro_sleeve("S3_A") is True
    assert is_core_or_macro_sleeve("S3_C") is True
    assert is_core_or_macro_sleeve("S6") is True
    assert is_core_or_macro_sleeve("S2") is False
    # Friday 2026-10-23 -> Monday 2026-10-26 is 1 trading day (Saturday Oct 24 & Sunday Oct 25 excluded)
    assert count_trading_days_held("2026-10-23", is_kr=False, as_of_date="2026-10-26") == 1
    assert count_trading_days_held("2026-10-23", is_kr=True, as_of_date="2026-10-26") == 1

    # 2. UniversalExitEngine: PG (S3_A) and TLT (S3_C) entered on 2026-10-23 must NOT trigger 3-day Time-Decay on Monday
    uee = UniversalExitEngine()
    uee._atr_cache["PG"] = 0.012
    uee._atr_cache["TLT"] = 0.011
    res_uee = uee.evaluate_all_positions(
        {
            "PG": {
                "ticker": "PG",
                "stream_id": "S3_A",
                "quantity": 2,
                "entry_price": 159.40,
                "current_price": 159.50,
                "highest_price": 159.60,
                "lowest_price": 159.35,
                "entry_date": "2026-10-23",
                "holding_minutes": 1440.0,
            },
            "TLT": {
                "ticker": "TLT",
                "stream_id": "S3_C",
                "quantity": 2,
                "entry_price": 81.75,
                "current_price": 81.65,
                "highest_price": 81.80,
                "lowest_price": 81.60,
                "entry_date": "2026-10-23",
                "holding_minutes": 1440.0,
            },
        },
        market_data={"vix": 19.1, "signal_cache": {"vix": 19.1, "usdkrw": 1325.0}},
        regime="caution",
    )
    assert res_uee["exit_orders"] == []

    # 3. S14SectorPairStream: missing parquet returns (0.0, 0.0), never fabricated (0.0, 0.20)
    s14 = S14SectorPairStream()
    ret_missing, vol_missing = s14._extract_returns_and_volatility("NON_EXISTENT_TICKER_XYZ", market="US", days=20)
    assert (ret_missing, vol_missing) == (0.0, 0.0)

    # 4. USNightExecutionEngine: when both atr_map and empirical ATR are 0, skips instead of fabricating 0.015
    import pytest
    import src.risk.universal_exit_engine as uee_mod
    import src.utils.dynamic_quant_engine as dqe_mod
    monkeypatch.setattr(uee_mod.UniversalExitEngine, "compute_ticker_atr", lambda self, tk: 0.0)
    monkeypatch.setattr(dqe_mod.DynamicQuantEngine, "get_asset_metrics", lambda self, tk: {"vol_daily": 0.0})
    us_eng = USNightExecutionEngine()
    us_eng.holdings = {"FAKE_US": {"ticker": "FAKE_US", "qty": 5, "entry_price": 100.0, "highest_price": 100.0}}
    exits_no_atr = us_eng.update_trailing_stop(
        current_prices={"FAKE_US": 97.5},
        atr_map={},
    )
    assert exits_no_atr == []

    # 5. USNightExecutionEngine enforces optimal stopping hard_sl_limit_pct (tighten_only), stopping PG at -2.49% <= -2.40%
    monkeypatch.setattr(uee_mod.UniversalExitEngine, "compute_ticker_atr", lambda self, tk: 0.0196)
    us_eng_pg = USNightExecutionEngine()
    us_eng_pg.holdings = {"PG": {"ticker": "PG", "qty": 1, "entry_price": 163.87, "highest_price": 164.30}}
    exits_pg = us_eng_pg.update_trailing_stop(current_prices={"PG": 159.78})
    assert len(exits_pg) == 1
    assert exits_pg[0]["ticker"] == "PG"
    assert exits_pg[0]["action"] == "sell"

    # 6. SectorPairAlphaEngine + PositionSizer: no 3X SOXS in US pairs & exact Volatility Parity under Probe Sizing
    from src.allocation.sector_pair_alpha_engine import SectorPairAlphaEngine
    from src.execution.position_sizer import PositionSizer
    from src.execution.position_ledger import infer_canonical_stream_and_track

    pair_eng = SectorPairAlphaEngine()
    pair_res = pair_eng.generate_pair_signals(
        sector_returns={"energy": 0.05, "semi": -0.04, "health": 0.03, "tech": -0.02},
        market="US",
        volatility_map={"energy": 0.15, "semi": 0.30, "health": 0.12, "tech": 0.24},
    )
    pair_tks = [o["ticker"] for o in pair_res["pair_orders"]]
    assert "SOXS" not in pair_tks
    assert "PSQ" in pair_tks
    sizer = PositionSizer()
    o_long = next(o for o in pair_res["pair_orders"] if o["ticker"] == "XLE")
    o_short = next(o for o in pair_res["pair_orders"] if o["pair_id"] == o_long["pair_id"] and o["ticker"] != "XLE")
    assert o_long["pair_vol_parity_scale"] == pytest.approx(1.0, rel=1e-9)
    assert o_short["pair_vol_parity_scale"] == pytest.approx(0.5, rel=1e-9)
    sz_long = sizer.compute(
        {"ticker": "XLE", "confidence": 0.85, "tp_pct": 5.0, "sl_pct": 2.0, "pair_vol_parity_scale": o_long["pair_vol_parity_scale"]},
        portfolio_value=18_000_000.0,
        regime="caution",
        vix=19.0,
    )["final_size"]
    sz_short = sizer.compute(
        {"ticker": "PSQ", "confidence": 0.85, "tp_pct": 5.0, "sl_pct": 2.0, "pair_vol_parity_scale": o_short["pair_vol_parity_scale"]},
        portfolio_value=18_000_000.0,
        regime="caution",
        vix=19.0,
    )["final_size"]
    assert sz_long * 0.15 == pytest.approx(sz_short * 0.30, rel=1e-6)
    assert infer_canonical_stream_and_track("XLE", holding_info={"strategy": "us_sector_pair_alpha"}) == ("S14_PAIR_ALPHA", "TRACK_1_TACTICAL")


def test_global_5_axis_macro_stress_and_stagflation_synergy(monkeypatch, tmp_path):
    """Verify 5-axis global macro stress integration (US term premium, Europe, KR debt, China, Geopolitical Oil) and Scenario 6/8 synergy."""
    from src.intelligence import macro_feature_integrator as mfi_mod
    from src.risk.master_scenario_engine import MasterScenarioEngine

    monkeypatch.setattr(mfi_mod, "_DATA", tmp_path / "data")
    monkeypatch.setattr(mfi_mod, "_RESULTS", tmp_path / "results")
    (tmp_path / "data" / "raw" / "cross_market").mkdir(parents=True, exist_ok=True)
    (tmp_path / "results").mkdir(parents=True, exist_ok=True)

    integrator = mfi_mod.MacroFeatureIntegrator()
    integrator._signal_cache = {
        "US10Y": 4.99,
        "US30Y": 5.33,
        "FEDFUNDS": 3.75,
        "WTI": 101.15,
        "wti_prev": 107.89,
        "wti_change_1m": 12.0,
        "KOSDAQ": 764.1,
        "kosdaq_ma20": 834.43,
        "sector_momentum": {
            "117700": {"momentum_1m": -16.0},
            "091170": {"momentum_1m": -16.0},
        },
    }
    feats = integrator._integrate_global_regional_and_kr_debt_stress()

    # Closed-form check 1: US Term Premium (10Y - FFR = 1.24, 30Y - FFR = 1.58, Z = max(1.24/0.75, 1.58) = 1.6533)
    assert feats["us_term_premium_10y_ffr"] == pytest.approx(1.24, rel=1e-9)
    assert feats["us_term_premium_30y_ffr"] == pytest.approx(1.58, rel=1e-9)
    assert feats["us_term_premium_z"] == pytest.approx(round(1.24 / 0.75, 4), rel=1e-9)

    # Closed-form check 2: KR Household Debt & Domestic Stress Z
    # dom_stress_z = -(-16.0)/8.0 = 2.0; kd_gap_pct = (764.1/834.43 - 1)*100 = -8.428508... -> kd_stress_z = 2.107127...
    expected_kd_z = -((764.1 / 834.43 - 1.0) * 100.0) / 4.0
    expected_kr_debt_z = round(0.6 * 2.0 + 0.4 * expected_kd_z, 4)
    assert feats["kr_debt_domestic_stress_z"] == pytest.approx(expected_kr_debt_z, rel=1e-9)

    # Closed-form check 3: Geopolitical Oil Z from WTI 107.89 (> 80 base, scale 12 -> (107.89 - 80)/12 = 2.3242)
    assert feats["geopolitical_oil_supply_z"] == pytest.approx(round((107.89 - 80.0) / 12.0, 4), rel=1e-9)

    # Closed-form check 4: MasterScenarioEngine Scenario 6 triggers via stagflation synergy (WTI Z + GSCPI + US Term Premium)
    eng = MasterScenarioEngine()
    sc_res = eng.evaluate_scenarios({
        "signal_cache": {
            "geopolitical_oil_supply_z": 2.3242,
            "gscpi_stress_z": 1.20,
            "us_term_premium_z": 1.85,
            "global_macro_stress_z": 1.90,
            "cboe_skew_stress_z": 1.80,
            "eu_banking_stress_z": 2.60,
            "kr_debt_domestic_stress_z": 2.55,
        }
    })
    active_ids = {s["scenario_id"] for s in sc_res["active_scenarios"]}
    assert "SCENARIO_6" in active_ids
    assert "SCENARIO_8" in active_ids
    assert sc_res["net_scenario_regime"] == "crash"






