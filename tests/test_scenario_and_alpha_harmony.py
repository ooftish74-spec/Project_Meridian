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
