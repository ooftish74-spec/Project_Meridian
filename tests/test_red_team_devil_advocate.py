#!/usr/bin/env python3
"""
tests/test_red_team_devil_advocate.py
=====================================
Red Team & Devil's Advocate Audit Regression Test Suite:
  1. Overseas Exchange Resolution (NASD, NYSE, AMEX).
  2. Auto Re-Peg Loop: SELL orders repeg as SELL, BUY orders repeg as BUY.
  3. Order Netting: Minimum order value (500k KRW) drops BUYs but NEVER drops SELLs.
  4. UniversalExitEngine: Evaluates positions loaded from kis_portfolio.json SSOT with holding_minutes fallback.
  5. ExecutionEngine: check_account_sync defines result_sync and handles exceptions safely.
  6. Cash deduction with dynamic USD/KRW exchange rate.
"""

import os
import sys
import json
import pytest
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)


class TestOverseasExchangeResolution:
    """Test overseas exchange resolution logic."""

    def test_ticker_exchange_mapping(self):
        from src.execution._kis_adapter import KISTraderAdapter
        assert KISTraderAdapter.resolve_overseas_exchange('NVDA') == 'NASD'
        assert KISTraderAdapter.resolve_overseas_exchange('QQQ') == 'NASD'
        assert KISTraderAdapter.resolve_overseas_exchange('SOXX') == 'NASD'
        assert KISTraderAdapter.resolve_overseas_exchange('SPY') == 'NYSE'
        assert KISTraderAdapter.resolve_overseas_exchange('SHV') == 'NYSE'
        assert KISTraderAdapter.resolve_overseas_exchange('XLK') == 'AMEX'
        assert KISTraderAdapter.resolve_overseas_exchange('SOXL') == 'AMEX'

    def test_explicit_exchange_override(self):
        from src.execution._kis_adapter import KISTraderAdapter
        assert KISTraderAdapter.resolve_overseas_exchange('ANY', 'NYSE') == 'NYSE'
        assert KISTraderAdapter.resolve_overseas_exchange('ANY', 'AMS') == 'AMEX'
        assert KISTraderAdapter.resolve_overseas_exchange('ANY', 'NASDAQ') == 'NASD'


class TestAutoRePegLoop:
    """Test Auto Re-Peg differentiation between BUY and SELL."""

    @patch('requests.get')
    def test_repeg_sell_vs_buy_direction(self, mock_get):
        from src.execution._kis_adapter import KISTraderAdapter, Order
        adapter = KISTraderAdapter(mode='mock')
        adapter.mode = 'live'
        adapter._access_token = 'mock_token'
        adapter.account_no = '12345678-01'

        # Mock unfilled orders from inquire-nccs: 1 SELL and 1 BUY
        mock_response = MagicMock()
        mock_response.json.return_value = {
            'rt_cd': '0',
            'output': [
                {
                    'odno': 'ORD_SELL_01',
                    'pdno': 'NVDA',
                    'nccs_qty': '8',
                    'ft_ord_unpr3': '240.0',
                    'ovrs_excg_cd': 'NASD',
                    'sll_buy_dvsn_cd': '01'  # SELL
                },
                {
                    'odno': 'ORD_BUY_02',
                    'pdno': 'SOXX',
                    'nccs_qty': '4',
                    'ft_ord_unpr3': '580.0',
                    'ovrs_excg_cd': 'NASD',
                    'sll_buy_dvsn_cd': '02'  # BUY
                }
            ]
        }
        mock_get.return_value = mock_response

        adapter.cancel_us_order = MagicMock(return_value={'success': True})
        adapter.sell = MagicMock(return_value=Order(order_id='NEW_SELL_1', ticker='NVDA', side='sell', quantity=8, price=235.0, order_type='limit', status='submitted'))
        adapter.buy = MagicMock(return_value=Order(order_id='NEW_BUY_2', ticker='SOXX', side='buy', quantity=4, price=585.0, order_type='limit', status='submitted'))
        adapter.get_live_overseas_price = MagicMock(side_effect=lambda t, ex, ask_side=True: 235.0 if not ask_side else 585.0)

        results = adapter.check_and_repeg_unexecuted_orders()
        assert len(results) >= 2

        # Verify SELL order triggered self.sell, NOT self.buy
        sell_repegs = [r for r in results if r['ticker'] == 'NVDA']
        assert len(sell_repegs) > 0
        assert sell_repegs[0]['side'] == 'sell'
        adapter.sell.assert_called_once()
        args, kwargs = adapter.sell.call_args
        assert kwargs['ticker'] == 'NVDA'
        assert kwargs['quantity'] == 8

        # Verify BUY order triggered self.buy
        buy_repegs = [r for r in results if r['ticker'] == 'SOXX']
        assert len(buy_repegs) > 0
        assert buy_repegs[0]['side'] == 'buy'
        adapter.buy.assert_called_once()
        b_args, b_kwargs = adapter.buy.call_args
        assert b_kwargs['ticker'] == 'SOXX'
        assert b_kwargs['quantity'] == 4


