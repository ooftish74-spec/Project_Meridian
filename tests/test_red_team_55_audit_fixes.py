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


def test_s3_us_regime_decoupling_from_kr_crash():
    from src.utils.dynamic_quant_engine import get_quant_engine

    dqe = get_quant_engine()
    vix_hist = [16.5, 17.0, 17.2, 16.8, 17.5, 18.0, 17.8, 17.1, 16.9, 17.4] * 3
    # 1) KRX is in 'crash' (VKOSPI=31.35), but US market is 'neutral' (VIX=19.05, score=-0.1757)
    eff_reg_neutral = dqe.resolve_effective_us_regime(
        "crash",
        market_data={
            "signal_cache": {"vix": 19.05, "vkospi": 31.35, "vix_history": vix_hist},
            "us_market_regime": {"regime": "neutral", "score": -0.1757, "raw": {"vix": 19.05}},
        },
    )
    assert eff_reg_neutral == "caution"

    # 2) US market is genuinely in 'crisis' (VIX=32.0, score=-0.75) -> resolves to 'crash'
    eff_reg_crisis = dqe.resolve_effective_us_regime(
        "caution",
        market_data={
            "signal_cache": {"vix": 32.0, "vix_history": vix_hist},
            "us_market_regime": {"regime": "crisis", "score": -0.75, "raw": {"vix": 32.0}},
        },
    )
    assert eff_reg_crisis == "crash"

    # 3) US market is in 'bull' (VIX=15.5, score=+0.45) -> resolves to 'bull'
    eff_reg_bull = dqe.resolve_effective_us_regime(
        "bear",
        market_data={
            "signal_cache": {"vix": 15.5, "vix_history": vix_hist},
            "us_market_regime": {"regime": "bull", "score": 0.45, "raw": {"vix": 15.5}},
        },
    )
    assert eff_reg_bull == "bull"


def test_s3_dynamic_us_tech_ai_universe_and_active_4factor_selection():
    from src.streams.s3_active_macro.active_macro_stream import S3FactorStream

    s3 = S3FactorStream()
    assert "CRM" in s3.GLOBAL_ETFS
    assert "AAPL" in s3.GLOBAL_ETFS
    assert "MSFT" in s3.GLOBAL_ETFS
    assert "GOOGL" in s3.GLOBAL_ETFS
    assert "AVGO" in s3.GLOBAL_ETFS

    vix_hist = [16.5, 17.0, 17.2, 16.8, 17.5, 18.0, 17.8, 17.1, 16.9, 17.4] * 3
    m_data = {
        "signal_cache": {"vix": 19.05, "vkospi": 31.35, "vix_history": vix_hist},
        "us_market_regime": {"regime": "neutral", "score": -0.1757, "raw": {"vix": 19.05}},
        "global_momentum": {
            "CRM": {"momentum_1m": 0.053, "momentum_3m": 0.339, "momentum_6m": 0.45, "vol_60d": 0.28},
            "AAPL": {"momentum_1m": 0.013, "momentum_3m": 0.118, "momentum_6m": 0.24, "vol_60d": 0.167},
            "MSFT": {"momentum_1m": 0.008, "momentum_3m": 0.114, "momentum_6m": 0.22, "vol_60d": 0.197},
            "TSLA": {"momentum_1m": 0.019, "momentum_3m": 0.218, "momentum_6m": 0.30, "vol_60d": 0.366},
        },
        "sector_momentum": {
            "XLE": {"momentum_1m": 0.092, "momentum_3m": 0.168, "momentum_6m": 0.15, "vol_60d": 0.232},
            "XLK": {"momentum_1m": -0.025, "momentum_3m": -0.010, "momentum_6m": 0.05, "vol_60d": 0.220},
        },
    }
    sigs = s3.generate_signals(regime="crash", market_data=m_data)
    s3_a_tickers = {s["ticker"] for s in sigs if s.get("stream_id") == "S3_A"}
    # Despite KRX regime='crash', US effective regime is 'caution' so top US Tech/AI leaders (CRM, AAPL) are selected!
    assert len(s3_a_tickers & {"CRM", "AAPL", "MSFT", "TSLA"}) >= 1
    assert "CRM" in s3_a_tickers


