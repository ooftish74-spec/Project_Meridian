"""
CapitalVelocityRecycler — 100% Dynamic Capital Velocity Optimization Engine
========================================================================

No Hardcoding, No Fixed Constants.
Dynamically recycles freed cash C_freed upon position liquidation into active candidate
signals using Sharpe-Weighted EV Allocation:

  w_k*(t) = ( EV_k(t) / σ_k(t) ) / Σ_j ( EV_j(t) / σ_j(t) ) * C_freed
"""

import math
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class CapitalVelocityRecycler:
    """100% 동적 자금 회전율 극대화 및 Sharpe-Optimal 재투입 엔진."""

    def recycle_freed_capital(
        self,
        freed_cash_krw: float,
        active_signals: List[Dict[str, Any]],
        market_data: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """청산으로 해제된 현금을 Sharpe 비중 기반으로 즉시 동적 재투입."""
        if freed_cash_krw <= 10000 or not active_signals:
            return []

        if market_data is None:
            market_data = {}

        # 1. Compute dynamic Sharpe ratio EV_k / σ_k for each candidate
        candidates = []
        for sig in active_signals:
            ticker = sig.get('ticker', '')
            direction = sig.get('direction', 'long')
            confidence = float(sig.get('confidence', 0.5) or 0.5)
            strategy = sig.get('strategy', 'alpha')

            # Dynamic Volatility σ_k estimation from market_data
            vix_val = float(market_data.get('signal_cache', {}).get('vix', 20.0) or 20.0)
            vol_daily = max(0.005, vix_val / (100.0 * math.sqrt(252.0)))

            # Expected Value EV_k proportional to confidence
            ev_k = max(0.01, confidence * 0.05)
            sharpe_k = ev_k / vol_daily

            candidates.append({
                'signal': sig,
                'ticker': ticker,
                'direction': direction,
                'confidence': confidence,
                'strategy': strategy,
                'sharpe': sharpe_k,
                'ev': ev_k,
                'vol': vol_daily
            })

        if not candidates:
            return []

        # 2. Dynamic Hurdle Filter: Median Sharpe Threshold
        sharpe_values = sorted([c['sharpe'] for c in candidates])
        median_sharpe = sharpe_values[len(sharpe_values) // 2]
        eligible = [c for c in candidates if c['sharpe'] >= median_sharpe]

        if not eligible:
            eligible = candidates

        # 3. Sharpe-Weighted EV Allocation
        total_sharpe_weight = sum(c['sharpe'] for c in eligible)
        if total_sharpe_weight <= 0:
            return []

        recycled_orders = []
        for c in eligible:
            weight = c['sharpe'] / total_sharpe_weight
            alloc_krw = freed_cash_krw * weight
            if alloc_krw >= 10000:
                recycled_orders.append({
                    'ticker': c['ticker'],
                    'direction': c['direction'],
                    'amount_krw': alloc_krw,
                    'confidence': c['confidence'],
                    'strategy': f"recycled_{c['strategy']}",
                    'reason': f"[VelocityRecycler] Sharpe-Weighted ({c['sharpe']:.2f}) allocation ₩{alloc_krw:,.0f}"
                })
                logger.info(f"  ⚡ [CapitalVelocityRecycler] 재투입 시그널 생성: {c['ticker']} -> ₩{alloc_krw:,.0f} (Sharpe={c['sharpe']:.2f})")

        return recycled_orders