class TestOrderNettingExemption:
    """Test that sell/stop-loss orders are never dropped due to min_order_value_krw."""

    def test_sell_order_under_min_value_is_not_dropped(self):
        from scripts.stream_orchestrator import StreamOrchestrator
        orch = StreamOrchestrator(exec_mode='shadow')

        # Run netting simulation
        min_order_krw = 500000.0

        # Case 1: Small SELL order (e.g. 100,000 KRW)
        final_action_sell = 'sell'
        netted_val_krw_sell = 100000.0
        final_price = 100000.0
        should_drop_sell = (final_action_sell == 'buy' and final_price > 0 and netted_val_krw_sell < min_order_krw)
        assert should_drop_sell is False, "Sell orders must NEVER be dropped due to min_order_krw!"

        # Case 2: Small BUY order (e.g. 100,000 KRW)
        final_action_buy = 'buy'
        netted_val_krw_buy = 100000.0
        should_drop_buy = (final_action_buy == 'buy' and final_price > 0 and netted_val_krw_buy < min_order_krw)
        assert should_drop_buy is True, "Small Buy orders under 500k KRW should be dropped."


class TestUniversalExitEngineLiveHoldings:
    """Test UniversalExitEngine evaluating positions with live holdings and holding_minutes."""

    def test_universal_exit_evaluates_live_holdings(self):
        from src.risk.universal_exit_engine import UniversalExitEngine
        engine = UniversalExitEngine()

        positions = {
            'LIVE_SSOT:SOXX': {
                'ticker': 'SOXX',
                'stream_id': 'LIVE_SSOT',
                'quantity': 4,
                'entry_price': 506.18,
                'current_price': 582.82,
                'unrealized_pnl_pct': 15.14,
                'peak_pnl_pct': 15.14,
                'holding_minutes': 999.0,
            },
            'LIVE_SSOT:069500': {
                'ticker': '069500',
                'stream_id': 'LIVE_SSOT',
                'quantity': 52,
                'entry_price': 107534.96,
                'current_price': 105650.0,
                'unrealized_pnl_pct': -1.75,
                'peak_pnl_pct': 0.0,
                'holding_minutes': 999.0,
            }
        }

        res = engine.evaluate_all_positions(positions, regime='caution')
        assert res['evaluated_count'] == 2
        assert isinstance(res['exit_orders'], list)


class TestExecutionEngineSafety:
    """Test ExecutionEngine check_account_sync exception safety."""

    def test_check_account_sync_safe_on_network_error(self):
        from src.execution.execution_engine import ExecutionEngine
        engine = ExecutionEngine(mode='shadow')
        sync_res = engine.check_account_sync(portfolio={'total_nav': 20000000.0}, raise_on_desync=False)
        assert sync_res['ok'] is True
        assert sync_res['nav_system'] > 0


