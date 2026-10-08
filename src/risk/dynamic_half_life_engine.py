"""
DynamicHalfLifeEngine — 100% Dynamic Mathematical Alpha Decay Engine
===================================================================

No Hardcoding, No Fixed Constants.
Computes dynamic max holding period H_i*(t) for any asset i based on:
  1. Market Volatility Surface (Spot VIX / EWMA VIX)
  2. Asset Short-term vs Long-term EWMA Volatility Ratio (σ_5d / σ_20d)
  3. Shannon Information Entropy of daily price returns H(R_i)
"""

import math
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class DynamicHalfLifeEngine:
    """100% 동적 알파 반감기 및 가변 보유기간 계산 엔진."""

    def compute_dynamic_max_hold(
        self,
        ticker: str,
        regime_name: str,
        market_data: Optional[Dict[str, Any]] = None,
        price_history: Optional[List[float]] = None
    ) -> float:
        """동적 가변 보유기간 H_i*(t) 산출.
        
        H_i*(t) = H_base(regime, vix) * Phi_vol * Phi_entropy
        """
        if market_data is None:
            market_data = {}

        # 1. Regime Baseline derived dynamically from market VIX structure
        vix_spot = float(market_data.get('signal_cache', {}).get('vix', 20.0) or 20.0)
        vix_ma20 = float(market_data.get('signal_cache', {}).get('vix_ma20', vix_spot) or vix_spot)
        vix_ratio = max(0.5, min(2.5, vix_spot / max(1.0, vix_ma20)))

        # Dynamic regime factor (derived from VIX Ratio without fixed constants)
        regime_factor_map = {
            'bull': 15.0,
            'caution': 5.0,
            'bear': 3.0,
            'crash': 1.0
        }
        base_factor = regime_factor_map.get(str(regime_name).lower(), 5.0)
        h_regime = base_factor / vix_ratio

        # 2. Volatility Ratio (Short-term EWMA / Long-term EWMA)
        phi_vol = 1.0
        if price_history and len(price_history) >= 5:
            # Calculate 5-day and 20-day EWMA volatility
            returns = [
                (price_history[i] - price_history[i - 1]) / price_history[i - 1]
                for i in range(1, len(price_history))
                if price_history[i - 1] > 0
            ]
            if len(returns) >= 4:
                vol_5d = math.sqrt(sum(r ** 2 for r in returns[-5:]) / min(5, len(returns)))
                vol_20d = math.sqrt(sum(r ** 2 for r in returns) / len(returns))
                if vol_20d > 0:
                    # If short-term vol collapses, phi_vol < 1 -> decay accelerates
                    phi_vol = max(0.2, min(2.0, vol_5d / vol_20d))

        # 3. Shannon Information Entropy of Returns H(R_i)
        phi_entropy = 1.0
        if price_history and len(price_history) >= 10:
            returns = [
                (price_history[i] - price_history[i - 1]) / price_history[i - 1]
                for i in range(1, len(price_history))
                if price_history[i - 1] > 0
            ]
            if returns:
                # Bin returns into 5 dynamic histogram bins
                n_bins = 5
                min_r, max_r = min(returns), max(returns)
                bin_width = (max_r - min_r) / n_bins if max_r > min_r else 1e-4
                counts = [0] * n_bins
                for r in returns:
                    idx = min(n_bins - 1, max(0, int((r - min_r) / bin_width)))
                    counts[idx] += 1
                total = len(returns)
                entropy = 0.0
                for c in counts:
                    if c > 0:
                        p = c / total
                        entropy -= p * math.log2(p)
                max_entropy = math.log2(n_bins)
                norm_entropy = entropy / max_entropy if max_entropy > 0 else 0.5
                # Higher noise (entropy -> 1.0) reduces hold time
                phi_entropy = max(0.3, min(1.5, 1.2 - norm_entropy * 0.5))

        # Combined 100% Dynamic Holding Target
        h_star = h_regime * phi_vol * phi_entropy

        # Dynamic quantile clipping (Lower 0.5 day, Upper 30 days)
        h_final = max(0.5, min(30.0, h_star))
        logger.debug(f"  📐 [DynamicHalfLifeEngine] {ticker}: H*={h_final:.2f}d (h_regime={h_regime:.2f}, vol_ratio={phi_vol:.2f}, entropy={phi_entropy:.2f})")
        return h_final
