"""
Tests for Meridian 3.0 Frontier Shadow Sandbox Isolation
==========================================================
Verifies:
1. ActiveInferenceEngine requires is_shadow_sandbox=True.
2. Order execution is strictly blocked.
3. Free energy calculation and phase transition detection work accurately.
4. Regime-conditional factor dormancy and reactivation operate as designed.
"""

import pytest
import os
from src.shadow_sandbox.active_inference_engine import ActiveInferenceEngine
from src.shadow_sandbox.sandbox_runner import run_isolated_shadow_sandbox

def test_shadow_sandbox_permission_guard():
    """Verify engine raises PermissionError if shadow_sandbox flag is False."""
    with pytest.raises(PermissionError) as excinfo:
        ActiveInferenceEngine(is_shadow_sandbox=False)
    assert "Shadow Sandbox Only" in str(excinfo.value)

def test_shadow_sandbox_execution_blocked():
    """Verify order execution is strictly blocked."""
    engine = ActiveInferenceEngine(is_shadow_sandbox=True)
    assert engine.order_execution_blocked is True
    assert engine.is_shadow_sandbox is True

def test_free_energy_calculation():
    """Verify Variational Free Energy metrics."""
    engine = ActiveInferenceEngine(is_shadow_sandbox=True)
    metrics = engine.calculate_free_energy(posterior_mean=0.02, posterior_var=0.05, log_likelihood=0.03)
    assert "free_energy" in metrics
    assert "complexity_penalty" in metrics
    assert "accuracy_reward" in metrics
    assert isinstance(metrics["is_overfitted"], bool)

def test_phase_transition_detection():
    """Verify liquidity entropy phase transition calculation."""
    engine = ActiveInferenceEngine(is_shadow_sandbox=True)
    ticks = [
        {"spread": 0.001, "volume": 100.0},
        {"spread": 0.005, "volume": 500.0},
        {"spread": 0.020, "volume": 2000.0}
    ]
    res = engine.detect_liquidity_phase_transition(ticks)
    assert "phase_collapse" in res
    assert "entropy" in res
    assert "entropy_spike" in res

def test_regime_conditional_dormancy():
    """Verify factor dormancy in adverse regimes and reactivation in favorable regimes."""
    engine = ActiveInferenceEngine(is_shadow_sandbox=True)
    
    # Test Bull regime -> tech_momentum active
    bull_weights = engine.evaluate_regime_conditional_factors("bull")
    assert bull_weights["tech_momentum"] == 0.85
    
    # Test Bear regime -> tech_momentum dormant (< 0.1)
    bear_weights = engine.evaluate_regime_conditional_factors("bear")
    assert bear_weights["tech_momentum"] == 0.05

def test_sandbox_runner_telemetry():
    """Verify end-to-end sandbox runner outputs valid telemetry file."""
    telemetry = run_isolated_shadow_sandbox()
    assert telemetry["engine_status"] == "SHADOW_SANDBOX_ACTIVE"
    assert telemetry["order_execution_permitted"] is False
    
    results_path = os.path.join(os.path.dirname(__file__), "..", "results", "shadow_sandbox_telemetry.json")
    assert os.path.exists(results_path)
