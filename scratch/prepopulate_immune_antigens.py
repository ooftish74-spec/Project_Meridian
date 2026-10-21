"""
Pre-populate Biological Immune Antigen Bank using Historical Market Crash Data (Comprehensive 10-Crisis Expansion)
=====================================================================================================================
Ingests 10 major 30-year historical crisis epochs and multi-dimensional quantitative features:
1. 1998 LTCM Moratorium & Arbitrage Breakdown
2. 2000 Dot-Com Bubble Burst & Tech Unwind
3. 2008 Financial Crisis & Lehman Collapse
4. 2011 US Credit Rating Downgrade
5. 2015 China Stock Market Crash & Yuan Devaluation
6. 2018 Volmageddon & VIX Spike
7. 2020 COVID-19 Liquidity Flash Crash
8. 2022 Fed Rate Hike Crunch & Bond/Equity Correlation Flip
9. 2023 SVB Regional Bank Run Collapse
10. 2024 Yen Carry Trade Global Unwind

Ingests Multi-Dimensional Immune Markers:
- VIX Term Structure Ratio (VIX / VIX3M > 1.15)
- Equity-Bond 20d Correlation (rho_equity_bond > 0.50)
- Foreign Net Outflow Velocity (3d Net Outflow > ₩1.5T)
- FX Volatility Acceleration (d(USD/KRW)/dt > 15 KRW/day)
- Global Semi Disconnect Gap (SOXX-KODEX Gap > 3.5%)
"""

import os
import sys
import json
import numpy as np
import logging

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ImmuneAntigenExpansion")

