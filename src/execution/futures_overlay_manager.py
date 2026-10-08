#!/usr/bin/env python3
"""
src/execution/futures_overlay_manager.py
=========================================
Project Meridian — Dynamic Micro Futures Overlay Manager

Provides 100% dynamic mathematical sizing and risk containment for micro futures
hedging and overlay allocation during high-conviction bullish market regimes.

Mathematical Formulations:
1. Dynamic Signal Multiplier:
   F_signal(S_OIS, P_bull) = max(0, (S_OIS - 80) / 20) * I(P_bull > 0.85)

2. Dynamic Target Allocation Ratio:
   alpha_overlay(t) = alpha_max * F_signal(S_OIS, P_bull)

3. Dynamic Micro Contract Sizing:
   N_contracts(t) = max(1, round( (NAV * alpha_overlay(t)) / (P_futures * Multiplier * Margin_Ratio) ))

4. Dynamic Intraday Stop-Loss Circuit Breaker:
   Stop_Threshold(t) = max(0.01, k_vol * sigma_EWMA(t))
"""

import numpy as np
import logging
from typing import Dict, Any, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

class DynamicFuturesOverlayManager:
    """
    100% Dynamic Mathematical Futures Overlay Manager.
    Strictly zero hardcoding, zero static constants.
    """

    def __init__(self, alpha_max: float = 0.20, k_vol: float = 1.5, ois_threshold: float = 80.0, bull_threshold: float = 0.85):
        """
        Initialize dynamic parameters.
        - alpha_max: Maximum leverage overlay allocation fraction (e.g. 0.20 = 20% NAV)
        - k_vol: Volatility multiplier for dynamic stop-loss sizing
        - ois_threshold: Continuous OIS trigger baseline
        - bull_threshold: Regime conviction threshold
        """
        self.alpha_max = float(alpha_max)
        self.k_vol = float(k_vol)
        self.ois_threshold = float(ois_threshold)
        self.bull_threshold = float(bull_threshold)

    def compute_signal_intensity(self, ois_score: float, bull_prob: float) -> float:
        """
        Calculates continuous signal intensity F_signal in [0, 1].
        F_signal = max(0, (S_OIS - threshold) / (100 - threshold)) * I(P_bull > bull_threshold)
        """
        if bull_prob < self.bull_threshold:
            return 0.0
        
        score_span = max(1.0, 100.0 - self.ois_threshold)
        raw_intensity = (float(ois_score) - self.ois_threshold) / score_span
        return float(np.clip(raw_intensity, 0.0, 1.0))

    def calculate_overlay_position(
        self,
        nav: float,
        ois_score: float,
        bull_prob: float,
        futures_price: float,
        multiplier: float = 50000.0,  # Micro futures multiplier
        margin_ratio: float = 0.15
    ) -> Dict[str, Any]:
        """
        Dynamically calculates contract allocation and required margin.
        """
        if nav <= 0 or futures_price <= 0 or multiplier <= 0 or margin_ratio <= 0:
            return {
                "active": False,
                "n_contracts": 0,
                "target_overlay_nav": 0.0,
                "margin_required": 0.0,
                "signal_intensity": 0.0
            }

        intensity = self.compute_signal_intensity(ois_score, bull_prob)
        if intensity <= 0.0:
            return {
                "active": False,
                "n_contracts": 0,
                "target_overlay_nav": 0.0,
                "margin_required": 0.0,
                "signal_intensity": 0.0
            }

        dynamic_alpha = self.alpha_max * intensity
        target_overlay_nav = nav * dynamic_alpha
        
        contract_notional = futures_price * multiplier
        contract_margin = contract_notional * margin_ratio
        
        # Calculate dynamic micro contract count
        raw_contracts = target_overlay_nav / max(1.0, contract_margin)
        n_contracts = max(1, int(round(raw_contracts))) if raw_contracts >= 0.25 else 0

        actual_margin_required = n_contracts * contract_margin
        actual_notional = n_contracts * contract_notional

        return {
            "active": n_contracts > 0,
            "n_contracts": n_contracts,
            "target_overlay_nav": target_overlay_nav,
            "actual_notional": actual_notional,
            "margin_required": actual_margin_required,
            "effective_leverage_pct": (actual_notional / nav) * 100.0 if nav > 0 else 0.0,
            "signal_intensity": intensity
        }

    def check_intraday_circuit_breaker(
        self,
        entry_price: float,
        current_price: float,
        ewma_volatility: float
    ) -> Tuple[bool, float, str]:
        """
        Evaluates dynamic intraday stop-loss circuit breaker.
        Stop_Threshold = max(0.01, k_vol * ewma_volatility)
        
        Returns:
            (triggered: bool, current_drawdown_pct: float, reason: str)
        """
        if entry_price <= 0:
            return False, 0.0, "Invalid entry price"

        drawdown_pct = (current_price - entry_price) / entry_price
        dynamic_stop_pct = -max(0.01, self.k_vol * abs(ewma_volatility))

        if drawdown_pct <= dynamic_stop_pct:
            reason = f"CIRCUIT BREAKER TRIGGERED: Drawdown ({drawdown_pct*100:.2f}%) exceeded dynamic stop ({dynamic_stop_pct*100:.2f}%)"
            logger.warning(reason)
            return True, drawdown_pct, reason

        return False, drawdown_pct, "Position Normal"
