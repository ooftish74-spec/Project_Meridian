"""Hermetic closed-form tests for the 100% Full Constitutional Separation-of-Powers Governance Engine."""

from __future__ import annotations

import json
import math
import numpy as np
import pytest

from src.governance.separation_of_powers import (
    DataLibrarian,
    AirGappedExplorerGate,
    AdversarialFalsifier,
    VaultAndGravekeeper,
    CapacityArchitect,
    ExecutionForensics,
    RegimeAndDecaySentinel,
    StagedPromotionEngine,
    PnLCoroner,
    CIODocketBuilder,
    CIODocketEmailService,
    run_daily_constitutional_audit,
)


def test_data_librarian_point_in_time_surrogate_and_survivorship_bias_closed_form():
    """Verify DataLibrarian blocks future records, preserves power spectrum, and quantifies survivorship bias."""
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

    orig = np.array([0.02, -0.05, 0.01, 0.03, -0.02, 0.04, -0.01, -0.03, 0.025, -0.015, 0.005, 0.012], dtype=float)
    surr = DataLibrarian.generate_phase_randomized_surrogate(orig, seed=12345)
    assert len(surr) == len(orig)
    assert float(np.mean(surr)) == pytest.approx(float(np.mean(orig)), rel=1e-9, abs=1e-12)
    assert float(np.var(surr, ddof=1)) == pytest.approx(float(np.var(orig, ddof=1)), rel=1e-9, abs=1e-12)
    assert not np.allclose(surr, orig)

    # Survivorship bias check: 10 PIT stocks, 2 delisted omitted from active backtest universe
    pit_u = [f"TK_{i}" for i in range(10)]
    delisted = ["TK_8", "TK_9"]
    active_u = [f"TK_{i}" for i in range(8)]
    surv = DataLibrarian.audit_survivorship_bias(pit_u, active_u, delisted, assumed_delist_return=-0.50)
    assert surv["n_omitted_delisted"] == 2
    assert surv["omission_rate"] == pytest.approx(0.20, rel=1e-9)
    assert surv["estimated_return_inflation"] == pytest.approx(0.10, rel=1e-9)
    assert surv["survivorship_bias_detected"] is True


def test_airgapped_explorer_gate_anonymizes_and_locks_in_shadow():
    """Verify AirGappedExplorerGate physically truncates OOS vault and locks submissions in SHADOW(0x)."""
    panel = {
        "005930": [0.01 * ((-1) ** i) for i in range(20)],
        "NVDA": [0.02 * ((-1) ** i) for i in range(20)],
    }
    ds = AirGappedExplorerGate.prepare_airgapped_explorer_dataset(panel, train_ratio=0.70, seed=99)
    assert ds["oos_vault_physically_truncated"] is True
    assert "005930" not in ds["anonymized_surrogate_data"]
    assert "NVDA" not in ds["anonymized_surrogate_data"]
    assert len(ds["anonymized_surrogate_data"]["ANON_ASSET_0001"]) == 14
    assert ds["vault_reserved_bars_per_asset"]["ANON_ASSET_0001"] == 6

    sub = AirGappedExplorerGate.submit_hypothesis_to_shadow("HYP_01", "Orderbook imbalance mean reversion")
    assert sub["stage"] == "SHADOW"
    assert sub["live_capital_multiplier"] == 0.0
    assert sub["pre_declared_mortality"]["max_drawdown_pct"] == -12.0


