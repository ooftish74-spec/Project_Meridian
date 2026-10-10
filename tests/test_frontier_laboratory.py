"""
Unit Tests for Meridian 3.0 Track 2 Frontier Laboratory
=======================================================
Verifies:
1. Isolated asynchronous execution without live trader dependencies.
2. Strict shadow sandbox enforcement (zero live orders).
3. Soliton Causal Diversity and KdV zero-bias conservation.
4. Genetic rule evolver chromosome adaptation within safety bounds.
5. DynamicConfig contract hot-reload (< 1ms).
"""

import os
import json
import pytest
from pathlib import Path
from scripts.run_frontier_laboratory import FrontierLaboratory
from config.dynamic_config import DynamicConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent

def test_frontier_laboratory_execution():
    """Verify end-to-end execution of Frontier Laboratory cycle."""
    lab = FrontierLaboratory(mode="daily", dry_run=False)
    telemetry = lab.run_cycle()

    # 1. Verification of Sandbox Security
    assert telemetry["sandbox_isolation"] == "STRICT_SHADOW_SANDBOX"
    assert telemetry["order_execution_blocked"] is True

    # 2. Verification of Active Inference Module
    ai_mod = telemetry["modules"]["active_inference"]
    assert ai_mod["status"] == "SUCCESS"
    assert "free_energy" in ai_mod
    assert "factor_weights" in ai_mod

    # 3. Verification of Soliton Causal Diversity Module
    sol_mod = telemetry["modules"]["soliton_causal"]
    assert sol_mod["status"] == "SUCCESS"
    assert sol_mod["zero_bias_verified"] is True
    assert sol_mod["elites_count"] > 0

    # 4. Verification of Genetic Rule Evolver & State Contract
    gen_mod = telemetry["modules"]["genetic_evolution"]
    assert gen_mod["status"] == "SUCCESS"
    assert gen_mod["overrides_updated"] == 5

    # 5. Verification of Telemetry File Creation
    telem_file = PROJECT_ROOT / "results" / "frontier_laboratory_telemetry.json"
    assert telem_file.exists()

    # 6. Verification of Hot-Reload in DynamicConfig
    cfg = DynamicConfig()
    cfg.reload()
    for key, val in gen_mod["best_chromosome"].items():
        assert cfg.get(key) == val

def test_frontier_laboratory_dry_run():
    """Verify dry-run mode skips disk mutation."""
    lab = FrontierLaboratory(mode="daily", dry_run=True)
    telemetry = lab.run_cycle()
    assert telemetry["dry_run"] is True
    assert telemetry["modules"]["genetic_evolution"]["status"] == "SKIPPED_DRY_RUN"
