"""
Shadow Sandbox Independent Runner
==================================
Runs the Active Inference Engine in complete isolation from the Production Live Trader.
Order execution is ZERO-PERMITTED.
Outputs telemetry to results/shadow_sandbox_telemetry.json.
"""

import os
import json
import logging
from datetime import datetime
from src.shadow_sandbox.active_inference_engine import ActiveInferenceEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ShadowSandbox.Runner")

def run_isolated_shadow_sandbox():
    logger.info("==========================================================")
    logger.info("      MERIDIAN 3.0 FRONTIER SHADOW SANDBOX ENGINE         ")
    logger.info("==========================================================")
    logger.info("📌 Isolation Mode         : STRICT_SHADOW_SANDBOX")
    logger.info("📌 KIS Live Execution     : ZERO_PERMITTED (ORDER ROUTING BLOCKED)")
    logger.info("----------------------------------------------------------")
    
    # Initialize Engine
    engine = ActiveInferenceEngine(is_shadow_sandbox=True)
    
    # Simulated input telemetry
    current_regime = "caution"
    market_ticks = [
        {"spread": 0.0012, "volume": 1500.0},
        {"spread": 0.0015, "volume": 1200.0},
        {"spread": 0.0028, "volume": 3500.0},
        {"spread": 0.0045, "volume": 4200.0}
    ]
    live_returns = [0.005, 0.012, -0.003, 0.008, 0.002]
    
    # Run Shadow Cycle
    telemetry = engine.run_shadow_cycle(current_regime, market_ticks, live_returns)
    telemetry["timestamp"] = datetime.now().isoformat()
    
    # Save Shadow Telemetry to results/shadow_sandbox_telemetry.json
    results_dir = os.path.join(os.path.dirname(__file__), "..", "..", "results")
    os.makedirs(results_dir, exist_ok=True)
    output_path = os.path.join(results_dir, "shadow_sandbox_telemetry.json")
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(telemetry, f, indent=2, ensure_ascii=False)
        
    # Save Dated History Ledger to results/shadow_history/YYYY-MM-DD.json
    history_dir = os.path.join(results_dir, "shadow_history")
    os.makedirs(history_dir, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    history_path = os.path.join(history_dir, f"{today_str}.json")
    
    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(telemetry, f, indent=2, ensure_ascii=False)
        
    logger.info(f"✅ Shadow Sandbox Telemetry written to: {output_path}")
    logger.info(f"✅ Shadow Daily Ledger entry written to: {history_path}")
    logger.info("==========================================================")
    return telemetry

if __name__ == "__main__":
    run_isolated_shadow_sandbox()