def test_adversarial_falsifier_orthogonal_residual_subperiod_and_accountability_closed_form():
    """Verify Falsifier rescues diversifier, rejects clone, checks sub-period stability, and scores Shadow Graveyard."""
    t = np.arange(60, dtype=float)
    active = 0.002 + 0.01 * np.sin(t)

    redundant_cand = -0.0005 + 1.5 * active + 0.0001 * np.cos(t)
    f_red = AdversarialFalsifier.falsify_candidate(
        candidate_returns=redundant_cand,
        active_returns=active,
        estimated_daily_cost=0.0001,
        perturbed_sharpes=[1.8, 1.9, 2.0, 2.1],
    )
    assert f_red["survived"] is False
    assert f_red["verdict"] == "FALSIFIED"

    diversifier_cand = 0.0015 - 0.8 * active + 0.001 * np.cos(t)
    f_div = AdversarialFalsifier.falsify_candidate(
        candidate_returns=diversifier_cand,
        active_returns=active,
        estimated_daily_cost=0.0001,
        perturbed_sharpes=[0.9, 1.1, 1.3, 1.4],
    )
    assert f_div["survived"] is True
    assert f_div["verdict"] == "SURVIVED"
    assert f_div["subperiod_positive_ratio"] == pytest.approx(1.0, rel=1e-9)

    outcomes = [
        {"strategy_id": "strat_bad_1", "realized_shadow_sharpe": -0.50},
        {"strategy_id": "strat_bad_2", "realized_shadow_sharpe": 0.40},
        {"strategy_id": "strat_missed_gem", "realized_shadow_sharpe": 2.50},
    ]
    acc = AdversarialFalsifier.evaluate_falsifier_accountability(outcomes, hurdle_sharpe=1.0)
    assert acc["falsifier_accuracy"] == pytest.approx(2.0 / 3.0, rel=1e-9)
    assert acc["false_rejection_penalty"] == pytest.approx(2.25 / 3.0, rel=1e-9)


def test_vault_and_gravekeeper_persistent_disk_ledger_and_effective_trials(tmp_path):
    """Verify One-Shot Vault persists across process restarts via vault_graveyard_ledger.json."""
    ledger_file = tmp_path / "vault_graveyard_ledger.json"
    vg1 = VaultAndGravekeeper(dsr_gate=0.80, dup_corr_gate=0.90, ledger_path=ledger_file)
    t = np.arange(80, dtype=float)
    r1 = 0.005 + 0.002 * np.sin(t)
    res1 = vg1.register_trial_and_open_vault("alpha_v1", r1)
    assert res1["passed"] is True
    assert ledger_file.exists()

    # Simulate process restart: instantiate a brand-new VaultAndGravekeeper pointing to same ledger_file
    vg2 = VaultAndGravekeeper(dsr_gate=0.80, dup_corr_gate=0.90, ledger_path=ledger_file)
    res_repeat = vg2.register_trial_and_open_vault("alpha_v1", r1)
    assert res_repeat["passed"] is False
    assert res_repeat["verdict"] == "VAULT_ACCESS_DENIED_REPEAT_QUERY"

    r1_clone = r1 * 1.02 + 1e-5 * np.cos(t)
    res_clone = vg2.register_trial_and_open_vault("alpha_v1_renamed", r1_clone)
    assert res_clone["passed"] is False
    assert res_clone["verdict"] == "REJECTED_CLONE_STRATEGY"


