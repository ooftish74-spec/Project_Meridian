"""Unit Tests for S0 Beta Stream V4 Engine (Project Meridian V3.5)"""

import pytest
from src.streams.s0_beta.beta_stream import S0BetaStream


class TestS0BetaStreamV4:
    """Test suite for S0 Beta Stream V4 upgrade."""

    def setup_method(self):
        self.stream = S0BetaStream()

    def test_compute_d_macro(self):
        market_data = {
            'z_skew': 2.0,
            'features': {
                'z_credit': 1.5,
                'z_breadth': 1.0,
                'z_vpin': 0.8
            }
        }
        from config.dynamic_config import DynamicConfig
        cfg = DynamicConfig()
        d_macro, metrics = self.stream._compute_d_macro(market_data, cfg)

        # d_macro = 0.35*2.0 + 0.25*1.5 + 0.25*1.0 + 0.15*0.8 = 0.70 + 0.375 + 0.25 + 0.12 = 1.445
        assert d_macro > 1.0
        assert metrics['z_skew'] == 2.0
        assert metrics['z_credit'] == 1.5

    def test_alert_zone_otm_put_spread(self):
        # D_macro >= 1.5 -> Alert Zone
        market_data = {
            'z_skew': 2.5,
            'features': {'z_credit': 2.0, 'z_breadth': 1.5, 'z_vpin': 1.0},
            'pipeline_state': {'hmm_transition': {'bull': 0.2, 'crash': 0.3, 'bear': 0.1}}
        }
        # Populate history
        for _ in range(40):
            self.stream._bull_history.append(0.2)
            self.stream._crash_history.append(0.3)

        signals = self.stream.generate_signals('sideways', market_data)
        assert len(signals) >= 1
        # Alert zone should trigger executable Inverse 2X ETF (252670)
        assert signals[0]['ticker'] == '252670'
        assert signals[0]['strategy'] == 'beta_convexity_hedge_alert'

    def test_trigger_zone_downside_router(self):
        # D_macro >= 2.5 & crash_prob >= 0.6 -> Trigger Zone
        market_data = {
            'z_skew': 3.5,
            'features': {'z_credit': 3.0, 'z_breadth': 2.5, 'z_vpin': 2.0, 'soxx_drop_pct': -4.0},
            'pipeline_state': {'hmm_transition': {'bull': 0.1, 'crash': 0.7, 'bear': 0.1}}
        }
        for _ in range(40):
            self.stream._bull_history.append(0.1)
            self.stream._crash_history.append(0.4)

        signals = self.stream.generate_signals('crash', market_data)
        assert len(signals) >= 1
        # Tech drop soxx <= -3.0 should route to KODEX 반도체인버스 (390390)
        assert signals[0]['ticker'] == '390390'

    def test_kofr_neutral_defense_fallback(self):
        market_data = {
            'z_skew': 0.0,
            'features': {'z_credit': 0.0, 'z_breadth': 0.0, 'z_vpin': 0.0},
            'pipeline_state': {'hmm_transition': {'bull': 0.3, 'crash': 0.2, 'bear': 0.1}}
        }
        for _ in range(40):
            self.stream._bull_history.append(0.3)
            self.stream._crash_history.append(0.3)

        signals = self.stream.generate_signals('sideways', market_data)
        assert len(signals) == 1
        assert signals[0]['ticker'] == '357870'  # KOFR
        assert signals[0]['is_base_position'] is True


    def test_global_buying_power_integrated_margin(self):
        # 10M KRX stock collateral (75%) + 2M KRW cash = 7.5M + 2M = 9.5M BP * 50% cap = 4.75M
        gbp = self.stream.get_global_buying_power(
            krx_portfolio_value_krw=10000000.0,
            krw_cash=2000000.0,
            margin_cap_pct=0.50
        )
        assert gbp == 4750000.0

    def test_s0_same_day_exit_lockout(self):
        market_data = {
            'z_skew': 3.5,
            'features': {'z_credit': 1.0, 'z_breadth': 2.5, 'z_vpin': 2.0},
            'pipeline_state': {'hmm_transition': {'bull': 0.1, 'crash': 0.7, 'bear': 0.1}},
            'daily_exit_details': {
                '252670': {'reason': '[ETF Stop] Trailing Stop (ATR 기반)', 'realized_pnl_pct': -3.05}
            },
        }
        for _ in range(40):
            self.stream._bull_history.append(0.1)
            self.stream._crash_history.append(0.4)

        signals = self.stream.generate_signals('crash', market_data)
        assert len(signals) == 1
        assert signals[0]['ticker'] == '357870'
        assert signals[0].get('is_base_position') is True

    def test_s0_opening_gap_exhaustion_veto(self):
        # 252670 gapped from 110 to 115 (+4.55%) and chased at 117 (+6.36%) >= 1.5 * sigma
        market_data = {
            'z_skew': 3.5,
            'features': {'z_credit': 1.0, 'z_breadth': 2.5, 'z_vpin': 2.0},
            'pipeline_state': {'hmm_transition': {'bull': 0.1, 'crash': 0.7, 'bear': 0.1}},
            'intraday_ohlc': {
                '252670': {'prev_close': 110.0, 'open': 115.0, 'high': 119.0, 'low': 114.0, 'close': 117.0}
            },
        }
        for _ in range(40):
            self.stream._bull_history.append(0.1)
            self.stream._crash_history.append(0.4)

        signals = self.stream.generate_signals('crash', market_data)
        assert len(signals) == 1
        assert signals[0]['ticker'] == '357870'
        assert signals[0].get('is_base_position') is True

    def test_s0_intraday_rebound_short_squeeze_veto(self):
        # KOSPI recovered to -0.09% while KOSDAQ surged +4.46% (intraday short squeeze)
        market_data = {
            'z_skew': 3.5,
            'features': {'z_credit': 1.0, 'z_breadth': 2.5, 'z_vpin': 2.0},
            'pipeline_state': {'hmm_transition': {'bull': 0.1, 'crash': 0.7, 'bear': 0.1}},
            'signal_cache': {'kospi_change_1d': -0.0969, 'kosdaq_change_pct': 4.4636},
        }
        for _ in range(40):
            self.stream._bull_history.append(0.1)
            self.stream._crash_history.append(0.4)

        signals = self.stream.generate_signals('crash', market_data)
        assert len(signals) == 1
        assert signals[0]['ticker'] == '357870'
        assert signals[0].get('is_base_position') is True

