"""
Unit and Integration Tests for Elevated ~50% Post-Tax CAGR Architecture Package
================================================================================
- DynamicMicroFuturesLeverage in SyntheticFuturesOverlay
- AsymmetricEventSqueezeSniper
- TaxLossHarvestingEngine
- CrossVenueSpreadNetting
"""

import pytest
import numpy as np
from src.allocation.synthetic_futures_overlay import SyntheticFuturesOverlay
from src.strategy.tactic_e_event_squeeze import AsymmetricEventSqueezeSniper
from src.tax.tax_loss_harvesting_engine import TaxLossHarvestingEngine
from src.execution.cross_venue_netting import CrossVenueSpreadNetting


class TestElevated50PctPipelineIntegration:

    def test_dynamic_micro_futures_leverage(self):
        overlay = SyntheticFuturesOverlay()

        # Bull regime (VIX Z = -1.2) -> Dynamic leverage ~ 1.8x
        alloc = {'S5': 10000000.0, 'S6': 5000000.0}
        res_bull = overlay.calculate_futures_overlay(alloc, 20000000.0, vix_z_score=-1.2)

        assert res_bull['applied_leverage'] > 1.5
        assert 'required_margin' in res_bull

        # Bear regime (VIX Z = +1.5) -> Leverage = 1.0x
        res_bear = overlay.calculate_futures_overlay(alloc, 20000000.0, vix_z_score=1.5)
        assert res_bear['applied_leverage'] == 1.0

    def test_tactic_e_asymmetric_event_squeeze_sniper(self):
        sniper = AsymmetricEventSqueezeSniper()

        # positive 15m CVD -> Long
        res_long = sniper.evaluate_earnings_event_trade('NVDA', cvd_15m=5000.0, iv_current=1.2, iv_historical_mean=0.5, is_negative_gex_zone=False, account_equity=16762231.0)
        assert res_long['action'] == 'LONG'
        assert res_long['allocated_capital'] > 0.0

        # negative 15m CVD -> Short/Inverse (Sell-the-news defense)
        res_short = sniper.evaluate_earnings_event_trade('NVDA', cvd_15m=-3000.0, iv_current=1.2, iv_historical_mean=0.5, is_negative_gex_zone=False, account_equity=16762231.0)
        assert res_short['action'] == 'SHORT_INVERSE'

        # Negative GEX Zone -> Veto skip
        res_veto = sniper.evaluate_earnings_event_trade('NVDA', cvd_15m=5000.0, iv_current=1.2, iv_historical_mean=0.5, is_negative_gex_zone=True, account_equity=16762231.0)
        assert res_veto['action'] == 'VETO_SKIP'

    def test_tax_loss_harvesting_engine(self):
        tax_engine = TaxLossHarvestingEngine(harvest_month=12)
        positions = [
            {'ticker': '005930', 'unrealized_pnl': -1500000.0, 'current_val': 10000000.0},
            {'ticker': '000660', 'unrealized_pnl': 500000.0, 'current_val': 5000000.0}
        ]

        res = tax_engine.evaluate_tax_loss_harvesting(positions, current_year_realized_profit=20000000.0, current_month=12)

        assert res['harvest_recommended'] is True
        assert res['total_harvestable_loss'] == 1500000.0
        assert res['estimated_tax_savings'] > 0.0

    def test_cross_venue_spread_netting(self):
        netting_engine = CrossVenueSpreadNetting()
        raw_orders = [
            {'ticker': '005930', 'direction': 1, 'amount': 5000000.0},
            {'ticker': '005930', 'direction': -1, 'amount': 2000000.0},
            {'ticker': 'NVDA', 'direction': 1, 'amount': 3000000.0}
        ]

        res = netting_engine.optimize_order_batch_netting(raw_orders)

        assert len(res['netted_orders']) == 2
        assert res['saved_friction_krw'] > 0.0