def test_operations_capacity_execution_forensics_decay_and_staged_promotion_closed_form():
    """Verify CapacityArchitect, ExecutionForensics, RegimeAndDecaySentinel, and StagedPromotionEngine."""
    # 1. CapacityArchitect: ADV = 1,000,000,000 KRW, vol = 0.02, max_part = 1% -> 10,000,000 KRW
    # Impact cap: 1e9 * (0.0020 / (0.10 * 0.02))^2 = 1e9 * 1.0 = 1e9 -> min(1e7, 1e9) = 10,000,000 KRW
    cap = CapacityArchitect.compute_stream_capacity(
        adv_krw_per_asset=[1_000_000_000.0],
        daily_vol_per_asset=[0.02],
        current_allocated_krw=20_000_000.0,
        max_participation_rate=0.01,
        max_impact_bps=20.0,
    )
    assert cap["max_capacity_krw"] == pytest.approx(10_000_000.0, rel=1e-9)
    assert cap["capacity_utilization"] == pytest.approx(2.0, rel=1e-9)
    assert cap["capacity_saturated"] is True
    assert cap["recommended_scale_cap"] == pytest.approx(0.50, rel=1e-9)

    # 2. ExecutionForensics: BUY at fill 100,600 vs decision 100,000 (+60 bps slippage) + 15 bps fee = 75 bps >= 50 bps
    ef = ExecutionForensics.audit_stream_execution([
        {"side": "BUY", "decision_price": 100_000.0, "fill_price": 100_600.0, "fee_bps": 15.0}
    ])
    assert ef["mean_shortfall_bps"] == pytest.approx(75.0, rel=1e-9)
    assert ef["retire_by_divergence"] is True
    assert ef["verdict"] == "RETIRED_BY_EXECUTION_DIVERGENCE"

    # 3. RegimeAndDecaySentinel: Linearly decaying IC [0.30, 0.20, 0.10, 0.00, -0.10] -> slope = -0.10 <= -0.05
    sent = RegimeAndDecaySentinel.detect_alpha_decay_and_crowding(
        rolling_perf_series=[0.30, 0.20, 0.10, 0.00, -0.10],
        volume_surge_z=2.0,
        max_pairwise_correlation=0.85,
    )
    assert sent["alpha_decay_slope"] == pytest.approx(-0.10, rel=1e-9)
    assert sent["alpha_decay_detected"] is True
    assert sent["crowding_detected"] is True
    assert sent["sentinel_status"] == "DECAY_OR_CROWDING_ALERT"

    # 4. StagedPromotionEngine: SHADOW -> PILOT -> FULL_ACTIVE -> RETIRED_TO_SHADOW
    s_to_p = StagedPromotionEngine.evaluate_stage_transition("S_NEW", "SHADOW", n_days=6, sharpe=1.35, mdd_pct=-2.0, is_incumbent=False)
    assert s_to_p["next_stage"] == "PILOT"
    assert s_to_p["recommended_multiplier_cap"] == pytest.approx(0.50, rel=1e-9)

    p_to_f = StagedPromotionEngine.evaluate_stage_transition("S_NEW", "PILOT", n_days=16, sharpe=1.45, mdd_pct=-3.5, is_incumbent=False)
    assert p_to_f["next_stage"] == "FULL_ACTIVE"
    assert p_to_f["recommended_multiplier_cap"] == pytest.approx(1.00, rel=1e-9)

    ret = StagedPromotionEngine.evaluate_stage_transition("S5", "FULL_ACTIVE", n_days=9, sharpe=-8.258, mdd_pct=-5.0, is_incumbent=True)
    assert ret["next_stage"] == "RETIRED_TO_SHADOW"
    assert ret["recommended_multiplier_cap"] == pytest.approx(0.50, rel=1e-9)