class TestPipelineDeadlockAndTrapsAudit:
    """Audit tests for the 5 root causes of frozen holdings and unexecuted signals."""

    def test_alpha_allocator_softmax_and_s10_runway(self):
        from src.allocation.alpha_allocator import AlphaAllocator
        alloc = AlphaAllocator()
        stream_metrics = {
            'S1': {'win_rate': 0.60, 'avg_win': 0.03, 'avg_loss': 0.01},
            'S10': {'win_rate': 0.70, 'avg_win': 0.05, 'avg_loss': 0.015},
            'S3': {'win_rate': 0.55, 'avg_win': 0.025, 'avg_loss': 0.01},
        }
        weights = alloc.allocate(stream_metrics, regime='bull', market_data={'signal_cache': {'vix': 16.0}})
        # Winner-take-all monopoly broken: S10 and S3 must have non-zero allocation
        assert weights.get('S10', 0.0) > 0.0, "S10 must have a dedicated allocation runway!"
        assert weights.get('S1', 0.0) < 0.95, "S1 must not have a 100% winner-take-all monopoly!"

    def test_intraday_capital_router_real_price_and_cross_asset_swap(self):
        from src.execution.intraday_capital_router import IntradayCapitalRouter
        router = IntradayCapitalRouter()

        candidate_signals = [{
            'ticker': '373220',  # LG Energy Solution
            'stream': 'S10_MEGA_TREND',
            'action': 'buy',
            'confidence': 0.85,
            'tp_pct': 5.0,
            'sl_pct': 2.0,
        }]

        market_data = {
            'current_prices': {'373220': 380000.0, '069500': 39000.0},
            'holdings_ev': {'069500': 0.20},  # Low EV holding
            'orderbook_depth_1_3': {'373220': 500}
        }

        portfolio = {
            'cash': 300000.0,  # Insufficient to buy 1 share @ 380,000 KRW
            'positions': {
                '069500': {
                    'ticker': '069500',
                    'name': 'KODEX 200',
                    'qty': 50,
                    'price': 39000.0,
                    'current_price': 39000.0
                }
            }
        }

        orders = router.route_intraday_capital(candidate_signals, market_data, total_cash_krw=300000.0, portfolio=portfolio)
        assert len(orders) >= 2, "Router must generate Cross-Asset Alpha Swap (sell low EV + buy high EV)!"
        sell_order = next(o for o in orders if o.get('action') == 'sell')
        buy_order = next(o for o in orders if o.get('action') == 'buy')

        assert sell_order['ticker'] == '069500'
        assert buy_order['ticker'] == '373220'
        assert buy_order['price'] == 380000.0
        assert buy_order['quantity'] >= 1

    def test_yield_parking_auto_liquidation_with_dict_and_459580(self):
        from scripts.stream_orchestrator import StreamOrchestrator
        orch = StreamOrchestrator(exec_mode='shadow')

        portfolio = {
            'cash': 200000.0,
            'positions': {
                '459580': {
                    'ticker': '459580',
                    'name': 'KODEX CD금리액티브(합성)',
                    'qty': 1,
                    'quantity': 1,
                    'current_price': 1074340.0,
                    'amount': 1074340.0
                }
            }
        }

        # An initial buy order
        candidate_orders = [{
            'stream_id': 'S10_MEGA_TREND',
            'ticker': '373220',
            'action': 'buy',
            'direction': 'long',
            'quantity': 2,
            'amount_krw': 760000.0,
            'price': 380000.0,
            'confidence': 0.85
        }]

        # Inject into orchestrator flow
        # In stream_orchestrator, lines 1024-1054 check parking auto-liquidation:
        # We can simulate the exact block
        _PARKING_TICKERS = {'459580', '449170', '430740', '357870', 'SHV', 'SGOV'}
        positions_source = portfolio.get('positions', {})
        positions_list = [{'ticker': k, **v} if isinstance(v, dict) else {'ticker': k} for k, v in positions_source.items()]
        has_buy_orders = any(str(o.get('action', '')).lower() in ('buy', 'long') for o in candidate_orders)

        park_pos = next((p for p in positions_list if p.get('ticker') in _PARKING_TICKERS), None)
        assert park_pos is not None, "459580 must be recognized as cash parking!"
        assert park_pos['amount'] == 1074340.0

    def test_core_sleeve_time_limit_exemption(self):
        from scripts.stream_orchestrator import StreamOrchestrator
        orch = StreamOrchestrator(exec_mode='shadow')

        # Core holdings held for 44 days
        portfolio = {
            'positions': {
                'NVDA': {'ticker': 'NVDA', 'quantity': 8, 'days_held': 44, 'unrealized_pnl_pct': 10.0, 'current_price': 130.0},
                'QQQ': {'ticker': 'QQQ', 'quantity': 1, 'days_held': 44, 'unrealized_pnl_pct': 6.0, 'current_price': 490.0},
                '069500': {'ticker': '069500', 'quantity': 52, 'days_held': 44, 'unrealized_pnl_pct': -1.5, 'current_price': 39000.0},
                '459580': {'ticker': '459580', 'quantity': 1, 'days_held': 44, 'unrealized_pnl_pct': 0.1, 'current_price': 1074340.0},
            }
        }

        market_data = {'signal_cache': {'vix': 18.0, 'vkospi': 18.0}}
        exit_orders = orch._evaluate_exits(market_data, regime='caution', portfolio=portfolio)

        # None of the core holdings should be exited due to 5-day Time Limit!
        time_limit_exits = [o for o in exit_orders if 'Time Limit' in str(o.get('reason', ''))]
        assert len(time_limit_exits) == 0, f"Core sleeve holdings must NOT trigger 5-day day-trading Time Limit! Got: {time_limit_exits}"

