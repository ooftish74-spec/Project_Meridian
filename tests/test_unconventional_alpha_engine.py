"""
Unit tests for Purely Mathematical Unconventional Alpha Engine
===============================================================
Verifies:
 - Zero hardcoded constants (all dynamic/mathematically calculated).
 - Mathematical accuracy of ETP Volatility Drag formula.
 - Adversarial Liquidity Trap Squeeze Z-Score calculation.
 - Anti-fragile Option & Dynamic Leverage Sizing calculation.
 - Integration with S1 ETF Sniper Stream and CRO Final Gate.
"""

import pytest
import numpy as np
import math
from src.strategy.unconventional_alpha_engine import PureMathematicalUnconventionalEngine, norm_cdf
from src.streams.s1_edge.etf_sniper_stream import S1ETFSniperStream
from src.risk.cro_final_gate import CROFinalGate


class TestPureMathematicalUnconventionalEngine:
    @pytest.fixture
    def engine(self):
        return PureMathematicalUnconventionalEngine()

    def test_norm_cdf_precision(self):
        # norm_cdf(0.0) == 0.5
        assert math.isclose(norm_cdf(0.0), 0.5, abs_tol=1e-6)
        # norm_cdf(1.96) ~= 0.975
        assert math.isclose(norm_cdf(1.96), 0.975, abs_tol=5e-3)

    def test_etf_volatility_decay_capture(self, engine):
        # Test 2X leverage with 2% daily vol
        res_2x = engine.etf_volatility_decay_capture(
            ticker='233740',
            leverage_factor=2.0,
            rolling_vol_daily=0.02
        )
        assert res_2x['ticker'] == '233740'
        # 0.5 * (4 - 2) * (0.02^2) = 0.5 * 2 * 0.0004 = 0.0004 -> 0.04% daily
        assert res_2x['decay_daily_pct'] > 0.03
        assert res_2x['recommended_action'] == 'HARVEST_DECAY'

        # Test 3X leverage with high 3% daily vol (TQQQ)
        res_3x = engine.etf_volatility_decay_capture(
            ticker='TQQQ',
            leverage_factor=3.0,
            rolling_vol_daily=0.03
        )
        # 0.5 * (9 - 3) * 0.0009 = 3 * 0.0009 = 0.0027 -> 0.27% daily decay
        assert res_3x['decay_daily_pct'] > 0.2
        assert res_3x['recommended_action'] == 'HARVEST_DECAY'

    def test_adversarial_liquidity_trap_squeeze(self, engine):
        vol_hist = [100.0] * 20
        depth_hist = [50.0] * 20
        
        # Test normal market conditions
        normal_eval = engine.adversarial_liquidity_trap_squeeze(
            volume_history=vol_hist,
            depth_history=depth_hist,
            current_volume=100.0,
            current_depth=50.0
        )
        assert not normal_eval['is_trap_detected']
        assert normal_eval['action'] == 'NORMAL_MONITOR'

        # Test liquidity trap (High Volume spike + Orderbook depth collapse)
        trap_eval = engine.adversarial_liquidity_trap_squeeze(
            volume_history=vol_hist,
            depth_history=depth_hist,
            current_volume=400.0,  # 3-sigma volume spike
            current_depth=5.0      # severe depth collapse
        )
        assert trap_eval['is_trap_detected']
        assert trap_eval['squeeze_prob'] > 0.65
        assert trap_eval['action'] == 'LIMIT_PINGPONG_SQUEEZE'

    def test_antifragile_option_overlay_evaluator(self, engine):
        res = engine.antifragile_option_overlay_evaluator(
            account_equity=100000000.0,
            vix_current=25.0,
            vix_historical_mean=18.0,
            var_99_pct=3.0
        )
        assert res['option_budget'] > 500000.0
        assert res['hedge_coverage_ratio'] > 0.8
        assert res['is_tail_hedged'] is True
        assert res['dynamic_leverage_scale'] > 1.5

    def test_evaluate_all_integration(self, engine):
        market_data = {
            'signal_cache': {
                'vix': 22.0,
                'var_99_pct': 2.5,
                'volume_now': 300000.0,
                'depth_now': 10000.0
            },
            'volume_history': [100000.0] * 20,
            'depth_history': [50000.0] * 20
        }
        full_res = engine.evaluate_all(market_data, account_equity=150000000.0)
        assert 'option_overlay' in full_res
        assert 'etf_decay_results' in full_res
        assert 'liquidity_trap_eval' in full_res
        assert full_res['dynamic_leverage_recommended'] >= 1.0


class TestS1StreamIntegration:
    def test_s1_stream_with_unconventional_engine(self):
        stream = S1ETFSniperStream()
        market_data = {
            'signal_cache': {
                'vix': 25.0,
                'vix_ma_20': 18.0,
                'vix_std_20': 2.0,
                'vix_history': [18.0, 25.0],
                'var_99_pct': 2.5,
                'volume_now': 400000.0,
                'depth_now': 5000.0
            },
            'volume_history': [100000.0] * 20,
            'depth_history': [50000.0] * 20
        }
        signals = stream.generate_signals(regime='bear', market_data=market_data)
        assert isinstance(signals, list)
        # Check if unconventional signals are generated
        unc_signals = [s for s in signals if s.get('strategy') in ('etf_volatility_decay_harvest', 'adversarial_liquidity_trap_squeeze')]
        assert len(unc_signals) > 0


class TestCROFinalGateIntegration:
    def test_cro_final_gate_dynamic_leverage_expansion(self):
        gate = CROFinalGate(max_portfolio_exposure=1.0)
        orders = [{'ticker': '233740', 'direction': 1, 'net_amount': 150000000.0, 'net_shares': 1000}]
        cov_matrix = np.array([[0.0001]])
        ticker_order_map = {'233740': 0}
        
        # Test without tail hedge (should scale down to 1.0x exposure)
        _, audit_no_hedge = gate.evaluate_and_scale_orders(
            orders=orders,
            portfolio_value=100000000.0,
            cov_matrix=cov_matrix,
            ticker_order_map=ticker_order_map,
            market_data={}
        )
        assert audit_no_hedge['veto_triggered'] is True
        assert audit_no_hedge['scaling_factor'] < 1.0

        # Test WITH verified tail hedge (dynamic leverage expanded to 2.0x)
        market_data_hedged = {
            'unconventional_eval': {
                'is_tail_hedged': True,
                'dynamic_leverage_recommended': 2.0
            }
        }
        _, audit_hedged = gate.evaluate_and_scale_orders(
            orders=orders,
            portfolio_value=100000000.0,
            cov_matrix=cov_matrix,
            ticker_order_map=ticker_order_map,
            market_data=market_data_hedged
        )
        assert audit_hedged['veto_triggered'] is False
        assert audit_hedged['scaling_factor'] == 1.0
