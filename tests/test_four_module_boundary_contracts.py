"""
Hermetic End-to-End 4-Module & Inter-Module Boundary Contract Tests
===================================================================
Verifies closed-form mathematical invariants and boundary contracts across:
  - Module 1 (Data Collection, Feature Store & Signal Cache SSOT)
  - Module 2 (ML Ensemble, Purged Walk-Forward, Regime Quartet & Conformal Calibration)
  - Module 3 (Streams S0-S14, AlphaAllocator, PositionSizer, CROFinalGate & Ledger Attribution)
  - Module 4 (Reporting, PnL Attribution, BetaTracker & Self-Evolving Feedback Loop)
"""

import json
import numpy as np
import pandas as pd
import pytest


def test_m1_investor_flow_orthogonal_proxy_and_dte_rollover(tmp_path, monkeypatch):
    """[M1-01, M1-12] InvestorFlowCollector proxy produces distinct foreign vs inst Z-scores; DTE rolls over."""
    import src.data_collection.investor_flow_collector as ifc_mod
    from src.data_collection.investor_flow_collector import InvestorFlowCollector
    from src.data_collection.kis_data_collector import KISDataCollector

    rng = np.random.default_rng(42)
    n = 60
    closes = 50000.0 * np.cumprod(1.0 + rng.normal(0.0005, 0.018, size=n))
    opens = closes * (1.0 + rng.normal(0.001, 0.009, size=n))
    highs = np.maximum(opens, closes) * (1.0 + np.abs(rng.normal(0.005, 0.004, size=n)))
    lows = np.minimum(opens, closes) * (1.0 - np.abs(rng.normal(0.005, 0.004, size=n)))
    vols = rng.integers(100_000, 900_000, size=n).astype(float)

    ohlcv = pd.DataFrame({
        "date": pd.date_range("2026-07-01", periods=n, freq="B"),
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": vols,
    })
    kr_dir = tmp_path / "data" / "kr_markets"
    kr_dir.mkdir(parents=True, exist_ok=True)
    ohlcv.to_parquet(kr_dir / "kr_005930.parquet", index=False)
    monkeypatch.setattr(ifc_mod, "_PROJECT_ROOT", tmp_path)

    collector = InvestorFlowCollector()
    proxy_df = collector._generate_proxy("005930", lookback_days=40)
    assert proxy_df is not None and not proxy_df.empty
    # Verify foreign_net and inst_net are not scalar multiples (old bug: inst = 0.7 * foreign)
    f_std = (proxy_df["foreign_net"] - proxy_df["foreign_net"].mean()) / proxy_df["foreign_net"].std()
    i_std = (proxy_df["inst_net"] - proxy_df["inst_net"].mean()) / proxy_df["inst_net"].std()
    assert float(np.max(np.abs(f_std - i_std))) > 0.1

    # [M1-12] Option expiry DTE rollover: 2026-07-09 is the 2nd Thursday of July 2026.
    # On 2026-07-09 and 2026-07-16, DTE must roll over to August 13, 2026 (35d and 28d).
    assert KISDataCollector._get_dte("20260709") == pytest.approx(35.0, rel=1e-9)
    assert KISDataCollector._get_dte("20260716") == pytest.approx(28.0, rel=1e-9)


def test_m1_signal_cache_alias_sync_and_sanity_bounds():
    """[M1-02] MacroRealtimeRefresher synchronizes alias keys and repairs out-of-bounds index/commodity values."""
    from src.data_collection.macro_realtime_refresher import MacroRealtimeRefresher

    existing = {
        "kospi": 5550.3,
        "kospi_ma20": 6341.74,
        "kospi_change_1d": -1.25,
        "kosdaq": 815.4,
        "kosdaq_change_1d": -2.3963,
        "kosdaq_change_pct": 4.4636,  # Stale opposite-sign value that must be synced
        "sp500_change_1d": -0.52,
        "nasdaq_change_1d": -0.21,
        "sox_change_1d": 0.44,
        "put_call_ratio": 1.05,
        "usdkrw": 1326.9,
        "usdkrw_prev": 1326.5,
    }
    safe_updates = dict(existing)
    MacroRealtimeRefresher._enrich_and_sync_signal_dict(
        existing, safe_updates, MacroRealtimeRefresher._INDEX_SANITY_BOUNDS
    )

    assert existing["KOSPI"] == pytest.approx(5550.3, rel=1e-9)
    assert existing["kospi_close"] == pytest.approx(5550.3, rel=1e-9)
    assert existing["kosdaq_change_pct"] == pytest.approx(-2.3963, rel=1e-9)
    assert existing["spx_change_1d"] == pytest.approx(-0.52, rel=1e-9)
    assert existing["ixic_change_1d"] == pytest.approx(-0.21, rel=1e-9)
    assert existing["soxx_change_1d"] == pytest.approx(0.44, rel=1e-9)
    assert existing["options_pcr"] == pytest.approx(1.05, rel=1e-9)
    assert existing["kospi_trend"] == "BEAR"


