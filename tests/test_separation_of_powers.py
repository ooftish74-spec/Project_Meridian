"""Hermetic closed-form tests for the Constitutional Separation-of-Powers Governance Engine."""

from __future__ import annotations

import math
import numpy as np
import pytest

from src.governance.separation_of_powers import (
    DataLibrarian,
    AdversarialFalsifier,
    VaultAndGravekeeper,
    PnLCoroner,
    run_daily_constitutional_audit,
)


def test_data_librarian_point_in_time_and_phase_randomized_surrogate_closed_form():
    """Verify DataLibrarian blocks future look-ahead records and preserves exact mean/variance/power spectrum."""
    records = [
        {"ticker": "005930", "timestamp": "2026-10-29T15:30:00", "available_at": "2026-10-29T16:00:00", "val": 1.0},
        {"ticker": "000660", "timestamp": "2026-10-30T09:00:00", "available_at": "2026-10-30T18:00:00", "val": 2.0},
        {"ticker": "086280", "timestamp": "2026-10-31T09:00:00", "available_at": "2026-10-31T09:00:00", "val": 3.0},
    ]
    pit = DataLibrarian.enforce_point_in_time(records, as_of_timestamp="2026-10-30T12:00:00")
    assert pit["n_clean"] == 1
    assert pit["n_leaked_blocked"] == 2
    assert pit["leakage_detected"] is True
    assert pit["clean_records"][0]["ticker"] == "005930"

    # Fourier Phase Randomization: preserves exact mean, variance, and L2 power spectrum while scrambling time path
    orig = np.array([0.02, -0.05, 0.01, 0.03, -0.02, 0.04, -0.01, -0.03, 0.025, -0.015, 0.005, 0.012], dtype=float)
    surr = DataLibrarian.generate_phase_randomized_surrogate(orig, seed=12345)
    assert len(surr) == len(orig)
    assert float(np.mean(surr)) == pytest.approx(float(np.mean(orig)), rel=1e-9, abs=1e-12)
    assert float(np.var(surr, ddof=1)) == pytest.approx(float(np.var(orig, ddof=1)), rel=1e-9, abs=1e-12)
    assert not np.allclose(surr, orig)


def test_adversarial_falsifier_orthogonal_residual_and_shadow_accountability_closed_form():
    """Verify Falsifier rescues low-standalone-Sharpe diversifier and rejects redundant high-Sharpe clone."""
    t = np.arange(60, dtype=float)
    active = 0.002 + 0.01 * np.sin(t)

    # 1. Redundant clone: r_clone = 1.5 * active + tiny negative alpha (-0.0005)
    # High standalone Sharpe, but negative orthogonal residual alpha -> FALSIFIED
    redundant_cand = -0.0005 + 1.5 * active + 0.0001 * np.cos(t)
    f_red = AdversarialFalsifier.falsify_candidate(
        candidate_returns=redundant_cand,
        active_returns=active,
        estimated_daily_cost=0.0001,
        perturbed_sharpes=[1.8, 1.9, 2.0, 2.1],
    )
    assert f_red["survived"] is False
    assert f_red["verdict"] == "FALSIFIED"
    assert f_red["stressed_orthogonal_ir"] < 0.0

    # 2. Orthogonal diversifier: negatively correlated (-0.8 * active) with positive residual edge
    diversifier_cand = 0.0015 - 0.8 * active + 0.001 * np.cos(t)
    f_div = AdversarialFalsifier.falsify_candidate(
        candidate_returns=diversifier_cand,
        active_returns=active,
        estimated_daily_cost=0.0001,
        perturbed_sharpes=[0.9, 1.1, 1.3, 1.4],
    )
    assert f_div["survived"] is True
    assert f_div["verdict"] == "SURVIVED"
    assert f_div["stressed_orthogonal_ir"] > 0.50

    # 3. Shadow Graveyard accountability (False Rejection Penalty exact closed-form)
    outcomes = [
        {"strategy_id": "strat_bad_1", "realized_shadow_sharpe": -0.50},
        {"strategy_id": "strat_bad_2", "realized_shadow_sharpe": 0.40},
        {"strategy_id": "strat_missed_gem", "realized_shadow_sharpe": 2.50},  # hurdle = 1.0 -> excess = 1.5 -> 1.5^2 = 2.25
    ]
    acc = AdversarialFalsifier.evaluate_falsifier_accountability(outcomes, hurdle_sharpe=1.0)
    assert acc["n_audited"] == 3
    assert acc["true_rejections"] == 2
    assert acc["false_rejections"] == 1
    assert acc["falsifier_accuracy"] == pytest.approx(2.0 / 3.0, rel=1e-9)
    assert acc["false_rejection_penalty"] == pytest.approx(2.25 / 3.0, rel=1e-9)
    assert acc["resurrected_strategies"] == ["strat_missed_gem"]


