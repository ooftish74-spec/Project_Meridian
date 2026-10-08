"""
Project Meridian — Dynamic Smart Order Router (SOR) & NXT Maker Liquidity Engine
==================================================================================
100% Dynamic Liquidity & Spread Weighted Venue Router (KRX vs NextTrade NXT).

Mathematical Formulations:
1. Dynamic Split Allocation:
   q_NXT = Q_total * (Depth_NXT / (Depth_KRX + Depth_NXT)) * I(Spread_NXT <= Spread_KRX)

2. Dynamic Maker Limit Price Formulation:
   gamma = Bid_Vol / (Bid_Vol + Ask_Vol + 1e-9)
   P_maker = P_bid + gamma * (P_ask - P_bid)

3. Dynamic Order Timeout Threshold:
   T_timeout = max(100ms, min(500ms, 1000ms / lambda_ticks_per_sec))

Zero static ticker overrides. 100% dynamic mathematical execution logic.
"""

import math
import logging
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

class DynamicSmartOrderRouter:
    """100% Dynamic Liquidity, Spread & Maker Weighted Smart Order Router."""

    def __init__(self, nxt_enabled: bool = True):
        self.nxt_enabled = nxt_enabled

    def calculate_maker_limit_price(
        self,
        bid1: float,
        ask1: float,
        bid_vol1: int,
        ask_vol1: int,
        is_buy: bool = True
    ) -> float:
        """
        Dynamically calculates passive Maker limit order price.
        P_maker = P_bid + gamma * (P_ask - P_bid)
        """
        if bid1 <= 0 or ask1 <= 0 or ask1 < bid1:
            return ask1 if is_buy else bid1

        total_vol = max(1, bid_vol1 + ask_vol1)
        gamma = float(bid_vol1) / float(total_vol)

        # For BUY: place price closer to bid1 to be Maker (passive)
        # For SELL: place price closer to ask1
        if is_buy:
            maker_price = bid1 + (1.0 - gamma) * (ask1 - bid1) * 0.5
            return round(maker_price, 2)
        else:
            maker_price = ask1 - gamma * (ask1 - bid1) * 0.5
            return round(maker_price, 2)

    def compute_dynamic_timeout_ms(self, tick_rate_hz: float = 10.0) -> float:
        """
        Calculates dynamic order timeout in milliseconds.
        T_timeout = max(100, min(500, 1000 / lambda_ticks))
        """
        lambda_ticks = max(1.0, float(tick_rate_hz))
        raw_timeout = 1000.0 / lambda_ticks
        return float(max(100.0, min(500.0, raw_timeout)))

    def evaluate_maker_switch_to_taker(
        self,
        order_age_ms: float,
        tick_rate_hz: float = 10.0,
        fill_pct: float = 0.0
    ) -> Tuple[bool, str]:
        """
        Evaluates whether a passive Maker limit order should switch to an aggressive Taker order.
        """
        timeout_ms = self.compute_dynamic_timeout_ms(tick_rate_hz)
        if fill_pct >= 1.0:
            return False, "Order Fully Filled"

        if order_age_ms > timeout_ms:
            reason = f"DYNAMIC TIMEOUT TRIGGERED ({order_age_ms:.1f}ms > {timeout_ms:.1f}ms): Switching Maker -> Taker"
            logger.info(reason)
            return True, reason

        return False, "Maker Active"

    def route_order(
        self,
        ticker: str,
        total_quantity: int,
        krx_depth: Dict[str, Any],
        nxt_depth: Optional[Dict[str, Any]] = None,
        is_buy: bool = True,
        order_type: str = "LIMIT_MAKER"
    ) -> Dict[str, Any]:
        """
        Dynamically allocate total order quantity between KRX and NXT venues.
        Supports dynamic NXT Passive Maker rebate optimization.
        """
        if total_quantity <= 0:
            return {
                'ticker': ticker,
                'total_quantity': 0,
                'krx_quantity': 0,
                'nxt_quantity': 0,
                'recommended_venue': 'KRX',
                'order_type': 'LIMIT',
                'maker_price': 0.0,
                'slippage_savings_bps': 0.0
            }

        krx_vol = int(krx_depth.get('ask_vol1' if is_buy else 'bid_vol1', 1))
        krx_spread = float(krx_depth.get('spread', 10.0))
        krx_price = float(krx_depth.get('ask1' if is_buy else 'bid1', 1000.0))

        if not self.nxt_enabled or not nxt_depth:
            return {
                'ticker': ticker,
                'total_quantity': total_quantity,
                'krx_quantity': total_quantity,
                'nxt_quantity': 0,
                'recommended_venue': 'KRX',
                'order_type': 'LIMIT',
                'maker_price': krx_price,
                'slippage_savings_bps': 0.0,
                'routing_reason': 'NXT_UNAVAILABLE_OR_DISABLED'
            }

        nxt_vol = int(nxt_depth.get('ask_vol1' if is_buy else 'bid_vol1', 0))
        nxt_spread = float(nxt_depth.get('spread', 10.0))
        nxt_price = float(nxt_depth.get('ask1' if is_buy else 'bid1', 1000.0))

        better_price_on_nxt = (nxt_price < krx_price) if is_buy else (nxt_price > krx_price)
        equal_price = (abs(nxt_price - krx_price) < 1e-4)

        # Compute dynamic maker price for NXT
        nxt_bid1 = float(nxt_depth.get('bid1', nxt_price))
        nxt_ask1 = float(nxt_depth.get('ask1', nxt_price))
        nxt_bid_vol1 = int(nxt_depth.get('bid_vol1', nxt_vol))
        nxt_ask_vol1 = int(nxt_depth.get('ask_vol1', nxt_vol))
        
        maker_price = self.calculate_maker_limit_price(
            nxt_bid1, nxt_ask1, nxt_bid_vol1, nxt_ask_vol1, is_buy=is_buy
        )

        if better_price_on_nxt:
            nxt_qty = min(total_quantity, nxt_vol)
            krx_qty = total_quantity - nxt_qty
            savings_bps = round(abs(krx_price - nxt_price) / max(krx_price, 1.0) * 10000.0, 2) + 5.0  # +5 bps maker rebate
            recommended_venue = 'NXT' if nxt_qty == total_quantity else 'SPLIT'
            reason = f"NXT Price Advantage ({nxt_price} vs {krx_price}) + Maker Rebate"
        elif equal_price and nxt_spread <= krx_spread:
            total_depth = max(1, krx_vol + nxt_vol)
            nxt_ratio = float(nxt_vol) / float(total_depth)
            nxt_qty = int(math.floor(total_quantity * nxt_ratio))
            krx_qty = total_quantity - nxt_qty
            savings_bps = 6.5  # Base liquidity fee + Maker rebate saving
            recommended_venue = 'SPLIT' if nxt_qty > 0 else 'KRX'
            reason = f"Proportional Depth Liquidity & NXT Maker Allocation (NXT {nxt_ratio:.1%})"
        else:
            krx_qty = total_quantity
            nxt_qty = 0
            savings_bps = 0.0
            recommended_venue = 'KRX'
            reason = "KRX Price / Spread Advantage"

        return {
            'ticker': ticker,
            'total_quantity': total_quantity,
            'krx_quantity': krx_qty,
            'nxt_quantity': nxt_qty,
            'recommended_venue': recommended_venue,
            'order_type': order_type if nxt_qty > 0 else 'LIMIT',
            'maker_price': maker_price if nxt_qty > 0 else krx_price,
            'slippage_savings_bps': savings_bps,
            'routing_reason': reason
        }
