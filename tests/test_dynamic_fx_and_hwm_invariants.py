"""Hermetic closed-form unit tests for Dynamic FX Ornstein-Uhlenbeck + Ito Allocation,
Ghost HWM Contamination Prevention, and RegimeEngine Macro History SSOT Fallback.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.allocation.alpha_allocator import AlphaAllocator
from src.execution.position_ledger import PositionLedgerManager
from src.execution.us_night_engine import USNightExecutionEngine
from src.intelligence.regime_engine import RegimeEngine
from src.utils.dynamic_quant_engine import DynamicQuantEngine


def test_ornstein_uhlenbeck_fx_dynamics_closed_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify get_fx_distribution_and_dynamics against hand-computed closed-form OU & momentum equations."""
    dqe = DynamicQuantEngine()

    # Construct deterministic 60-day log-FX series around exp(7.23) ~ 1380.22
    idx = pd.date_range("2026-08-01", periods=60, freq="B")
    t_arr = np.arange(60, dtype=float)
    log_vals = 7.23 + 0.02 * np.sin(t_arr * 0.25)
    fx_vals = np.exp(log_vals)
    fx_series = pd.Series(fx_vals, index=idx)

    monkeypatch.setattr(dqe, "_load_fx_close_series", lambda tail_n=252: fx_series)
    # Prevent external index correlation file reads so rho_us_fx == 0, rho_kr_fx == 0 deterministically
    monkeypatch.setattr(dqe, "_load_close_and_adt", lambda path, tail_n=120: None)

    spot_fx = 1436.0
    res = dqe.get_fx_distribution_and_dynamics(spot_fx=spot_fx)

    # Hand-compute closed-form values on historical series + spot evaluation
    log_fx = np.log(fx_vals)
    n_obs = float(len(log_fx))
    mu_x = float(np.mean(log_fx))
    sigma_x = float(np.std(log_fx, ddof=1))
    z_fx = (math.log(spot_fx) - mu_x) / sigma_x

    y = log_fx - mu_x
    phi_raw = float(np.dot(y[1:], y[:-1]) / np.dot(y[:-1], y[:-1]))
    phi_1 = min(1.0 - 1.0 / n_obs, max(math.exp(-1.0), phi_raw))
    theta_ou = -math.log(phi_1)
    half_life = math.log(2.0) / theta_ou
    mu_fx_ou = -252.0 * theta_ou * (math.log(spot_fx) - mu_x)

    fx_rets = np.diff(log_fx)
    sig_daily = float(np.std(fx_rets, ddof=1))
    sig_ann = sig_daily * math.sqrt(252.0)
    recent_20 = fx_rets[-20:]
    mean_20 = float(np.mean(recent_20))
    t_mom = abs(mean_20) / (sig_daily / math.sqrt(20.0))
    mu_fx_mom = (t_mom / (1.0 + t_mom)) * mean_20 * 252.0

    w_ou = (z_fx * z_fx) / (1.0 + z_fx * z_fx)
    mu_fx_exp = w_ou * mu_fx_ou + (1.0 - w_ou) * mu_fx_mom
    quality = n_obs / (n_obs + 20.0)

    assert res["spot_fx"] == pytest.approx(round(spot_fx, 4), rel=1e-9)
    assert res["eq_fx_mean"] == pytest.approx(round(math.exp(mu_x), 4), rel=1e-9)
    assert res["fx_z_score"] == pytest.approx(round(z_fx, 4), rel=1e-9)
    assert res["ou_theta_daily"] == pytest.approx(round(theta_ou, 6), rel=1e-9)
    assert res["ou_half_life_days"] == pytest.approx(round(half_life, 2), rel=1e-9)
    assert res["expected_fx_ou_drift_ann"] == pytest.approx(round(mu_fx_ou, 6), rel=1e-9)
    assert res["expected_fx_mom_drift_ann"] == pytest.approx(round(mu_fx_mom, 6), rel=1e-9)
    assert res["expected_fx_drift_ann"] == pytest.approx(round(mu_fx_exp, 6), rel=1e-9)
    assert res["fx_vol_ann"] == pytest.approx(round(sig_ann, 6), rel=1e-9)
    assert res["quality"] == pytest.approx(round(quality, 4), rel=1e-9)
    # At elevated spot_fx = 1436 > eq_fx_mean ~ 1380, z_fx > 0 and OU drift is strictly negative
    assert z_fx > 1.0
    assert mu_fx_ou < 0.0


