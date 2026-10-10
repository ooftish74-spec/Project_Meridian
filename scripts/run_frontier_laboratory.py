#!/usr/bin/env python3
"""
Meridian 3.0 — Track 2 Frontier Laboratory Runner
=================================================
Asynchronous scientific research, genetic self-evolution, and shadow sandbox daemon.
Runs completely decoupled from Track 1 (Lean Production Live Execution).

Features:
  1. Active Inference Engine (Variational Free Energy, Liquidity Phase Transitions, Factor Dormancy).
  2. Soliton Causal Diversity Engine (KdV Hamiltonian Wave Resonance, do(X) Causal Invariance, MAP-Elites).
  3. Genetic Rule Evolver (Multi-generation chromosome adaptation with strict safety bounds).
  4. State Contract: Publishes validated multipliers to results/dynamic_overrides.json (< 1ms read in Track 1).
  5. Zero Execution Guarantee: is_shadow_sandbox=True, order_execution_blocked=True.

Usage:
  python3 scripts/run_frontier_laboratory.py [--mode krx|us|daily] [--dry-run]
"""

import os
import sys
import json
import logging
import argparse
import numpy as np
from pathlib import Path
from datetime import datetime

# Set Project Root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.dynamic_config import DynamicConfig
from src.utils.file_ops import atomic_write_json
from src.shadow_sandbox.active_inference_engine import ActiveInferenceEngine
from src.evolution.soliton_causal_diversity_engine import SolitonCausalDiversityEngine
from src.learning.genetic_rule_evolver import GeneticRuleEvolver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("Meridian.FrontierLaboratory")


