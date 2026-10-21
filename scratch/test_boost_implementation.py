#!/usr/bin/env python3
"""
scratch/test_boost_implementation.py
======================================
Comprehensive Test Suite for 50%+ Post-Tax CAGR Boost Pillars:
1. Component 1: Dynamic Micro Futures Overlay Manager
2. Component 2: Offline Corporate Tax Optimization Ledger
3. Component 3: Dynamic NXT Maker Order Router & Timeout Engine
"""

import os
import sys
import unittest
from pathlib import Path

# Add Project_Meridian root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.execution.futures_overlay_manager import DynamicFuturesOverlayManager
from src.execution.smart_order_router import DynamicSmartOrderRouter
from scripts.export_corporate_ledger import export_ledgers, calculate_corporate_tax_bracket

class TestBoostImplementationPillars(unittest.TestCase):

    def setUp(self):
        self.futures_mgr = DynamicFuturesOverlayManager(alpha_max=0.20, ois_threshold=80.0, bull_threshold=0.85)
        self.sor = DynamicSmartOrderRouter(nxt_enabled=True)

    def test_component1_futures_overlay(self):
        """Test Dynamic Futures Overlay Position Sizing & Circuit Breaker."""
        nav = 20_000_000.0
        futures_price = 350.0  # KOSPI 200 micro futures index
        micro_multiplier = 10000.0  # Micro contract multiplier
        margin_ratio = 0.15

        # Case A: Low OIS score (OIS = 70 < 80) -> Should NOT trigger overlay
        res_a = self.futures_mgr.calculate_overlay_position(nav, 70.0, 0.90, futures_price, micro_multiplier, margin_ratio)
        self.assertFalse(res_a["active"])
        self.assertEqual(res_a["n_contracts"], 0)

        # Case B: High OIS score (OIS = 90 > 80) and High Bull Prob (0.90 > 0.85) -> Active overlay
        res_b = self.futures_mgr.calculate_overlay_position(nav, 90.0, 0.90, futures_price, micro_multiplier, margin_ratio)
        self.assertTrue(res_b["active"])
        self.assertGreater(res_b["n_contracts"], 0)
        self.assertGreater(res_b["margin_required"], 0.0)

        # Case C: Circuit Breaker Test (-2.0% drawdown > -1.5% dynamic stop)
        triggered, drawdown, reason = self.futures_mgr.check_intraday_circuit_breaker(
            entry_price=350.0, current_price=342.0, ewma_volatility=0.01
        )
        self.assertTrue(triggered)
        self.assertIn("CIRCUIT BREAKER TRIGGERED", reason)

    def test_component2_corporate_tax_calc(self):
        """Test Offline Corporate Tax Bracket Calculations."""
        # Tier 1 Income (100m KRW) -> 9.9% tax = 9.9m KRW
        tax_t1 = calculate_corporate_tax_bracket(100_000_000)
        self.assertAlmostEqual(tax_t1, 9_900_000.0, places=2)

        # Tier 2 Income (300m KRW) -> 200m * 0.099 + 100m * 0.209 = 19.8m + 20.9m = 40.7m KRW
        tax_t2 = calculate_corporate_tax_bracket(300_000_000)
        self.assertAlmostEqual(tax_t2, 40_700_000.0, places=2)

        # Export Ledgers verification
        export_ledgers()
        results_dir = PROJECT_ROOT / "results"
        self.assertTrue(any(results_dir.glob("Daily_Corporate_Ledger_*.csv")))
        self.assertTrue(any(results_dir.glob("Corporate_Tax_Optimization_Report_*.md")))

    def test_component3_smart_order_router_maker(self):
        """Test Dynamic NXT Maker Limit Order Price & Timeout Engine."""
        # Test Maker Limit Price calculation
        maker_price = self.sor.calculate_maker_limit_price(
            bid1=1000.0, ask1=1010.0, bid_vol1=500, ask_vol1=500, is_buy=True
        )
        self.assertGreaterEqual(maker_price, 1000.0)
        self.assertLessEqual(maker_price, 1010.0)

        # Test Timeout Switching (400ms age > 100ms dynamic timeout)
        switched, reason = self.sor.evaluate_maker_switch_to_taker(
            order_age_ms=400.0, tick_rate_hz=10.0, fill_pct=0.0
        )
        self.assertTrue(switched)
        self.assertIn("DYNAMIC TIMEOUT TRIGGERED", reason)

        # Test Route Allocation with NXT depth
        krx_depth = {'ask1': 1000.0, 'bid1': 995.0, 'ask_vol1': 100, 'bid_vol1': 100, 'spread': 5.0}
        nxt_depth = {'ask1': 998.0, 'bid1': 994.0, 'ask_vol1': 50, 'bid_vol1': 50, 'spread': 4.0}
        
        route = self.sor.route_order(
            ticker="069500", total_quantity=100, krx_depth=krx_depth, nxt_depth=nxt_depth, is_buy=True
        )
        self.assertIn(route['recommended_venue'], ['NXT', 'SPLIT'])
        self.assertGreater(route['nxt_quantity'], 0)
        self.assertGreater(route['slippage_savings_bps'], 0.0)

if __name__ == "__main__":
    unittest.main()