def test_m2_cross_sectional_eb_collinearity_dedup_and_conformal_shrinkage():
    """[M2-01, M2-03] CrossSectionalEBClassifier skips duplicate collinear features and Conformal avoids 1.0 width."""
    from src.intelligence.conformal_predictor import AdaptiveConformalPredictor
    from src.ml.cross_sectional_eb_model import CrossSectionalEBClassifier

    rng = np.random.default_rng(123)
    n, p = 600, 10
    X = rng.standard_normal((n, p))
    # Feature 0 is true signal; Feature 1 is an exact duplicate of Feature 0 (corr = 1.0)
    signal = X[:, 0]
    X[:, 1] = X[:, 0]
    y = (signal + 0.5 * rng.standard_normal(n) > 0.0).astype(int)

    clf = CrossSectionalEBClassifier(n_blocks=5, top_k=4, ortho_strength=0.0, tree_blend=0.0)
    clf.fit(X, y)
    # Exactly one of {0, 1} may enter top_idx_, never both!
    selected_dups = [idx for idx in clf.top_idx_ if idx in (0, 1)]
    assert len(selected_dups) == 1

    # Conformal predictor on binary {0, 1} labels with predicted probs near 0.52
    cp = AdaptiveConformalPredictor(target_coverage=0.90)
    probs = np.clip(0.5 + 0.08 * rng.standard_normal(200), 0.35, 0.65)
    cp.calibrate(probs, y[:200])
    pred_interval = cp.predict_interval(np.array([0.56]))
    assert float(pred_interval["width"]) < 0.50

    # Probability shrinkage toward 0.50 preserves bullish direction (> 0.50)
    up_prob = 0.56
    meta_conf = 0.80
    shrunk_prob = 0.5 + meta_conf * (up_prob - 0.5)
    assert shrunk_prob == pytest.approx(0.548, rel=1e-9)
    assert shrunk_prob > 0.50


def test_m2_regime_quartet_harmony_and_bayesian_crash_decay(tmp_path):
    """[M2-02] OnlineBayesianParticleFilter decays crash state when calm; RegimeEngine weights sum to 1.0."""
    from src.intelligence.regime_engine import RegimeEngine
    from src.regime.online_bayesian_filter import OnlineBayesianParticleFilter

    pf = OnlineBayesianParticleFilter(num_particles=100)
    # Save a degenerate 100% crash state to disk and verify load_state() injects ergodic prior
    state_file = tmp_path / "bayesian_state.json"
    state_file.write_text(json.dumps({
        "particles": ["crash"] * 100,
        "weights": [0.01] * 100,
    }))
    assert pf.load_state(str(state_file)) is True
    # Run 5 calm updates (vkospi=18.0, ofi_z=0.2, foreign_flow=+500)
    for _ in range(5):
        res = pf.update({"vkospi": 18.0, "ofi_z": 0.2, "lead_lag": 0.1, "foreign_net_buy": 500.0})
    assert res["probabilities"]["crash"] < 0.25
    assert res["dominant_regime"] != "crash"

    # RegimeEngine score weights sum to 1.0 and block crash plurality when VIX=19.45 & VKOSPI=23.05
    engine = RegimeEngine()
    measurements = {
        "vix": 19.45,
        "vkospi": 23.05,
        "kospi_trend": "down",
        "kospi_ma20_dist": -13.36,
        "kospi_volatility": 48.7,
        "usdkrw": 1326.9,
        "usdkrw_prev": 1326.5,
        "macro_composite": -0.20,
    }
    scores = engine._score(measurements)
    assert sum(scores.values()) == pytest.approx(1.0, rel=1e-6)
    regime, _ = engine._decide(scores)
    assert regime in ("caution", "bear")


