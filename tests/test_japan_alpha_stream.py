"""
tests/test_japan_alpha_stream.py
Unit tests for S13 Japan Niche Alpha Stream.
"""

import pytest
from src.streams.s13_japan_alpha.japan_stream import S13JapanAlphaStream, norm_cdf, sigmoid


@pytest.fixture
def stream():
    return S13JapanAlphaStream()


def test_calculate_us_lead_signal_bullish(stream):
    res = stream.calculate_us_lead_signal(
        nvda_chg_pct=2.5,
        soxx_chg_pct=1.8,
        qqq_chg_pct=1.2
    )
    assert res['lead_signal'] > 0.50
    assert res['is_bullish_lead']


def test_calculate_us_lead_signal_bearish(stream):
    res = stream.calculate_us_lead_signal(
        nvda_chg_pct=-3.0,
        soxx_chg_pct=-2.5,
        qqq_chg_pct=-1.5
    )
    assert res['lead_signal'] < -0.50
    assert not res['is_bullish_lead']


def test_score_japan_universe(stream):
    scored = stream.score_japan_universe(
        us_lead_signal=0.8,
        jpy_krw_chg_pct=0.5
    )
    assert len(scored) == len(stream.JAPAN_NICHE_UNIVERSE)
    # Semiconductor equipment or trading house highest scored
    assert scored[0]['composite_score'] > 0.30
    assert 'suggested_weight' in scored[0]


def test_generate_signals_backtest_mode(stream):
    market_data = {
        'backtest_mode': True,
        'signal_cache': {
            'nvda_chg_pct': 2.0,
            'soxx_chg_pct': 1.5,
            'qqq_chg_pct': 1.0,
            'jpy_krw_chg_pct': 0.1
        }
    }
    signals = stream.generate_signals('bull', market_data)
    assert len(signals) > 0
    assert signals[0]['stream'] == 'S13'
    assert signals[0]['market'] == 'JP'
    assert 'symbol' in signals[0]