def test_pnl_coroner_and_cio_docket_builder_end_to_end(tmp_path):
    """Verify position-level Coroner autopsy, Friday regular CIO docket, and daily constitutional audit."""
    me_file = tmp_path / "measurement_engine.json"
    ov_file = tmp_path / "dynamic_overrides.json"
    wf_file = tmp_path / "walk_forward_daily.json"
    kp_file = tmp_path / "kis_portfolio.json"

    # Verify bounded luck_share_of_return in [0, 1] even when realized_return == 0.0
    zero_ret_autopsy = PnLCoroner.autopsy_realized_return(
        realized_return=0.0,
        expected_model_return=0.0,
        market_return=-0.01,
        asset_beta=0.7627685,
        residual_volatility=0.01,
    )
    assert zero_ret_autopsy["luck_share_of_return"] == pytest.approx(0.50, rel=1e-9)
    assert 0.0 <= zero_ret_autopsy["luck_share_of_return"] <= 1.0

    me_file.write_text(json.dumps({
        "date": "2026-10-30",  # Friday!
        "official": {
            "portfolio_beta": 0.80,
            "annualized_vol_pct": 15.874507866387544,
            "per_stream": {
                "S2": {"sharpe": 1.50, "n_days": 6, "daily_returns": [0.01, -0.005, 0.008, 0.004, 0.012, -0.002]},
                "S3": {"sharpe": 2.10, "n_days": 6, "daily_returns": [-0.004, 0.009, -0.002, 0.006, -0.005, 0.007]},
                "S5": {"sharpe": -8.258, "n_days": 9, "daily_returns": [-0.01, -0.02, -0.015, -0.01, -0.012, -0.008]},
            },
        },
        "views": {
            "portfolio": {
                "daily_return_pct": 4.80,
                "benchmark_return_pct": 1.00,
            }
        },
        "daily_series": [
            {"date": "2026-10-23", "daily_return_pct": 0.5},
            {"date": "2026-10-26", "daily_return_pct": 0.2},
            {"date": "2026-10-27", "daily_return_pct": 0.4},
            {"date": "2026-10-28", "daily_return_pct": 0.3},
            {"date": "2026-10-29", "daily_return_pct": 0.6},
            {"date": "2026-10-30", "daily_return_pct": 0.1},
        ],
    }), encoding="utf-8")
    ov_file.write_text(json.dumps({
        "gravekeeper_retired_streams": {
            "S5": {"status": "RETIRED_TO_SHADOW", "sharpe": -8.258},
        }
    }), encoding="utf-8")
    wf_file.write_text(json.dumps([
        {
            "date": "2026-10-30",
            "oos_acc": 0.5355,
            "oos_excess_acc": 0.5902,
            "oos_ic": 0.2621,
            "prob_calibration_bias": 0.2088,
        }
    ]), encoding="utf-8")
    kp_file.write_text(json.dumps({
        "holdings": {
            "000270": {"name": "기아", "return_pct": -9.47, "expected_return_pct": 0.50, "beta": 1.0, "daily_vol": 0.02},
            "086280": {"name": "현대글로비스", "return_pct": 12.50, "expected_return_pct": 0.50, "beta": 1.0, "daily_vol": 0.02},
            "034230": {"name": "파라다이스", "return_pct": -0.71, "expected_return_pct": 0.0, "beta": 1.0, "daily_vol": 0.02},
        }
    }), encoding="utf-8")

    res = run_daily_constitutional_audit(tmp_path)
    assert (tmp_path / "constitutional_governance_audit.json").exists()
    assert (tmp_path / "vault_graveyard_ledger.json").exists()
    assert res["coroner"]["verdict"] == "UNEXPLAINED_WINDFALL_GAIN"
    assert res["coroner"]["positions_autopsy"]["n_positions_autopsied"] == 3
    assert res["coroner"]["positions_autopsy"]["n_anomalies"] == 2
    assert res["coroner"]["positions_autopsy"]["position_anomalies"][0]["ticker"] == "000270"
    assert "S2" in res["falsifier"]["orthogonal_stream_audits"]
    assert "S3" in res["falsifier"]["orthogonal_stream_audits"]
    assert res["cio_approval_docket"]["is_weekly_docket_day"] is True
    assert res["cio_approval_docket"]["is_emergency_escalation"] is True
    assert res["cio_approval_docket"]["n_pending_dockets"] == 2
    assert "<!-- MERIDIAN_OUTBOUND_DOCKET -->" in res["cio_approval_docket"]["email_markdown"]


