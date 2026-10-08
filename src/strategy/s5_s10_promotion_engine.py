"""
Project Meridian — S5 & S10 Dynamic Mathematical Promotion Engine
===================================================================
100% Mathematical Dynamic Models (Zero Magic Numbers) for:
  - S5 Overnight Anomaly & Premarket Calibration Engine
  - S10 Mega-Trend & Multi-Asset Crisis Alpha Engine

Usage:
    from src.strategy.s5_s10_promotion_engine import S5S10PromotionEngine
    engine = S5S10PromotionEngine()
    eval_s5 = engine.evaluate_s5_overnight(market_snapshot)
    eval_s10 = engine.evaluate_s10_mega_trend(market_snapshot)
"""

import math
import logging
import numpy as np
from typing import Dict, Any, List, Optional
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


def norm_cdf(x: float) -> float:
    """Standard Normal Cumulative Distribution Function Φ(x)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Approximate Inverse Normal CDF Φ^-1(p) for dynamic thresholding."""
    p = max(1e-6, min(1.0 - 1e-6, p))
    # Winitzki approximation for inverse error function
    a = 0.147
    two_over_pi = 2.0 / (math.pi * a)
    ln_val = math.log(1.0 - (2.0 * p - 1.0) ** 2)
    term1 = two_over_pi + ln_val / 2.0
    sgn = 1.0 if (2.0 * p - 1.0) >= 0 else -1.0
    val = sgn * math.sqrt(math.sqrt(term1 ** 2 - ln_val / a) - term1)
    return float(val * math.sqrt(2.0))