class FrontierLaboratory:
    """Track 2 Asynchronous Frontier Laboratory Coordinator."""

    def __init__(self, mode: str = "daily", dry_run: bool = False):
        self.mode = mode
        self.dry_run = dry_run
        self.results_dir = PROJECT_ROOT / "results"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = DynamicConfig()

    def _load_market_context(self) -> dict:
        """Load latest market regime and returns context."""
        regime = "caution"
        vix = 18.5
        market_returns = [0.002, -0.005, 0.008, 0.001, -0.003, 0.006, 0.004]

        # 1. US Market Regime
        usr_path = self.results_dir / "us_market_regime.json"
        if usr_path.exists():
            try:
                data = json.loads(usr_path.read_text(encoding="utf-8"))
                regime = data.get("regime", regime)
            except Exception as e:
                logger.debug(f"US market regime read error: {e}")

        # 2. Signal Cache / VIX
        sig_path = self.results_dir / "signal_cache.json"
        if sig_path.exists():
            try:
                sig_data = json.loads(sig_path.read_text(encoding="utf-8"))
                vix = float(sig_data.get("vix", vix))
            except Exception as e:
                logger.debug(f"Signal cache read error: {e}")

        # 3. KIS Portfolio PnL for returns feedback
        port_path = self.results_dir / "kis_portfolio.json"
        if port_path.exists():
            try:
                port_data = json.loads(port_path.read_text(encoding="utf-8"))
                holdings = port_data.get("holdings", [])
                if holdings:
                    pnl_list = [float(h.get("pnl_pct", 0.0)) / 100.0 for h in holdings if "pnl_pct" in h]
                    if pnl_list:
                        market_returns = pnl_list
            except Exception as e:
                logger.debug(f"Portfolio read error: {e}")

        return {
            "regime": regime,
            "vix": vix,
            "returns": market_returns
        }

    def run_cycle(self) -> dict:
        """Execute full Track 2 Frontier Research & Self-Evolution Cycle."""
        logger.info("=" * 70)
        logger.info(f"  🔬 [Track 2 Frontier Laboratory] 가동 (Mode: {self.mode}, DryRun: {self.dry_run})")
        logger.info("  🛡️  Execution Sandbox   : STRICT_SHADOW_SANDBOX (Zero Live Execution)")
        logger.info("=" * 70)

        ctx = self._load_market_context()
        logger.info(f"  📊 Market Context: Regime={ctx['regime']}, VIX={ctx['vix']}, Samples={len(ctx['returns'])}")

        telemetry = {
            "timestamp": datetime.now().isoformat(),
            "mode": self.mode,
            "dry_run": self.dry_run,
            "sandbox_isolation": "STRICT_SHADOW_SANDBOX",
            "order_execution_blocked": True,
            "modules": {}
        }

        # ── Step 1: Active Inference & Autoimmune Telemetry ──
        logger.info("\n  [1/3] 🧬 Active Inference & Free Energy Optimization...")
        try:
            active_engine = ActiveInferenceEngine(is_shadow_sandbox=True)
            mock_ticks = [
                {"spread": 0.0012, "volume": 1200.0},
                {"spread": 0.0018, "volume": 2400.0},
                {"spread": 0.0025, "volume": 3100.0},
                {"spread": 0.0015, "volume": 1800.0}
            ]
            shadow_res = active_engine.run_shadow_cycle(
                current_regime=ctx["regime"],
                market_ticks=mock_ticks,
                live_returns=ctx["returns"]
            )
            telemetry["modules"]["active_inference"] = {
                "status": "SUCCESS",
                "free_energy": shadow_res.get("free_energy_metrics"),
                "phase_transition": shadow_res.get("phase_transition"),
                "factor_weights": shadow_res.get("factor_weights"),
                "surge_agonists": shadow_res.get("surge_agonists")
            }
            logger.info(f"     ✅ Variational Free Energy: {shadow_res.get('free_energy_metrics', {}).get('free_energy', 0):.4f}")
            logger.info(f"     ✅ Factor Dormancy State: {shadow_res.get('factor_weights')}")
        except Exception as e:
            logger.error(f"     ❌ Active Inference Cycle 실패: {e}", exc_info=True)
            telemetry["modules"]["active_inference"] = {"status": "ERROR", "error": str(e)}

        # ── Step 2: Soliton Causal Diversity & MAP-Elites Resonance ──
        logger.info("\n  [2/3] 🌊 Soliton Causal Diversity & KdV Hamiltonian Modulation...")
        try:
            soliton_engine = SolitonCausalDiversityEngine()
            baseline_weights = {
                "S0": 0.05, "S1": 0.25, "S2": 0.20, "S3": 0.10,
                "S5": 0.20, "S11_HIGHBETA_SNIPER": 0.10, "S14_PAIR_ALPHA": 0.10
            }
            np.random.seed(42)
            n_samples = max(10, len(ctx["returns"]))
            stream_sigs = {
                k: np.random.normal(0.01, 0.02, n_samples)
                for k in baseline_weights.keys()
            }
            stream_resids = {
                k: v - np.mean(v) for k, v in stream_sigs.items()
            }
            m_returns = np.array(ctx["returns"]) if len(ctx["returns"]) >= 5 else np.random.normal(0.001, 0.01, n_samples)

            soliton_res = soliton_engine.process_frontier_evolution(
                current_allocations=baseline_weights,
                stream_signals=stream_sigs,
                stream_residuals=stream_resids,
                market_returns=m_returns
            )
            telemetry["modules"]["soliton_causal"] = {
                "status": "SUCCESS",
                "modulated_weights": soliton_res.get("modulated_weights"),
                "causal_invariance_scores": soliton_res.get("causal_invariance_scores"),
                "elites_count": soliton_res.get("elites_count"),
                "zero_bias_verified": soliton_res.get("zero_bias_verified")
            }
            logger.info(f"     ✅ KdV Soliton Zero-Bias Verified: {soliton_res.get('zero_bias_verified')}")
            logger.info(f"     ✅ MAP-Elites Archive Size: {soliton_res.get('elites_count')}")
        except Exception as e:
            logger.error(f"     ❌ Soliton Causal Diversity 실패: {e}", exc_info=True)
            telemetry["modules"]["soliton_causal"] = {"status": "ERROR", "error": str(e)}

        # ── Step 3: Genetic Rule Evolution & Dynamic Overrides Update ──
        logger.info("\n  [3/3] 🔬 Genetic Rule Evolver (Chromosome Self-Adaptation)...")
        try:
            evolver = GeneticRuleEvolver()
            ret_arr = np.array(ctx["returns"])
            sharpe_est = float(np.mean(ret_arr) / (np.std(ret_arr) + 1e-8) * np.sqrt(252)) if len(ret_arr) > 1 else 1.2
            gap_metrics = {"recent_sharpe": sharpe_est}

            if not self.dry_run:
                ev_result = evolver.run_evolution(gap_metrics=gap_metrics)
                telemetry["modules"]["genetic_evolution"] = {
                    "status": "SUCCESS",
                    "best_chromosome": ev_result.get("best_chromosome"),
                    "overrides_updated": ev_result.get("overrides_updated")
                }
                logger.info(f"     ✅ Dynamic Overrides Saved to results/dynamic_overrides.json ({ev_result.get('overrides_updated')} keys)")
                logger.info(f"     🧬 Evolved Chromosome: {ev_result.get('best_chromosome')}")
            else:
                logger.info("     ⚠️ Dry-Run: Skipping disk override modification.")
                telemetry["modules"]["genetic_evolution"] = {"status": "SKIPPED_DRY_RUN"}
        except Exception as e:
            logger.error(f"     ❌ Genetic Evolution 실패: {e}", exc_info=True)
            telemetry["modules"]["genetic_evolution"] = {"status": "ERROR", "error": str(e)}

        # ── Save Laboratory Telemetry ──
        telemetry_file = self.results_dir / "frontier_laboratory_telemetry.json"
        atomic_write_json(str(telemetry_file), telemetry)
        logger.info(f"\n  💾 Telemetry saved: {telemetry_file}")

        history_dir = self.results_dir / "shadow_history"
        history_dir.mkdir(parents=True, exist_ok=True)
        today_str = datetime.now().strftime("%Y-%m-%d")
        history_file = history_dir / f"frontier_lab_{today_str}.json"
        atomic_write_json(str(history_file), telemetry)
        logger.info(f"  💾 Daily History Ledger saved: {history_file}")

        logger.info("=" * 70)
        logger.info("  🏁 [Track 2 Frontier Laboratory] Cycle 완료!")
        logger.info("=" * 70)
        return telemetry


def main():
    parser = argparse.ArgumentParser(description="Meridian 3.0 Track 2 Frontier Laboratory Runner")
    parser.add_argument("--mode", type=str, default="daily", choices=["krx", "us", "daily"], help="Cycle mode")
    parser.add_argument("--dry-run", action="store_true", help="Run without writing overrides")
    args = parser.parse_args()

    lab = FrontierLaboratory(mode=args.mode, dry_run=args.dry_run)
    lab.run_cycle()


if __name__ == "__main__":
    main()