def test_cio_docket_email_markdown_and_reply_execution(tmp_path):
    """Verify outbound email text is never self-triggered as a reply, and genuine replies ('승인'/'보류'/'기각') execute cleanly."""
    docket = CIODocketBuilder.build_docket(
        as_of_date="2026-10-30",
        wf_report={
            "oos_acc": 0.3169,
            "oos_excess_acc": 0.5902,
            "oos_ic": 0.2621,
            "base_rate_up": 0.3169,
            "prob_calibration_bias": 0.2088,
            "top_bottom_spread_pct": 1.379,
            "n_validated": 183,
        },
        coroner_report={"quarantine_recommended": False, "verdict": "EXPLAINED_NORMAL"},
        execution_report={"retire_by_divergence": False, "verdict": "EXECUTION_WITHIN_BOUNDS"},
        gravekeeper_report={"n_effective_trials": 3.0},
    )
    email_md = CIODocketBuilder.format_docket_email_markdown(docket)
    assert "한눈에 이해하는 핵심 요약" in email_md
    assert "시험이 아주 어렵게 출제되어" in email_md
    assert "Renaissance Technologies (Medallion) & Two Sigma" in email_md
    assert "Citadel GQS & D.E. Shaw" in email_md
    assert "AQR Capital & Millennium Management" in email_md
    assert "위원회 공식 제안" in email_md

    # 1. Outbound email body must NEVER be parsed as a reply
    assert CIODocketEmailService.parse_cio_reply_text(email_md, default_docket_id="DOCKET-2026-10-30-CAL01") is None

    # 2. Genuine reply with quoted original email
    reply_approve = (
        "승인\n\n"
        "On Fri, Oct 30, 2026 at 9:40 PM <ooftish74@gmail.com> wrote:\n"
        "> ⚖️ [Meridian CIO 결재 상정안] (2026-10-30)\n"
        "> 기각 또는 보류\n"
    )
    parsed_app = CIODocketEmailService.parse_cio_reply_text(reply_approve, default_docket_id="DOCKET-2026-10-30-CAL01")
    assert parsed_app is not None
    assert parsed_app["decision"] == "APPROVE_PILOT"
    assert parsed_app["docket_id"] == "DOCKET-2026-10-30-CAL01"

    parsed_def = CIODocketEmailService.parse_cio_reply_text("2번 보류합니다\n> 원본", default_docket_id="DOCKET-2026-10-30-CAL01")
    assert parsed_def is not None and parsed_def["decision"] == "DEFER_SHADOW"

    parsed_rej = CIODocketEmailService.parse_cio_reply_text("기각 (DOCKET-2026-10-30-CAL01)", default_docket_id="")
    assert parsed_rej is not None and parsed_rej["decision"] == "REJECT_TO_GRAVEYARD"
    assert parsed_rej["docket_id"] == "DOCKET-2026-10-30-CAL01"

    # 3. Apply decision and verify SSOT files
    (tmp_path / "cio_approval_docket.json").write_text(json.dumps(docket), encoding="utf-8")
    applied = CIODocketEmailService.apply_cio_decision(
        docket_id="DOCKET-2026-10-30-CAL01",
        decision="APPROVE_PILOT",
        raw_reply="승인",
        results_dir=tmp_path,
        send_confirmation_email=False,
    )
    assert applied["applied"] is True
    ov = json.loads((tmp_path / "dynamic_overrides.json").read_text(encoding="utf-8"))
    assert ov["cio_approved_dockets"]["DOCKET-2026-10-30-CAL01"]["stage"] == "PILOT"
    assert ov["parameters"]["ml.enable_prior_shift_calibration"] is True
    assert ov["parameters"]["ml.prior_shift_pilot_scale"] == pytest.approx(0.50, rel=1e-9)

    # 4. Verify Prior-Shift Calibration applies exact 0.5x shift (0.65 - 0.50 * 0.2088 = 0.5456)
    (tmp_path / "walk_forward_daily.json").write_text(
        json.dumps([{"date": "2026-10-30", "prob_calibration_bias": 0.2088}]),
        encoding="utf-8",
    )
    scored = [{"ticker": "005930", "up_prob": 0.65}]
    scored_cal = CIODocketEmailService.apply_prior_shift_calibration_to_scored(scored, results_dir=tmp_path)
    assert scored_cal[0]["up_prob_uncalibrated"] == pytest.approx(0.65, rel=1e-9)
    assert scored_cal[0]["up_prob"] == pytest.approx(0.5456, rel=1e-9)
    assert scored_cal[0]["prior_shift_applied"] == pytest.approx(-0.1044, rel=1e-9)

    from src.interface.email_notifier import MeridianEmail
    html_fallback = MeridianEmail._simple_markdown_to_html("# 제목\n> **승인** `코드`")
    assert "<h1>제목</h1>" in html_fallback
    assert "<blockquote><strong>승인</strong> <code>코드</code></blockquote>" in html_fallback