def test_ito_global_weights_and_usd_wallet_boundary_closed_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Ito 2nd-order multi-currency return/variance and USD wallet non-stranding cap."""
    dqe = DynamicQuantEngine()
    monkeypatch.setattr(dqe, "_ensure_calibrated", lambda force=False: None)

    fake_fx_dyn = {
        "spot_fx": 1436.0,
        "eq_fx_mean": 1385.0,
        "fx_z_score": 1.35,
        "expected_fx_drift_ann": -0.04,
        "fx_vol_ann": 0.08,
        "rho_us_fx": -0.25,
        "rho_kr_fx": -0.40,
        "quality": 0.80,
    }
    monkeypatch.setattr(dqe, "get_fx_distribution_and_dynamics", lambda signal_cache=None, spot_fx=None: fake_fx_dyn)

    krx_m = {"ret": 0.10, "vol": 0.20}
    us_m = {"ret": 0.14, "vol": 0.16}
    regime_probs = {"bull": 0.20, "caution": 0.50, "bear": 0.20, "crash": 0.10}
    acct_state = {
        "total_equity": 19_180_000.0,
        "usd_cash": 1520.30,
        "us_positions_value_krw": 5_859_600.0,
    }

    res = dqe.compute_fx_adjusted_global_weights(
        krx_metrics=krx_m,
        us_metrics=us_m,
        regime="caution",
        regime_probs=regime_probs,
        account_state=acct_state,
    )

    # Hand-computed closed-form oracle
    cov_us_fx = -0.25 * 0.16 * 0.08
    mu_us_krw = 0.14 + (-0.04) + cov_us_fx
    sig_us_krw = math.sqrt(0.16**2 + 0.08**2 + 2.0 * cov_us_fx)
    mu_kr_adj = 0.10 + (-0.40) * (-0.04)
    sig_kr = 0.20

    inv_kr = 1.0 / sig_kr
    inv_us = 1.0 / sig_us_krw
    w_us_rp = inv_us / (inv_kr + inv_us)

    score_kr = inv_kr * math.exp(math.tanh(mu_kr_adj / sig_kr))
    score_us = inv_us * math.exp(math.tanh(mu_us_krw / sig_us_krw))
    w_us_boltz = score_us / (score_kr + score_us)

    w_us_target = 0.20 * w_us_rp + 0.80 * w_us_boltz
    w_kr_target = 1.0 - w_us_target

    expected_energy = 0.20 * 0.0 + 0.50 * 0.5 + 0.20 * 1.0 + 0.10 * 2.0
    kappa = math.exp(-expected_energy)
    w_wallet = (1520.30 * 1436.0 + 5_859_600.0) / 19_180_000.0
    fx_decay = math.exp(-1.35)
    cap_expected = max(kappa * w_us_target * fx_decay, kappa * w_wallet)

    assert res["mu_us_krw"] == pytest.approx(round(mu_us_krw, 6), rel=1e-9)
    assert res["sig_us_krw"] == pytest.approx(round(sig_us_krw, 6), rel=1e-9)
    assert res["mu_kr_adj"] == pytest.approx(round(mu_kr_adj, 6), rel=1e-9)
    assert res["w_us_risk_parity"] == pytest.approx(round(w_us_rp, 4), rel=1e-9)
    assert res["w_us_boltzmann"] == pytest.approx(round(w_us_boltz, 4), rel=1e-9)
    assert res["w_us"] == pytest.approx(round(w_us_target, 4), rel=1e-9)
    assert res["w_krx"] == pytest.approx(round(w_kr_target, 4), rel=1e-9)
    assert res["kappa_regime"] == pytest.approx(round(kappa, 4), rel=1e-9)
    assert res["w_usd_wallet"] == pytest.approx(round(w_wallet, 4), rel=1e-9)
    assert res["us_exposure_cap_pct"] == pytest.approx(round(cap_expected, 4), rel=1e-9)

    # Verify AlphaAllocator delegation
    alloc = AlphaAllocator()
    alloc_res = alloc.compute_global_market_weights(
        krx_metrics={"expected_return": 0.10, "volatility": 0.20},
        us_metrics={"expected_return": 0.14, "volatility": 0.16},
        regime="caution",
        regime_probs=regime_probs,
        account_state=acct_state,
    )
    assert alloc_res["w_krx"] + alloc_res["w_us"] == pytest.approx(1.0, rel=1e-9)
    assert alloc_res["us_exposure_cap_pct"] == pytest.approx(round(cap_expected, 4), rel=1e-9)


def test_ghost_hwm_contamination_prevented_on_reentry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduce V ($404.36 ghost peak vs $397.74 re-entry) and verify P_peak == P_avg * (1 + peak_pnl/100)."""
    ledger_file = tmp_path / "position_entry_ledger.json"
    exit_file = tmp_path / "daily_exited_tickers.json"
    us_holdings_file = tmp_path / "us_night_holdings.json"
    kp_file = tmp_path / "kis_portfolio.json"

    # 1. Seed stale us_night_holdings.json from Oct 27 trade (entry $399.83, peak $404.36)
    us_holdings_file.write_text(
        json.dumps([
            {
                "ticker": "V",
                "entry_price": 399.83,
                "current_price": 399.83,
                "highest_price": 404.36,
                "peak_price": 404.36,
                "peak_pnl_pct": 1.664,
                "trail_activated": True,
                "quantity": 1,
                "entry_date": "2026-10-27",
            }
        ]),
        encoding="utf-8",
    )

    # 2. Seed contaminated ledger entry where peak_price=404.36 was written when last_avg_price=0.0
    ledger_file.write_text(
        json.dumps({
            "V": {
                "entry_date": "2026-10-28",
                "created_at": "2026-10-28T23:35:00",
                "peak_price": 404.36,
                "lowest_price": 401.545,
                "peak_pnl_pct": 0.956655,
                "peak_date": "2026-10-28",
                "trail_activated": False,
                "scaled_out": False,
                "last_avg_price": 0.0,
                "last_qty": 0.0,
                "last_quantity": 0.0,
                "stream_id": "S3_B",
                "track": "TRACK_2_BUFFETT_QVM",
            }
        }),
        encoding="utf-8",
    )

    # 3. Seed live kis_portfolio.json with Oct 28 re-entry at avg_price = $397.74, current_price = $401.545
    kp_file.write_text(
        json.dumps({
            "account": {"total_equity": 19180000, "usd_cash": 1520.30, "fx_rate": 1436.0},
            "holdings": {
                "V": {
                    "name": "Visa Inc.",
                    "qty": 1,
                    "avg_price": 397.74,
                    "current_price": 401.545,
                    "pnl_pct": 0.956655,
                    "return_pct": 0.956655,
                    "stream_id": "S3_B",
                    "track": "TRACK_2_BUFFETT_QVM",
                }
            },
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr("src.execution.us_night_engine._RESULTS_DIR", tmp_path)
    monkeypatch.setattr("src.execution.us_night_engine._US_HOLDINGS_FILE", us_holdings_file)

    engine = USNightExecutionEngine()
    assert len(engine.holdings) == 1
    v_hold = engine.holdings[0]

    # Expected peak price is 397.74 * (1 + 0.956655 / 100) = 401.545, NOT the ghost 404.36!
    expected_peak = 397.74 * (1.0 + 0.956655 / 100.0)
    assert v_hold["peak_price"] == pytest.approx(expected_peak, rel=1e-6)
    assert v_hold["highest_price"] == pytest.approx(expected_peak, rel=1e-6)
    assert v_hold["peak_pnl_pct"] == pytest.approx(0.956655, rel=1e-5)
    assert v_hold["trail_activated"] is False

    # Check persisted position_entry_ledger.json
    plm = PositionLedgerManager(ledger_path=ledger_file)
    led_v = plm.load_ledger()["V"]
    assert led_v["last_avg_price"] == pytest.approx(397.74, rel=1e-9)
    assert led_v["last_qty"] == pytest.approx(1.0, rel=1e-9)
    assert led_v["peak_price"] == pytest.approx(expected_peak, rel=1e-6)


def test_regime_engine_macro_history_parquet_ssot_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify RegimeEngine._read_macro_history loads from Parquet/signal_cache when overnight_macro is absent."""
    re_eng = RegimeEngine()
    # Point _PROJECT_ROOT to tmp_path where data/raw/overnight_macro does NOT exist
    monkeypatch.setattr("src.intelligence.regime_engine._PROJECT_ROOT", tmp_path)

    kr_dir = tmp_path / "data" / "kr_markets"
    kr_dir.mkdir(parents=True, exist_ok=True)
    idx = pd.date_range("2026-06-01", periods=70, freq="B")
    df_vix = pd.DataFrame({"date": idx, "close": np.linspace(15.0, 21.0, 70)})
    df_vix.to_parquet(kr_dir / "cross_vix.parquet")

    fx_series = pd.Series(np.linspace(1360.0, 1436.0, 70), index=idx)
    dqe = DynamicQuantEngine()
    monkeypatch.setattr(dqe, "_load_fx_close_series", lambda tail_n=120: fx_series)

    fx_hist = re_eng._read_macro_history("usdkrw")
    vix_hist = re_eng._read_macro_history("vix")

    assert len(fx_hist) == 70
    assert fx_hist[-1] == pytest.approx(1436.0, rel=1e-9)
    assert len(vix_hist) == 70
    assert vix_hist[-1] == pytest.approx(21.0, rel=1e-9)
    assert re_eng._read_fx_history() is not None