def test_m3_canonical_holding_attribution_sizer_and_cro_inverse_hedge():
    """[M3-01, M3-02, M3-05] Canonical holding stream inference, S3_B tp_pct=100 sizing, and CRO inverse VaR reduction."""
    from src.execution.position_ledger import infer_canonical_stream_and_track
    from src.execution.position_sizer import PositionSizer
    from src.risk.cro_final_gate import CROFinalGate

    assert infer_canonical_stream_and_track("USO")[0] == "S3"
    assert infer_canonical_stream_and_track("MSFT")[0] == "S6"
    assert infer_canonical_stream_and_track("091180")[0] == "S5"
    s3b_stream, s3b_track = infer_canonical_stream_and_track("000270")
    assert s3b_stream == "S3_B"
    assert s3b_track == "TRACK_2_BUFFETT_QVM"

    # [M3-02] S3_B signal with tp_pct=100.0, sl_pct=30.0 must preserve positive Kelly EV (80.0 - 6.0 = 74.0%)
    sizer = PositionSizer()
    s3b_sig = {
        "ticker": "000270",
        "stream": "S3_B",
        "stream_id": "S3_B",
        "track": "TRACK_2_BUFFETT_QVM",
        "tp_pct": 100.0,
        "sl_pct": 30.0,
        "win_prob": 0.62,
        "confidence": 0.80,
        "expected_return": 0.35,
    }
    sized = sizer.compute(s3b_sig, portfolio_value=10_000_000, regime="caution", vix=19.45)
    assert sized["approved"] is True
    assert sized["ev_pct"] == pytest.approx(74.0, rel=1e-9)
    assert sized["tp_pct"] == pytest.approx(100.0, rel=1e-9)
    assert sized["sl_pct"] == pytest.approx(30.0, rel=1e-9)
    assert sized["final_size"] > 0.0

    # [M3-05] CROFinalGate single-factor beta covariance: adding inverse hedge reduces VaR
    gate = CROFinalGate()
    long_only_orders = [
        {"ticker": "005930", "action": "buy", "price": 60000, "quantity": 50, "amount_krw": 3_000_000},
        {"ticker": "000660", "action": "buy", "price": 150000, "quantity": 20, "amount_krw": 3_000_000},
    ]
    hedged_orders = long_only_orders + [
        {"ticker": "114800", "action": "buy", "price": 5000, "quantity": 400, "amount_krw": 2_000_000},
    ]
    _, audit_long = gate.evaluate_and_scale_orders(
        long_only_orders, portfolio_value=10_000_000, market_data={"vix": 24.0}
    )
    _, audit_hedged = gate.evaluate_and_scale_orders(
        hedged_orders, portfolio_value=10_000_000, market_data={"vix": 24.0}
    )
    assert audit_hedged["proposed_var_pct"] < audit_long["proposed_var_pct"]


def test_m4_brinson_fachler_attribution_beta_ddof1_and_genetic_tighten_only(tmp_path):
    """[M4-02, M4-03] Brinson-Fachler closed-form attribution, sample Beta ddof=1, and Genetic tighten_only."""
    from src.allocation.pnl_attribution import PnLAttributionEngine
    from src.learning.genetic_rule_evolver import GeneticRuleEvolver
    from src.measurement.beta_tracker import BetaTracker

    # Closed-form Brinson-Fachler verification:
    # A_s = (w_p - w_b) * (r_p - r_b_total)
    attr = PnLAttributionEngine()
    sids = attr.STREAMS  # e.g. ['S1', 'S2', 'S3', 'S4']
    n_s = len(sids)
    w_b = 1.0 / n_s
    raw_data = {sid: {"daily_returns": [0.01, 0.01]} for sid in sids}
    raw_data[sids[0]] = {"daily_returns": [0.02, 0.02]}  # r_p(sids[0]) = 0.04, others = 0.02
    weights = {sid: w_b for sid in sids}
    weights[sids[0]] = w_b + 0.10
    weights[sids[1]] = w_b - 0.10
    r_b_total = (0.04 + (n_s - 1) * 0.02) / n_s
    expected_s0_alloc = 0.10 * (0.04 - r_b_total) * 100.0
    brinson = attr._brinson_decomposition({"raw_data": raw_data}, weights)
    assert brinson["stream_effects"][sids[0]]["allocation_effect"] == pytest.approx(expected_s0_alloc, rel=1e-4)

    # Sample beta ddof=1 verification: if p_rets == 2.0 * b_rets, rolling beta must be exactly 2.000000000
    b_rets = np.array([0.01, -0.02, 0.015, -0.005, 0.02, -0.01, 0.008, -0.012, 0.005, 0.011])
    p_rets = 2.0 * b_rets
    bt = BetaTracker()
    bt._history = [
        {"portfolio_ret_pct": float(pr), "benchmark_ret_pct": float(br)}
        for pr, br in zip(p_rets, b_rets)
    ]
    beta_val = bt._rolling_beta(window=10)
    assert beta_val == pytest.approx(2.0, rel=1e-9)

    # GeneticRuleEvolver tighten_only invariant on exit.sl_atr_multiplier
    evolver = GeneticRuleEvolver()
    clamped = evolver._enforce_tighten_only(
        {"exit.sl_atr_multiplier": 2.3978},
        baseline={"exit.sl_atr_multiplier": 1.2},
    )
    assert clamped["exit.sl_atr_multiplier"] == pytest.approx(1.2, rel=1e-9)
    mutated = evolver._mutate(
        {"exit.sl_atr_multiplier": 1.25, "execution.us_3x_min_vp": 130.0, "execution.us_3x_min_z": 2.0, "exit.trailing_tp_trigger": 2.5, "regime.leverage.ois_min": 55.0},
        baseline={"exit.sl_atr_multiplier": 1.25},
    )
    assert float(mutated["exit.sl_atr_multiplier"]) <= 1.25