def prepopulate_historical_antigens():
    logger.info("==========================================================")
    logger.info("  10-CRISIS COMPREHENSIVE IMMUNE ANTIGEN BANK PIPELINE    ")
    logger.info("==========================================================")
    
    # 10 Major Historical Crisis Epochs with 5 Multi-Dimensional Feature Vectors
    crisis_epochs = {
        "1998_LTCM_Moratorium": {
            "mdd": -0.22,
            "vix_peak": 45.0,
            "entropy_spike": 3.6,
            "vix_term_structure_ratio": 1.25,
            "equity_bond_correlation": -0.15,
            "foreign_net_flow_velocity_krw": -1.8,
            "fx_acceleration_krw_per_day": 25.0,
            "soxx_kodex_semi_gap_pct": 4.2,
            "regime": "bear"
        },
        "2000_DotCom_Burst": {
            "mdd": -0.78,
            "vix_peak": 48.0,
            "entropy_spike": 3.9,
            "vix_term_structure_ratio": 1.18,
            "equity_bond_correlation": -0.30,
            "foreign_net_flow_velocity_krw": -2.2,
            "fx_acceleration_krw_per_day": 12.0,
            "soxx_kodex_semi_gap_pct": 5.8,
            "regime": "bear"
        },
        "2008_Lehman_Crisis": {
            "mdd": -0.55,
            "vix_peak": 80.0,
            "entropy_spike": 4.5,
            "vix_term_structure_ratio": 1.42,
            "equity_bond_correlation": -0.45,
            "foreign_net_flow_velocity_krw": -3.5,
            "fx_acceleration_krw_per_day": 35.0,
            "soxx_kodex_semi_gap_pct": 6.5,
            "regime": "bear"
        },
        "2011_US_Credit_Downgrade": {
            "mdd": -0.18,
            "vix_peak": 48.0,
            "entropy_spike": 3.2,
            "vix_term_structure_ratio": 1.20,
            "equity_bond_correlation": -0.25,
            "foreign_net_flow_velocity_krw": -1.6,
            "fx_acceleration_krw_per_day": 18.0,
            "soxx_kodex_semi_gap_pct": 3.8,
            "regime": "bear"
        },
        "2015_China_Yuan_Crash": {
            "mdd": -0.15,
            "vix_peak": 40.0,
            "entropy_spike": 3.1,
            "vix_term_structure_ratio": 1.16,
            "equity_bond_correlation": -0.10,
            "foreign_net_flow_velocity_krw": -1.9,
            "fx_acceleration_krw_per_day": 20.0,
            "soxx_kodex_semi_gap_pct": 4.0,
            "regime": "caution"
        },
        "2018_Volmageddon": {
            "mdd": -0.12,
            "vix_peak": 50.0,
            "entropy_spike": 4.1,
            "vix_term_structure_ratio": 1.35,
            "equity_bond_correlation": 0.15,
            "foreign_net_flow_velocity_krw": -1.2,
            "fx_acceleration_krw_per_day": 8.0,
            "soxx_kodex_semi_gap_pct": 3.2,
            "regime": "caution"
        },
        "2020_COVID_Flash_Crash": {
            "mdd": -0.35,
            "vix_peak": 82.0,
            "entropy_spike": 4.8,
            "vix_term_structure_ratio": 1.48,
            "equity_bond_correlation": -0.50,
            "foreign_net_flow_velocity_krw": -4.1,
            "fx_acceleration_krw_per_day": 28.0,
            "soxx_kodex_semi_gap_pct": 5.2,
            "regime": "bear"
        },
        "2022_Fed_Rate_Hike_Crunch": {
            "mdd": -0.28,
            "vix_peak": 36.0,
            "entropy_spike": 2.9,
            "vix_term_structure_ratio": 1.12,
            "equity_bond_correlation": 0.65,  # Bond/Equity correlation flip!
            "foreign_net_flow_velocity_krw": -1.5,
            "fx_acceleration_krw_per_day": 16.0,
            "soxx_kodex_semi_gap_pct": 3.5,
            "regime": "bear"
        },
        "2023_SVB_BankRun": {
            "mdd": -0.09,
            "vix_peak": 30.0,
            "entropy_spike": 2.5,
            "vix_term_structure_ratio": 1.14,
            "equity_bond_correlation": -0.20,
            "foreign_net_flow_velocity_krw": -0.9,
            "fx_acceleration_krw_per_day": 10.0,
            "soxx_kodex_semi_gap_pct": 2.8,
            "regime": "caution"
        },
        "2024_Yen_Carry_Unwind": {
            "mdd": -0.12,
            "vix_peak": 65.0,
            "entropy_spike": 3.8,
            "vix_term_structure_ratio": 1.30,
            "equity_bond_correlation": 0.10,
            "foreign_net_flow_velocity_krw": -2.8,
            "fx_acceleration_krw_per_day": 22.0,
            "soxx_kodex_semi_gap_pct": 4.8,
            "regime": "caution"
        }
    }
    
    antigen_bank = {}
    
    for epoch_name, metrics in crisis_epochs.items():
        antigen_vector = {
            "epoch": epoch_name,
            "regime": metrics["regime"],
            "entropy_threshold": metrics["entropy_spike"],
            "vix_trigger": metrics["vix_peak"],
            "vix_term_structure_ratio": metrics["vix_term_structure_ratio"],
            "equity_bond_correlation": metrics["equity_bond_correlation"],
            "foreign_net_flow_velocity_krw": metrics["foreign_net_flow_velocity_krw"],
            "fx_acceleration_krw_per_day": metrics["fx_acceleration_krw_per_day"],
            "soxx_kodex_semi_gap_pct": metrics["soxx_kodex_semi_gap_pct"],
            "bayesian_dormant_factors": ["tech_momentum", "high_beta_sniper"],
            "active_immune_factors": ["orderbook_entropy", "ois_macro_proxy", "cd_rate_cash"]
        }
        antigen_bank[epoch_name] = antigen_vector
        logger.info(f"✅ Ingested Loss Antigen [{epoch_name}]: VIX Peak {metrics['vix_peak']}, VIX/VIX3M {metrics['vix_term_structure_ratio']}, EQ-Bond Corr {metrics['equity_bond_correlation']}")

    out_file = os.path.join(PROJECT_ROOT, "results", "immune_antigen_bank.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(antigen_bank, f, indent=2, ensure_ascii=False)
        
    logger.info("==========================================================")
    logger.info(f"🎉 All 10 Historical Crisis Antigens & Multi-Dimensional Features populated to: {out_file}")
    logger.info("==========================================================")
    
    return antigen_bank

if __name__ == "__main__":
    prepopulate_historical_antigens()
