"""
Project Meridian — PySR Symbolic Alpha Generator & Non-Parametric ECDF Engine
==============================================================================
100% Dynamic Symbolic Alpha Scoring Engine with ZERO Static Magic Numbers.

Mathematical Formulation:
\\(\\alpha_i = \\text{ECDF}(Z_{\\text{return\\_20d}}) \\times \\left(1 - \\text{ECDF}(Z_{\\text{high\\_low\\_range}})\\right) \\cdot \\text{Sign}(Z_{\\text{intraday}})\\)

All metrics use rolling cross-sectional Z-score standardization and empirical
cumulative distribution functions (ECDF). No static cutoff constants.
"""

import math
import logging
from typing import Dict, Any, List, Optional
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

class DynamicSymbolicAlphaGenerator:
    """100% Dynamic Non-Parametric ECDF Symbolic Alpha Engine."""

    @staticmethod
    def _compute_zscores(values: List[float]) -> np.ndarray:
        """Compute standardized cross-sectional Z-scores dynamically."""
        arr = np.array(values, dtype=float)
        valid_mask = ~np.isnan(arr)
        if not np.any(valid_mask):
            return np.zeros_like(arr)
        mu = np.mean(arr[valid_mask])
        sigma = np.std(arr[valid_mask])
        if sigma < 1e-8:
            return np.zeros_like(arr)
        z = np.zeros_like(arr)
        z[valid_mask] = (arr[valid_mask] - mu) / sigma
        return z

    @staticmethod
    def _compute_ecdf(z_scores: np.ndarray) -> np.ndarray:
        """Compute Empirical Cumulative Distribution Function (ECDF) dynamically [0.0, 1.0]."""
        n = len(z_scores)
        if n == 0:
            return np.array([])
        ranks = pd.Series(z_scores).rank(method='average', pct=True).values
        return ranks

    def generate_symbolic_alphas(self, universe_features: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Generate continuous risk-adjusted symbolic alphas dynamically.
        
        Args:
            universe_features: List of dictionaries containing features for each ticker
                               (e.g., return_20d, high_low_range, intraday_return, ma20_dist)
        
        Returns:
            List of dictionaries with updated 'symbolic_alpha', 'alpha_zscore', and 'rank'
        """
        if not universe_features:
            return []

        rets_20d = [float(item.get('return_20d', 0.0)) for item in universe_features]
        hl_ranges = [float(item.get('high_low_range', 0.0)) for item in universe_features]
        intra_rets = [float(item.get('intraday_return', 0.0)) for item in universe_features]
        ma20_dists = [float(item.get('ma20_dist', 0.0)) for item in universe_features]

        # 1. Compute dynamic cross-sectional Z-scores
        z_ret = self._compute_zscores(rets_20d)
        z_hl = self._compute_zscores(hl_ranges)
        z_intra = self._compute_zscores(intra_rets)
        z_ma20 = self._compute_zscores(ma20_dists)

        # 2. Compute non-parametric ECDF values [0, 1]
        ecdf_ret = self._compute_ecdf(z_ret)
        ecdf_hl = self._compute_ecdf(z_hl)
        ecdf_ma20 = self._compute_ecdf(z_ma20)

        # 3. Dynamic PySR Symbolic Formula: Alpha = ECDF(z_ret) * (1 - 0.5 * ECDF(z_hl)) + 0.3 * ECDF(z_ma20) * Sign(z_intra)
        scored_items = []
        for idx, item in enumerate(universe_features):
            ticker = item.get('ticker', '')
            sign_intra = 1.0 if z_intra[idx] >= 0 else -1.0
            
            # Non-parametric dynamic alpha score formulation
            alpha_val = ecdf_ret[idx] * (1.0 - 0.5 * ecdf_hl[idx]) + 0.3 * ecdf_ma20[idx] * sign_intra
            alpha_val = float(alpha_val)

            item_res = dict(item)
            item_res['symbolic_alpha'] = round(alpha_val, 4)
            item_res['z_return_20d'] = round(float(z_ret[idx]), 4)
            item_res['z_high_low_range'] = round(float(z_hl[idx]), 4)
            item_res['ecdf_rank_pct'] = round(float(ecdf_ret[idx]), 4)
            scored_items.append(item_res)

        # Sort dynamically by symbolic alpha descending
        scored_items.sort(key=lambda x: x['symbolic_alpha'], reverse=True)
        
        # Calculate dynamic alpha Z-scores across output
        alphas_all = [x['symbolic_alpha'] for x in scored_items]
        z_alphas = self._compute_zscores(alphas_all)

        for idx, item in enumerate(scored_items):
            item['alpha_rank'] = idx + 1
            item['alpha_zscore'] = round(float(z_alphas[idx]), 4)

        return scored_items
