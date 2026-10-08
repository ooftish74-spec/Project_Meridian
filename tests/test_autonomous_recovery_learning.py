"""
Unit Tests for Autonomous Recovery & Self-Learning Enhancements (Project Meridian 3.0)
"""

import pytest
import numpy as np
from src.learning.rl_adaptive_position_sizer import RLAdaptivePositionSizer
from src.execution.autonomous_state_repair_engine import AutonomousStateRepairEngine
from src.intelligence.ood_drift_self_healer import OODDriftSelfHealer


def test_rl_adaptive_position_sizer():
    sizer = RLAdaptivePositionSizer(learning_rate=0.1)
    weights = {"S0": 0.2, "S1": 0.3, "S2": 0.5}
    perf = {
        "S0": {"delta_nav": 50000.0, "sharpe": 2.0},
        "S1": {"delta_nav": -10000.0, "sharpe": 0.5},
        "S2": {"delta_nav": 100000.0, "sharpe": 2.5},
    }
    new_w = sizer.update_stream_weights(weights, perf, ewma_volatility=0.015)
    assert len(new_w) == 3
    assert abs(sum(new_w.values()) - 1.0) < 1e-3
    assert new_w["S2"] > new_w["S1"]


def test_autonomous_state_repair_engine_clean():
    engine = AutonomousStateRepairEngine()
    kis_p = {"positions": {"069500": {"qty": 50}, "NVDA": {"qty": 10}}}
    shadow_p = {"positions": {"069500": {"qty": 50}, "NVDA": {"qty": 10}}}
    res = engine.audit_and_repair(kis_p, shadow_p)
    assert res["audit_passed"] is True
    assert res["mismatch_count"] == 0
    assert len(res["repair_actions"]) == 0


def test_autonomous_state_repair_engine_mismatch():
    engine = AutonomousStateRepairEngine()
    kis_p = {"positions": {"069500": {"qty": 52}, "NVDA": {"qty": 8}}}
    shadow_p = {"positions": {"069500": {"qty": 50}, "NVDA": {"qty": 10}}}
    res = engine.audit_and_repair(kis_p, shadow_p)
    assert res["audit_passed"] is False
    assert res["mismatch_count"] == 2
    assert len(res["repair_actions"]) == 2
    tickers_repaired = {a["ticker"]: a for a in res["repair_actions"]}
    assert "069500" in tickers_repaired
    assert "NVDA" in tickers_repaired
    assert tickers_repaired["069500"]["adjustment_qty"] == 2
    assert tickers_repaired["NVDA"]["adjustment_qty"] == 2


def test_ood_drift_self_healer_normal():
    healer = OODDriftSelfHealer()
    current = np.array([0.01, 0.02, -0.005])
    historic = np.random.normal(0.01, 0.02, (100, 3))
    res = healer.evaluate_ood_and_heal(current, historic, ml_stream_weight=1.0)
    assert "mahalanobis_distance" in res
    assert "ood_detected" in res
    assert res["healed_ml_weight"] <= 1.0


def test_ood_drift_self_healer_drift_trigger():
    healer = OODDriftSelfHealer()
    # Extreme OOD feature vector
    current = np.array([10.0, -15.0, 20.0])
    historic = np.random.normal(0.0, 1.0, (100, 3))
    res = healer.evaluate_ood_and_heal(current, historic, ml_stream_weight=1.0)
    assert res["ood_detected"] is True
    assert res["healed_ml_weight"] < 0.5
    assert res["trigger_background_retrain"] is True