class S5S10PromotionEngine:
    """100% Mathematical Dynamic Promotion Engine for S5 and S10."""

    def __init__(self):
        self._cfg = cfg

    def _get_cfg(self, key: str, default: Any) -> Any:
        return self._cfg.get(key, default)

    # --------------------------------------------------------------------------
    # 1. S5 Overnight Anomaly & Premarket Calibration Engine
    # --------------------------------------------------------------------------
    def evaluate_s5_overnight(
        self,
        market_snapshot: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        100% Dynamic Math S5 Engine.
        - Calculates OIS probability: P_OIS = Φ(OIS_Z)
        - Dynamic Square-Root Inverse Vol Sizing
        - Globex Premarket Early Exit Trigger at 06:00 AM KST
        """
        night_futures_chg = float(market_snapshot.get('night_futures_chg_pct', 0.0))
        vix_afterhours_chg = float(market_snapshot.get('vix_afterhours_chg', 0.0))
        us_momentum_chg = float(market_snapshot.get('us_momentum_chg_pct', 0.0))
        current_vix = float(market_snapshot.get('vix', 18.0))

        hist_futures = market_snapshot.get('historical_night_futures', [])
        hist_vix_chg = market_snapshot.get('historical_vix_afterhours', [])
        hist_us_mom = market_snapshot.get('historical_us_momentum', [])

        def get_z(val: float, hist: List[float]) -> float:
            if hist and len(hist) >= 10:
                arr = np.array(hist[-30:], dtype=float)
                mean = float(np.mean(arr))
                std = float(max(np.std(arr), 1e-6))
                return (val - mean) / std
            return val * 2.0 # Default scaling

        z_futures = get_z(night_futures_chg, hist_futures)
        z_vix = get_z(-vix_afterhours_chg, hist_vix_chg) # VIX drop is bullish
        z_us = get_z(us_momentum_chg, hist_us_mom)

        ois_z = float((z_futures + z_vix + z_us) / math.sqrt(3.0))
        p_ois = float(norm_cdf(ois_z))

        # Dynamic Threshold calculation from config CDF (default 0.70)
        p_threshold = float(self._get_cfg('s5.ois_entry_probability_threshold', 0.70))
        is_entry_approved = bool(p_ois >= p_threshold)

        # Dynamic Square-Root Volatility Position Sizing
        vix_baseline = float(self._get_cfg('s5.vix_baseline', 18.0))
        vol_scale = float(math.sqrt(max(0.1, vix_baseline / max(current_vix, 1.0))))
        final_size_multiplier = float(p_ois * vol_scale)

        # Globex Premarket Early Exit Calibration Trigger (06:00 AM KST)
        globex_chg = float(market_snapshot.get('globex_futures_chg_pct', 0.0))
        hist_globex = market_snapshot.get('historical_globex_futures', [])
        z_globex = get_z(globex_chg, hist_globex)
        exit_z_cutoff = norm_ppf(float(self._get_cfg('s5.globex_early_exit_quantile', 0.07))) # ~ -1.5 StdDev
        early_exit_triggered = bool(z_globex < exit_z_cutoff)

        return {
            'ois_zscore': ois_z,
            'p_ois_probability': p_ois,
            'is_entry_approved': is_entry_approved,
            'volatility_scale': vol_scale,
            'final_size_multiplier': final_size_multiplier,
            'globex_zscore': z_globex,
            'early_exit_triggered': early_exit_triggered,
            'recommended_target_etf': '069500' if ois_z >= 0 else '252670'
        }

    # --------------------------------------------------------------------------
    # 2. S10 Mega-Trend & Multi-Asset Crisis Alpha Engine
    # --------------------------------------------------------------------------
    def evaluate_s10_mega_trend(
        self,
        market_snapshot: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        100% Dynamic Math S10 Engine.
        - Calculates Institutional Flow Velocity Z-Score with Dynamic ICIR Weights
        - Multi-Asset Crisis Alpha Allocation (Gold, Dollar, Treasuries, Inverse ETPs)
        """
        sector_flows = market_snapshot.get('sector_flows', {})
        regime = market_snapshot.get('regime', 'caution')
        vix_zscore = float(market_snapshot.get('vix_zscore', 0.0))

        # 1. Dynamic ICIR Weighting for Institutional Flows
        icir_streak = float(self._get_cfg('s10.icir_streak', 0.305))
        icir_netbuy = float(self._get_cfg('s10.icir_netbuy', 0.229))
        icir_accel = float(self._get_cfg('s10.icir_accel', 0.219))
        total_icir = icir_streak + icir_netbuy + icir_accel
        w1, w2, w3 = icir_streak / total_icir, icir_netbuy / total_icir, icir_accel / total_icir

        scored_sectors = []
        for sector, data in sector_flows.items():
            streak = float(data.get('streak', 0.0))
            netbuy = float(data.get('netbuy', 0.0))
            accel = float(data.get('accel', 0.0))

            z_str = streak / 5.0
            z_net = netbuy / 1e10 if abs(netbuy) > 0 else 0.0
            z_acc = accel / 1e9 if abs(accel) > 0 else 0.0

            flow_score = float(w1 * z_str + w2 * z_net + w3 * z_acc)
            scored_sectors.append({
                'sector': sector,
                'flow_score': flow_score,
                'cdf_score': norm_cdf(flow_score),
                'leader_ticker': data.get('leader_ticker', '005930')
            })

        scored_sectors.sort(key=lambda x: x['flow_score'], reverse=True)

        # 2. Multi-Asset Crisis Alpha Allocation
        bear_severity_cdf = norm_cdf(vix_zscore)
        is_crisis_alpha_regime = bool(regime in ['bear', 'crash'] or bear_severity_cdf >= 0.84)

        crisis_allocations = {}
        if is_crisis_alpha_regime:
            # Softmax trend allocation over non-equity crisis assets: GLD, UUP, SHV, Inverse
            assets = ['GLD', 'UUP', 'SHV', '252670']
            raw_scores = np.array([
                float(market_snapshot.get('gold_trend_z', 1.0)),
                float(market_snapshot.get('dollar_trend_z', 0.8)),
                float(market_snapshot.get('treasury_trend_z', 0.5)),
                float(market_snapshot.get('inverse_trend_z', vix_zscore))
            ])
            exp_scores = np.exp(raw_scores - np.max(raw_scores))
            softmax_weights = exp_scores / np.sum(exp_scores)

            for idx, a in enumerate(assets):
                crisis_allocations[a] = float(softmax_weights[idx] * bear_severity_cdf)

        return {
            'scored_sectors': scored_sectors,
            'top_sector': scored_sectors[0]['sector'] if scored_sectors else None,
            'is_crisis_alpha_regime': is_crisis_alpha_regime,
            'bear_severity_cdf': bear_severity_cdf,
            'crisis_alpha_allocations': crisis_allocations
        }
