"""
tests/test_s14_pair_stream.py
Unit tests for S14 Market-Neutral Sector Pair & Stat-Arb Alpha Stream.
"""

import pytest
import numpy as np
from src.streams.s14_pair_alpha.sector_pair_stream import S14SectorPairStream
from src.allocation.alpha_allocator import AlphaAllocator


@pytest.fixture
def stream():
    return S14SectorPairStream()


def test_calculate_market_ker_trending(stream):
    # Perfect uptrend: KER should be 1.0
    trending_prices = np.linspace(100, 200, 21)
    ker = stream.calculate_market_ker(trending_prices)
    assert pytest.approx(ker, rel=1e-3) == 1.0


def test_calculate_market_ker_chop(stream):
    # Severe chop / zig-zag: KER should be low
    chop_prices = np.array([100, 105, 100, 105, 100, 105, 100, 105, 100, 105, 100])
    ker = stream.calculate_market_ker(chop_prices)
    assert ker < 0.20


def test_generate_signals_sideways_market(stream):
    # Market in chop/sideways: KER < 0.40
    market_data = {
        'qqq_ker': 0.20,
        'kospi_ker': 0.25,
        'vix': 16.0
    }
    signals = stream.generate_signals(regime='caution', market_data=market_data)
    assert len(signals) > 0
    for s in signals:
        assert s['stream_id'] == 'S14_PAIR_ALPHA'
        assert s['direction'] in ('long', 'short')
        assert s['action'] in ('buy', 'sell')
        assert 0.0 <= s['confidence'] <= 1.0
        assert s['size_pct'] > 0.0


def test_generate_signals_strong_trend_market(stream):
    # Strong unidirectional trend: KER > 0.60
    # Pair stream should remain idle to avoid betting against runaway momentum
    market_data = {
        'qqq_ker': 0.75,
        'kospi_ker': 0.70,
        'vix': 14.0
    }
    signals = stream.generate_signals(regime='bull', market_data=market_data)
    # High KER means trending, so pair engine should withhold signals
    assert len(signals) == 0


def test_stream_positions_and_performance(stream):
    positions = stream.get_positions()
    assert isinstance(positions, list)
    perf = stream.get_performance()
    assert isinstance(perf, dict)
    assert perf['stream_id'] == 'S14_PAIR_ALPHA'
    assert 'win_rate' in perf
    assert 'sharpe' in perf


def test_allocator_includes_s14():
    allocator = AlphaAllocator()
    assert 'S14_PAIR_ALPHA' in allocator.STREAMS
    weights = allocator._default_regime_weights()
    assert 'S14_PAIR_ALPHA' in weights['caution']
    assert weights['caution']['S14_PAIR_ALPHA'] > 0.10
