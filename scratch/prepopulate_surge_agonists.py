"""
Pre-populate Biological Surge Agonist Bank for Track 2 Alpha Maximization
==========================================================================
Ingests 5 major historical upside surge agonists for directional leverage expansion & inverse alpha:
1. Post-Crisis Liquidity Surge (VIX < 30, Coherence > 0.80, 200% Leverage Expansion)
2. Tech Super-Cycle Agonist (SOXX/NVDA Coherence > 0.85, 150% Tech Focus)
3. Inverse Bear Alpha Agonist (VIX/VIX3M > 1.15, EQ-Bond Corr > 0.50, 100% Inverse ETP / Short Alpha)
4. Rate Cut Rally Agonist (Institutional Impulse > 1.8sigma, 180% Semi Leverage)
5. Intraday Dip-Rebound Agonist (RSI < 25, Bollinger -2.5sigma, +1.5%~+3.0% Scalp Harvest)
"""

import os
import sys
import json
import logging

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SurgeAgonistPrePopulation")

def prepopulate_surge_agonists():
    logger.info("==========================================================")
    logger.info("  5 SURGE AGONIST LIBRARY PRE-POPULATION PIPELINE         ")
    logger.info("==========================================================")
    
    surge_agonists = {
        "Post_Crisis_Liquidity_Surge": {
            "agonist_name": "Post_Crisis_Liquidity_Surge",
            "coherence_threshold": 0.80,
            "vix_upper_limit": 30.0,
            "target_leverage_multiplier": 2.0,
            "target_etps": ["TQQQ", "KODEX_LEVERAGE", "SOXX"],
            "bayes_reactivation_factors": ["tech_momentum", "high_beta_sniper"],
            "strategy": "MAX_LONG_LEVERAGE"
        },
        "Tech_SuperCycle_Agonist": {
            "agonist_name": "Tech_SuperCycle_Agonist",
            "coherence_threshold": 0.85,
            "vix_upper_limit": 35.0,
            "target_leverage_multiplier": 1.5,
            "target_etps": ["SOXX", "NVDA", "KODEX_SEMICONDUCTOR"],
            "bayes_reactivation_factors": ["tech_momentum"],
            "strategy": "CONCENTRATED_TECH_BULL"
        },
        "Inverse_Bear_Alpha_Agonist": {
            "agonist_name": "Inverse_Bear_Alpha_Agonist",
            "coherence_threshold": 0.30,
            "vix_term_structure_min": 1.15,
            "equity_bond_corr_min": 0.50,
            "target_leverage_multiplier": 1.5,
            "target_etps": ["SQQQ", "KODEX_200_INVERSE_2X"],
            "bayes_reactivation_factors": ["inverse_alpha_engine"],
            "strategy": "BEAR_MARKET_INVERSE_ALPHA"
        },
        "Rate_Cut_Rally_Agonist": {
            "agonist_name": "Rate_Cut_Rally_Agonist",
            "coherence_threshold": 0.75,
            "institutional_impulse_sigma": 1.8,
            "target_leverage_multiplier": 1.8,
            "target_etps": ["SOXX", "QQQ", "KODEX_SEMICONDUCTOR"],
            "bayes_reactivation_factors": ["rate_cut_momentum"],
            "strategy": "SECTOR_IMPULSE_REBOUND"
        },
        "Intraday_Dip_Rebound_Agonist": {
            "agonist_name": "Intraday_Dip_Rebound_Agonist",
            "rsi_oversold_threshold": 25.0,
            "bollinger_lower_sigma": -2.5,
            "target_intraday_alpha_pct": 2.0,
            "target_etps": ["SOXX", "NVDA", "091160"],
            "bayes_reactivation_factors": ["intraday_scalp_sniper"],
            "strategy": "INTRADAY_VOLATILITY_SNIPING"
        }
    }
    
    out_file = os.path.join(PROJECT_ROOT, "results", "surge_agonist_bank.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(surge_agonists, f, indent=2, ensure_ascii=False)
        
    logger.info("==========================================================")
    logger.info(f"🎉 5 Major Surge Agonist Receptors successfully populated to: {out_file}")
    logger.info("==========================================================")
    
    return surge_agonists

if __name__ == "__main__":
    prepopulate_surge_agonists()