def test_residual_risk_prescriptions_cold_start_shrinkage_and_sigma_tighten_only(monkeypatch):
    """Verify Empirical-Bayes cold-start stream shrinkage (n_0=20, n_prior=5 -> 0.20x) and sigma_adj <= 1.0 in caution regime."""
    from src.allocation.alpha_allocator import AlphaAllocator
    from src.risk.exposure_orchestrator import ExposureOrchestrator

    alloc = AlphaAllocator()
    zero_stream = {"n_days": 0, "total_trades": 0, "active_positions": 0, "daily_returns": []}
    live_like_metrics = {
        "S0": dict(zero_stream),
        "S1": dict(zero_stream),
        "S2": {"n_days": 9, "total_trades": 42, "active_positions": 6, "win_rate": 0.6667, "avg_win": 0.015, "avg_loss": 0.012, "daily_returns": [0.01, -0.005, 0.012, 0.008, -0.004, 0.009]},
        "S3": {"n_days": 30, "total_trades": 7, "active_positions": 4, "win_rate": 0.60, "avg_win": 0.010, "avg_loss": 0.008, "daily_returns": [0.008, 0.006, -0.003, 0.009, 0.005, 0.004]},
        "S5": {"n_days": 3, "total_trades": 5, "active_positions": 1, "win_rate": 0.50, "avg_win": 0.005, "avg_loss": 0.004, "daily_returns": [0.002, 0.001, 0.002]},
        "S10_MEGA_TREND": dict(zero_stream),
        "S11_HIGHBETA_SNIPER": dict(zero_stream),
        "S12_DERIVATIVE_SQUEEZE": dict(zero_stream),
        "S13_JAPAN_ALPHA": dict(zero_stream),
        "S14_PAIR_ALPHA": dict(zero_stream),
    }
    weights = alloc.allocate(live_like_metrics, regime="caution")
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-2)
    idle_offensive_sum = (
        weights.get("S1", 0.0)
        + weights.get("S10_MEGA_TREND", 0.0)
        + weights.get("S11_HIGHBETA_SNIPER", 0.0)
        + weights.get("S12_DERIVATIVE_SQUEEZE", 0.0)
        + weights.get("S13_JAPAN_ALPHA", 0.0)
        + weights.get("S14_PAIR_ALPHA", 0.0)
    )
    # Idle zero-evidence offensive streams must be shrunk to 0.0 (previously 41.3%+)
    assert idle_offensive_sum == pytest.approx(0.0, abs=1e-6)
    assert weights.get("S2", 0.0) > weights.get("S11_HIGHBETA_SNIPER", 0.0)

    # ExposureOrchestrator sigma_adjustment must enforce tighten_only (<= 1.0) in non-bull regimes
    eo = ExposureOrchestrator()
    res = eo.calculate(sentiment={"regime": "caution", "vix": 19.45, "vkospi": 23.05, "ois": 63.1, "options_skew": 0.1})
    assert float(res["sigma_adjustment"]) <= 1.0

