"""
Unit Tests for Alpha 1 (S2 ML ETF Basket Decomposer)
"""
import pytest
from src.streams.s2_ml_alpha.etf_basket_decomposer import S2ETFBasketDecomposer

class TestAlpha1ETFBasketDecomposer:
    def test_decompose_empty_input(self):
        decomposer = S2ETFBasketDecomposer()
        res = decomposer.decompose_to_etf_signals([])
        assert res == []

    def test_decompose_sector_mapping(self):
        decomposer = S2ETFBasketDecomposer()
        scored_stocks = [
            {'ticker': '005930', 'score': 0.85, 'sector': 'IT/Semiconductor'},
            {'ticker': '000660', 'score': 0.75, 'sector': 'IT/Semiconductor'},
            {'ticker': '005380', 'score': 0.70, 'sector': 'Battery/Auto'},
            {'ticker': '068270', 'score': 0.40, 'sector': 'Bio/Healthcare'}, # Under min threshold
        ]
        res = decomposer.decompose_to_etf_signals(scored_stocks)
        assert len(res) >= 1
        top_sig = res[0]
        assert top_sig['stream_id'] == 'S2'
        assert top_sig['strategy'] == 's2_ml_etf_basket_decomposition'
        assert top_sig['tax_drag_pct'] == 0.00
        assert top_sig['ticker'] == '091160' # KODEX 반도체

    def test_s2_stream_integration_only_outputs_etfs(self):
        from src.streams.s2_ml_alpha.ml_stream import S2MLAlphaStream
        s2 = S2MLAlphaStream()
        mock_candidates = [
            {'ticker': '005930', 'name': '삼성전자', 'close': 70000, 'rsi': 60, 'bb_position': 0.6, 'macd_signal': 1, 'volume_ratio': 1.2, 'momentum_5d': 0.02, 'sector': 'IT/Semiconductor'},
            {'ticker': '000660', 'name': 'SK하이닉스', 'close': 140000, 'rsi': 65, 'bb_position': 0.7, 'macd_signal': 1, 'volume_ratio': 1.5, 'momentum_5d': 0.03, 'sector': 'IT/Semiconductor'},
            {'ticker': '005380', 'name': '현대차', 'close': 200000, 'rsi': 55, 'bb_position': 0.5, 'macd_signal': 0, 'volume_ratio': 1.0, 'momentum_5d': 0.01, 'sector': 'Battery/Auto'}
        ]
        m_data = {'alpha_candidates': mock_candidates, 'signal_cache': {'vix': 16.0, 'vkospi': 16.0}}
        signals = s2.generate_signals(regime='bull', market_data=m_data)

        # Check all emitted non-meta tickers are ETF codes (6 digits, e.g. 091160, 091170)
        # and NONE are individual stock codes like 005930, 000660, 005380
        etf_tickers = {'091160', '091170', '244580', '091180', '117460', '390390', '305720', '_SYS_META'}
        for sig in signals:
            t = sig['ticker']
            assert t in etf_tickers, f"Individual stock ticker leaked into S2 output: {t}"
            if t != '_SYS_META':
                assert sig['tax_drag_pct'] == 0.00
                assert sig['strategy'] == 's2_ml_etf_basket_decomposition'
