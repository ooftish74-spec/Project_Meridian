"""Unit Tests for Intraday Capital Router & Time-Decay Auto Exit Engines (Project Meridian V3.5)"""

import pytest
from src.execution.intraday_capital_router import IntradayCapitalRouter
from src.risk.time_decay_exit import TimeDecayExitEvaluator


class TestIntradayCapitalRouter:
    """Test suite for IntradayCapitalRouter."""

    def setup_method(self):
        self.router = IntradayCapitalRouter(max_depth_pct=0.15, default_timeout_min=3)

    def test_compute_ev_adj(self):
        # High WinRate, Low Target (Scalping)
        ev_scalp = self.router.compute_ev_adj(win_rate=0.75, target_return_pct=1.0, stop_loss_pct=0.8, intraday_atr_pct=1.2)
        # Mid WinRate, High Target (Trend)
        ev_trend = self.router.compute_ev_adj(win_rate=0.55, target_return_pct=4.0, stop_loss_pct=1.5, intraday_atr_pct=1.2)

        assert ev_scalp > 0
        assert ev_trend > 0
        assert isinstance(ev_scalp, float)

    def test_rank_signals_by_ev_adj(self):
        signals = [
            {'ticker': '069500', 'win_rate': 0.50, 'tp_pct': 1.0, 'sl_pct': 1.0, 'stream': 'S11'},
            {'ticker': '005930', 'win_rate': 0.80, 'tp_pct': 2.0, 'sl_pct': 0.5, 'stream': 'S1_TACTIC_D'},
        ]
        market_data = {'atr_pct': 1.0}
        ranked = self.router.rank_signals_by_ev_adj(signals, market_data)

        assert len(ranked) == 2
        # Highest EV_adj should be first (005930 with 80% win rate)
        assert ranked[0]['ticker'] == '005930'
        assert ranked[0]['ev_adj'] > ranked[1]['ev_adj']

    def test_market_depth_capacity_cap(self):
        # Target 100 shares, Depth 1-3 total 200 shares -> 15% cap = 30 shares
        capped_qty = self.router.check_market_depth_capacity('069500', target_qty=100, orderbook_depth_1_3_qty=200)
        assert capped_qty == 30

        # Depth missing -> returns target_qty
        assert self.router.check_market_depth_capacity('069500', target_qty=100, orderbook_depth_1_3_qty=0) == 100

    def test_resolve_signal_conflicts_same_ticker(self):
        signals = [
            {'ticker': '069500', 'side': 'buy', 'stream': 'S1'},
            {'ticker': '069500', 'side': 'sell', 'stream': 'S11'},
        ]
        valid = self.router.resolve_signal_conflicts(signals)
        # Both opposing signals should be cancelled
        assert len(valid) == 0

    def test_resolve_signal_conflicts_inverse_pair(self):
        signals = [
            {'ticker': '069500', 'side': 'buy', 'stream': 'S1'},   # KODEX 200 Long
            {'ticker': '114800', 'side': 'buy', 'stream': 'S11'},  # KODEX Inverse Long
        ]
        valid = self.router.resolve_signal_conflicts(signals)
        # Opposing inverse pair should be cancelled
        assert len(valid) == 0

    def test_resolve_signal_conflicts_semicon_inverse_pair(self):
        signals = [
            {'ticker': '091160', 'side': 'buy', 'stream': 'S1'},   # KODEX 반도체
            {'ticker': '390390', 'side': 'buy', 'stream': 'S0'},  # KODEX 반도체인버스
        ]
        valid = self.router.resolve_signal_conflicts(signals)
        assert len(valid) == 0


    def test_cascading_cash_swap(self):
        freed_cash = 10000000.0  # 10M KRW
        pending_s11 = [
            {'ticker': '005930', 'price': 70000.0, 'target_amount': 5000000.0},
            {'ticker': '000660', 'price': 150000.0, 'target_amount': 5000000.0},
        ]
        swapped = self.router.cascading_cash_swap(freed_cash, pending_s11)
        assert len(swapped) == 2
        assert swapped[0]['quantity'] > 0
        assert swapped[1]['quantity'] > 0

    def test_route_intraday_capital_ofi_z(self):
        signals = [
            {'ticker': '069500', 'price': 100000.0, 'stream': 'S1', 'tactic': 'TACTIC_D'},
            {'ticker': '005930', 'price': 70000.0, 'stream': 'S11'},
        ]
        market_data = {'ofi_z_score': 3.5, 'orderbook_depth_1_3': {'069500': 1000, '005930': 1000}}
        routed = self.router.route_intraday_capital(signals, market_data, total_cash_krw=10000000.0)

        assert len(routed) > 0

    def test_route_intraday_capital_us_asset(self):
        signals = [
            {'ticker': 'SOXX', 'price': 220.0, 'stream': 'S11', 'side': 'buy'},
        ]
        market_data = {'ofi_z_score': 1.0, 'orderbook_depth_1_3': {'SOXX': 5000}}
        routed = self.router.route_intraday_capital(signals, market_data, total_cash_krw=10000000.0)

        assert len(routed) == 1
        assert routed[0]['ticker'] == 'SOXX'
        assert routed[0]['quantity'] > 0

    def test_compute_holdings_ev(self):
        portfolio = {
            'holdings': {
                '069500': {'ticker': '069500', 'name': 'KODEX 200', 'current_price': 105650.0, 'avg_price': 107500.0, 'return_pct': -1.75},
                '091160': {'ticker': '091160', 'name': 'KODEX 반도체', 'current_price': 146000.0, 'avg_price': 127000.0, 'return_pct': 15.0},
            }
        }
        market_data = {
            'signal_cache': {
                'kospi': 6830.0,
                'kospi_ma20': 6850.0,
                'stock_technicals': {
                    '069500': {'rsi_14': 48.0, 'macd_hist': -150.0},
                    '091160': {'rsi_14': 72.0, 'macd_hist': 500.0},
                }
            }
        }
        holdings_ev = self.router.compute_holdings_ev(portfolio, market_data)
        assert '069500' in holdings_ev
        assert '091160' in holdings_ev
        # Weakened KODEX 200 EV should be significantly lower than winning KODEX 반도체 EV
        assert holdings_ev['069500'] < holdings_ev['091160']

    def test_cross_asset_alpha_swap_trigger(self):
        portfolio = {
            'holdings': {
                '069500': {'ticker': '069500', 'name': 'KODEX 200', 'qty': 50, 'current_price': 100000.0, 'avg_price': 102000.0, 'return_pct': -1.96},
            }
        }
        market_data = {
            'holdings_ev': {'069500': 0.20},  # Low EV
            'ticker_atr_pct': {'373220': 2.0},
        }
        # High conviction new signal
        signals = [
            {'ticker': '373220', 'name': 'LG에너지솔루션', 'win_rate': 0.85, 'confidence': 0.85, 'price': 380000.0, 'tp_pct': 5.0, 'sl_pct': 2.0, 'side': 'buy'}
        ]
        # Total cash is 0, so normal cash buy cannot occur
        routed = self.router.route_intraday_capital(signals, market_data, total_cash_krw=0.0, portfolio=portfolio)
        
        # Should generate paired sell + buy orders
        assert len(routed) == 2
        sell_order = routed[0]
        buy_order = routed[1]
        assert sell_order['ticker'] == '069500'
        assert sell_order['action'] == 'sell'
        assert buy_order['ticker'] == '373220'
        assert buy_order['action'] == 'buy'
        assert buy_order['reallocate_from'] == '069500'

    def test_us_holdings_ev_computation(self):
        portfolio = {
            'holdings': {
                'NVDA': {'ticker': 'NVDA', 'name': 'NVIDIA Corp', 'qty': 8, 'current_price': 237.47, 'avg_price': 213.59, 'return_pct': 11.18},
                'QQQ': {'ticker': 'QQQ', 'name': 'Invesco QQQ Trust', 'qty': 1, 'current_price': 757.73, 'avg_price': 706.98, 'return_pct': 7.18},
            }
        }
        market_data = {
            'signal_cache': {
                'qqq_close': 757.73,
                'qqq_ma20': 740.0,
                'stock_technicals': {
                    'NVDA': {'rsi_14': 68.0, 'macd_hist': 2.5},
                    'QQQ': {'rsi_14': 58.0, 'macd_hist': 1.2},
                }
            }
        }
        holdings_ev = self.router.compute_holdings_ev(portfolio, market_data)
        assert 'NVDA' in holdings_ev
        assert 'QQQ' in holdings_ev
        assert holdings_ev['NVDA'] > 0.5
        assert holdings_ev['QQQ'] > 0.5

    def test_us_cross_asset_alpha_swap(self):
        portfolio = {
            'holdings': {
                'XLK': {'ticker': 'XLK', 'name': 'Technology Select Sector SPDR', 'qty': 10, 'current_price': 200.0, 'avg_price': 205.0, 'return_pct': -2.44},
            }
        }
        market_data = {
            'holdings_ev': {'XLK': 0.15},  # Low EV
            'ticker_atr_pct': {'TSLA': 3.0},
        }
        signals = [
            {'ticker': 'TSLA', 'name': 'Tesla Inc', 'win_rate': 0.80, 'confidence': 0.80, 'price': 250.0, 'tp_pct': 6.0, 'sl_pct': 2.0, 'side': 'buy'}
        ]
        # Cash is 0 -> Swaps XLK to buy TSLA
        routed = self.router.route_intraday_capital(signals, market_data, total_cash_krw=0.0, portfolio=portfolio)
        assert len(routed) == 2
        sell_order = routed[0]
        buy_order = routed[1]
        assert sell_order['ticker'] == 'XLK'
        assert sell_order['action'] == 'sell'
        assert sell_order['quantity'] >= 1
        assert buy_order['ticker'] == 'TSLA'
        assert buy_order['action'] == 'buy'
        assert buy_order['quantity'] >= 1
        assert buy_order['reallocate_from'] == 'XLK'



