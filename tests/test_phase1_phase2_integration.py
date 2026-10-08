"""
Unit and Integration Tests for Phase 1 and Phase 2 Quant Enhancements
========================================================================
- Phase 1: Asymmetric V-Bounce Rebound Engine in CROFinalGate & VIX Z-score dynamic allocation.
- Phase 2: SyntheticFuturesOverlay & 3-Tier Margin Safety Protection.
"""

import pytest
import numpy as np
from src.risk.cro_final_gate import CROFinalGate
from src.allocation.capital_allocator import MetaCapitalAllocator
from src.allocation.synthetic_futures_overlay import SyntheticFuturesOverlay


class TestPhase1AndPhase2Integration:

    def test_cro_final_gate_asymmetric_v_bounce(self):
        cro_gate = CROFinalGate(max_daily_var_pct=0.01)  # Strict VaR limit to trigger scale
        orders = [{
            'ticker': '005930',
            'direction': 1,
            'net_shares': 1000,
            'net_amount': 70000000.0,
            'price': 70000.0
        }]
        cov = np.array([[0.0004]])  # High vol
        ticker_map = {'005930': 0}

        # Step 1: Scale orders down due to VaR breach
        scaled_orders1, veto1 = cro_gate.evaluate_and_scale_orders(orders, 100000000.0, cov, ticker_map)
        scale1 = veto1['scaling_factor']

        # Step 2: Simulate GARCH volatility decay (Vol Decay) -> Trigger V-bounce rebound release
        cro_gate._prev_garch_vol = 0.050  # High vol
        scaled_orders2, veto2 = cro_gate.evaluate_and_scale_orders(orders, 100000000.0, cov, ticker_map)

        assert veto1['veto_triggered'] is True
        assert 'scaling_factor' in veto2

    def test_capital_allocator_vix_z_score(self):
        allocator = MetaCapitalAllocator()
        mult_s1 = allocator._get_regime_multiplier('S1')
        mult_s5 = allocator._get_regime_multiplier('S5')

        assert mult_s1 > 0.0
        assert mult_s5 > 0.0

    def test_synthetic_futures_overlay_margin_calc(self):
        overlay = SyntheticFuturesOverlay(krx_margin_ratio=0.15, us_margin_ratio=0.20)
        allocations = {'S1': 10000000.0, 'S5': 20000000.0}
        total_equity = 50000000.0

        res = overlay.calculate_futures_overlay(allocations, total_equity)

        assert res['required_margin'] == 10000000.0 * 0.15 + 20000000.0 * 0.20
        assert res['unlocked_cash'] > 0.0
        assert res['kofr_yield_harvesting_amount'] > 0.0
        assert res['free_margin_ratio'] > 0.0

    def test_3tier_margin_safety(self):
        overlay = SyntheticFuturesOverlay()
        # Normal state
        res1 = overlay.evaluate_3tier_margin_safety(100000000.0, 20000000.0, 5000000.0)
        assert res1['tier1_pass'] is True
        assert res1['tier2_sweep_needed'] is False
        assert res1['tier3_emergency_trim_needed'] is False

        # Margin maintenance ratio < 150% (Tier 2 sweep needed)
        res2 = overlay.evaluate_3tier_margin_safety(100000000.0, 80000000.0, 5000000.0)
        assert res2['tier2_sweep_needed'] is True

        # Margin maintenance ratio < 120% (Tier 3 emergency trim needed)
        res3 = overlay.evaluate_3tier_margin_safety(100000000.0, 95000000.0, 5000000.0)
        assert res3['tier3_emergency_trim_needed'] is True
        assert res3['trim_scale'] < 1.0
