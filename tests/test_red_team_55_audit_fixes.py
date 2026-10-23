"""
Regression Test Suite for the 55-Item Red Team / Devil's Advocate Full-System Audit Fixes
=========================================================================================
Verifies core P0/P1 mathematical and operational invariants across Layers 1–4.
"""

import numpy as np
import pytest


def test_l3_01_s2_etf_basket_decomposer_reads_up_prob_and_resolves_sector():
    from src.streams.s2_ml_alpha.etf_basket_decomposer import S2ETFBasketDecomposer

    decomposer = S2ETFBasketDecomposer()
    scored_stocks = [
        {"ticker": "005930", "name": "삼성전자", "up_prob": 0.72},
        {"ticker": "000660", "name": "SK하이닉스", "up_prob": 0.68},
    ]
    sigs = decomposer.decompose_to_etf_signals(scored_stocks)
    assert len(sigs) >= 1
    assert sigs[0]["ticker"] == "091160"
    assert sigs[0]["confidence"] >= 0.60


def test_l2_12_hyper_qvm_zero_variance_factor_gets_zero_weight():
    from src.strategy.hyper_qvm_engine import ModularHyperQVMEngine

    engine = ModularHyperQVMEngine()
    stocks = [
        {
            "ticker": "005930",
            "roe": 15.0,
            "ey": 0.08,
            "mom_6m": 0.12,
            "buyback_yield": 0.0,  # Constant 0 across all stocks (missing reflexivity factor)
            "pricing_power": 0.0,
        },
        {
            "ticker": "000660",
            "roe": 24.0,
            "ey": 0.11,
            "mom_6m": 0.35,
            "buyback_yield": 0.0,
            "pricing_power": 0.0,
        },
        {
            "ticker": "035420",
            "roe": 8.0,
            "ey": 0.04,
            "mom_6m": -0.10,
            "buyback_yield": 0.0,
            "pricing_power": 0.0,
        },
    ]
    ranked = engine.score_hyper_qvm_universe(stocks)
    assert len(ranked) == 3
    score_map = {r["ticker"]: r["hyper_qvm_score"] for r in ranked}
    # 000660 must strictly beat 035420 (missing reflexivity factor must NOT dominate 99.97% of weight)
    assert score_map["000660"] > score_map["005930"] > score_map["035420"]
    assert score_map["000660"] > 0.5


def test_l4_02_universal_exit_engine_respects_scaled_out_flag():
    from src.risk.universal_exit_engine import UniversalExitEngine

    uee = UniversalExitEngine()
    positions = {
        "S3:069500": {
            "ticker": "069500",
            "stream_id": "S3",
            "quantity": 25,
            "entry_price": 80000.0,
            "current_price": 80400.0,
            "highest_price": 80500.0,
            "holding_days": 16,
            "holding_minutes": 5000.0,
            "scaled_out": True,
        }
    }
    res = uee.evaluate_all_positions(positions, market_data={"vix": 18.0}, regime="caution")
    exits = res.get("exit_orders", [])
    partial_exits = [
        e for e in exits if e.get("exit_type") in ("scale_out_50pct", "time_decay_exit")
    ]
    assert len(partial_exits) == 0


def test_l4_05_cro_final_gate_buy_only_exposure_and_usd_fx():
    from src.risk.cro_final_gate import CROFinalGate

    gate = CROFinalGate()
    nav = 20_000_000.0
    orders = [
        {
            "ticker": "069500",
            "action": "sell",
            "quantity": 100,
            "price": 80000.0,
            "amount_krw": 8_000_000.0,
        },
        {
            "ticker": "TLT",
            "action": "buy",
            "quantity": 2,
            "price": 100.0,
            "fx_rate": 1380.0,
        },
    ]
    cov = np.eye(2) * 0.0001
    scaled_orders, meta = gate.evaluate_and_scale_orders(
        orders=orders,
        portfolio_value=nav,
        cov_matrix=cov,
        ticker_order_map={"069500": 0, "TLT": 1},
    )
    # Sell order must NOT increase proposed exposure; TLT buy is 2 * $100 * 1380 = 276,000 KRW (0.0138 of NAV)
    assert meta["veto_triggered"] is False
    assert abs(meta["proposed_exposure_pct"] - (276_000.0 / nav)) < 1e-4
    tlt_order = [o for o in scaled_orders if o["ticker"] == "TLT"][0]
    assert abs(tlt_order["amount_krw"] - 276_000.0) < 1.0


def test_wave2_inverse_intraday_tape_and_gap_guard_closed_form():
    from src.streams.s0_beta.beta_stream import S0BetaStream

    bs = S0BetaStream()
    # Case 1: KOSPI rebounding intraday (+0.85% >= +0.40%) -> blocked
    passed_rebound, reason_rebound = bs._evaluate_intraday_tape_and_gap_guard(
        ticker="252670",
        is_inverse=True,
        market_data={
            "_test_mode": True,
            "signal_cache": {"kospi_change_pct": 0.85},
        },
    )
    assert passed_rebound is False
    assert "Rebound Veto" in reason_rebound

    # Case 2: KOSPI falling (-1.40%) and 252670 not over-gapped -> approved
    passed_ok, reason_ok = bs._evaluate_intraday_tape_and_gap_guard(
        ticker="252670",
        is_inverse=True,
        market_data={
            "_test_mode": True,
            "signal_cache": {"kospi_change_pct": -1.40},
        },
    )
    assert passed_ok is True
    assert reason_ok == ""


def test_s3_track_c_3x_etf_intraday_reversal_veto():
    from src.streams.s3_active_macro.cross_asset_sleeve import S3CrossAssetSleeve

    sleeve = S3CrossAssetSleeve()
    # Even if 5d return of SQQQ is +9.61% in bear regime, a negative 1d return (-2.5%) must block SQQQ entry
    sigs = sleeve.generate_track_c_signals(
        regime="bear",
        market_data={
            "spy_return_5d": -0.03,
            "sqqq_return_5d": 0.0961,
            "sqqq_return_1d": -0.025,
        },
    )
    tickers = [s["ticker"] for s in sigs]
    assert "SQQQ" not in tickers