def test_vault_and_gravekeeper_one_shot_and_effective_trials_closed_form():
    """Verify One-Shot Vault blocks repeat queries, blocks renamed clones, and computes N_eff = 1 + (K-1)(1 - rho_bar)."""
    vg = VaultAndGravekeeper(dsr_gate=0.80, dup_corr_gate=0.90)
    t = np.arange(80, dtype=float)
    r1 = 0.005 + 0.002 * np.sin(t)
    res1 = vg.register_trial_and_open_vault("alpha_v1", r1)
    assert res1["passed"] is True
    assert res1["verdict"] == "VAULT_PASS"
    assert res1["k_trials"] == 1
    assert res1["n_effective_trials"] == pytest.approx(1.0, rel=1e-9)

    # Second query of same strategy_id -> strictly denied
    res_repeat = vg.register_trial_and_open_vault("alpha_v1", r1)
    assert res_repeat["passed"] is False
    assert res_repeat["verdict"] == "VAULT_ACCESS_DENIED_REPEAT_QUERY"

    # Renamed clone with correlation > 0.90 -> blocked by Gravekeeper
    r1_clone = r1 * 1.02 + 1e-5 * np.cos(t)
    res_clone = vg.register_trial_and_open_vault("alpha_v1_renamed", r1_clone)
    assert res_clone["passed"] is False
    assert res_clone["verdict"] == "REJECTED_CLONE_STRATEGY"
    assert res_clone["max_correlation_with_graveyard"] >= 0.90

    # Orthogonal strategy -> passes and updates N_eff
    r2_ortho = 0.006 + 0.002 * np.cos(t * 3.0)
    res_ortho = vg.register_trial_and_open_vault("alpha_v2_ortho", r2_ortho)
    assert res_ortho["passed"] is True
    assert res_ortho["k_trials"] == 3
    assert res_ortho["n_effective_trials"] > 1.5


def test_pnl_coroner_unexplained_windfall_gain_and_tail_loss_closed_form():
    """Verify PnLCoroner flags both unexplained windfall luck gains and structural tail losses."""
    # 1. Unexplained windfall gain: expected +0.5%, market +0.2% * beta 1.0 = +0.2% -> explained = +0.7%
    # Realized = +8.7% -> luck_residual = +8.0%, vol = 2.0% -> z = +4.0 >= 2.5, luck_share = 8.0 / 8.7 = 0.9195 >= 0.60
    autopsy_gain = PnLCoroner.autopsy_realized_return(
        realized_return=0.087,
        expected_model_return=0.005,
        market_return=0.002,
        asset_beta=1.0,
        residual_volatility=0.02,
    )
    assert autopsy_gain["explained_return"] == pytest.approx(0.007, rel=1e-9)
    assert autopsy_gain["luck_residual"] == pytest.approx(0.080, rel=1e-9)
    assert autopsy_gain["residual_z_score"] == pytest.approx(4.0, rel=1e-9)
    assert autopsy_gain["luck_share_of_return"] == pytest.approx(0.080 / 0.087, rel=1e-9)
    assert autopsy_gain["verdict"] == "UNEXPLAINED_WINDFALL_GAIN"
    assert autopsy_gain["quarantine_recommended"] is True

    # 2. Structural tail loss: z = -3.5 <= -2.5 -> UNEXPLAINED_TAIL_LOSS
    autopsy_loss = PnLCoroner.autopsy_realized_return(
        realized_return=-0.063,
        expected_model_return=0.005,
        market_return=0.002,
        asset_beta=1.0,
        residual_volatility=0.02,
    )
    assert autopsy_loss["luck_residual"] == pytest.approx(-0.070, rel=1e-9)
    assert autopsy_loss["residual_z_score"] == pytest.approx(-3.5, rel=1e-9)
    assert autopsy_loss["verdict"] == "UNEXPLAINED_TAIL_LOSS"
    assert autopsy_loss["quarantine_recommended"] is True


def test_run_daily_constitutional_audit_end_to_end(tmp_path):
    """Verify evening pipeline daily constitutional audit writes constitutional_governance_audit.json."""
    import json

    me_file = tmp_path / "measurement_engine.json"
    ov_file = tmp_path / "dynamic_overrides.json"
    me_file.write_text(json.dumps({
        "official": {
            "portfolio_beta": 0.80,
            "annualized_vol_pct": 15.874507866387544,  # 1.0% daily vol
            "per_stream": {
                "S2": {"sharpe": 1.50, "daily_returns": [0.01, -0.005, 0.008, 0.004, 0.012, -0.002]},
                "S3": {"sharpe": 2.10, "daily_returns": [-0.004, 0.009, -0.002, 0.006, -0.005, 0.007]},
                "S5": {"sharpe": -8.258, "daily_returns": [-0.01, -0.02, -0.015, -0.01, -0.012, -0.008]},
            },
        },
        "views": {
            "portfolio": {
                "daily_return_pct": 4.80,
                "benchmark_return_pct": 1.00,
            }
        },
        "daily_series": {
            "portfolio_returns": [0.005, 0.002, 0.004, 0.003, 0.006, 0.001],
        },
    }), encoding="utf-8")
    ov_file.write_text(json.dumps({
        "gravekeeper_retired_streams": {
            "S5": {"status": "RETIRED_TO_SHADOW", "sharpe": -8.258},
        }
    }), encoding="utf-8")

    res = run_daily_constitutional_audit(tmp_path)
    out_path = tmp_path / "constitutional_governance_audit.json"
    assert out_path.exists()
    assert res["gravekeeper"]["k_streams_tracked"] == 3
    assert res["gravekeeper"]["retired_streams_count"] == 1
    assert "S2" in res["falsifier"]["orthogonal_stream_audits"]
    assert res["falsifier"]["shadow_accountability"]["true_rejections"] == 1
    assert res["coroner"]["verdict"] == "UNEXPLAINED_WINDFALL_GAIN"