class TestTimeDecayExitEvaluator:
    """Test suite for TimeDecayExitEvaluator."""

    def setup_method(self):
        self.evaluator = TimeDecayExitEvaluator(min_days=3, max_days=5, range_atr_mult=0.5)

    def test_holding_time_under_min_days(self):
        pos = {'ticker': '069500', 'avg_price': 100000.0, 'highest_price': 101000.0, 'lowest_price': 99500.0, 'quantity': 10}
        res = self.evaluator.evaluate_time_decay(pos, current_price=100000.0, atr_val=2000.0, holding_days=2.0)
        assert res['trigger'] is False

    def test_range_bound_trigger_at_3_days(self):
        # ATR = 2000, 0.5 * ATR = 1000. High = 100400, Low = 99800 (Range = 600 <= 1000)
        pos = {'ticker': '069500', 'avg_price': 100000.0, 'highest_price': 100400.0, 'lowest_price': 99800.0, 'quantity': 10}
        res = self.evaluator.evaluate_time_decay(pos, current_price=100100.0, atr_val=2000.0, holding_days=3.5)

        assert res['trigger'] is True
        assert res['scale_out_pct'] == 0.50

    def test_range_bound_trigger_at_5_days(self):
        pos = {'ticker': '069500', 'avg_price': 100000.0, 'highest_price': 100400.0, 'lowest_price': 99800.0, 'quantity': 10}
        res = self.evaluator.evaluate_time_decay(pos, current_price=100100.0, atr_val=2000.0, holding_days=5.2)

        assert res['trigger'] is True
        assert res['scale_out_pct'] == 1.00
