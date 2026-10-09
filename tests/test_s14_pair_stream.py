"""
tests/test_s14_pair_stream.py
Unit tests for S14 Market-Neutral Sector Pair & Stat-Arb Alpha Stream.
Verifies 100% Dynamic Mathematical Model:
  1. Kaufman Efficiency Ratio (KER) & Brownian Noise (1 - KER)
  2. Dynamic Risk Parity (Zero-Beta Volatility Inverse Weighting: σ_s * w_s == σ_w * w_w)
  3. Gaussian Error Function (erf) CDF Confidence
  4. Continuous Bayesian Prior in AlphaAllocator
"""

import pytest
import math
import numpy as np
from src.streams.s14_pair_alpha.sector_pair_stream import S14SectorPairStream
from src.allocation.sector_pair_alpha_engine import SectorPairAlphaEngine
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
    # Severe chop / zig-zag: KER should be low (< 0.20)
    chop_prices = np.array([100, 105, 100, 105, 100, 105, 100, 105, 100, 105, 100])
    ker = stream.calculate_market_ker(chop_prices)
    assert ker < 0.20


def test_dynamic_risk_parity_weighting():
    # Verify that long and short risk contributions are equal: σ_s * w_s == σ_w * w_w
    engine = SectorPairAlphaEngine()
    sector_returns = {
        'semi': 0.05,
        'tech': 0.03,
        'energy': -0.02,
        'consumer_stap': -0.04
    }
    # semi has higher volatility (30%), consumer_stap has lower volatility (15%)
    volatility_map = {
        'semi': 0.30,
        'tech': 0.25,
        'energy': 0.20,
        'consumer_stap': 0.15
    }
    res = engine.generate_pair_signals(
        sector_returns=sector_returns,
        market='US',
        volatility_map=volatility_map
    )
    assert res['pair_active']
    orders = res['pair_orders']
    assert len(orders) >= 2

    # Check top pair (semi long vs consumer_stap short)
    semi_order = next(o for o in orders if o['sector'] == 'semi')
    stap_order = next(o for o in orders if o['sector'] == 'consumer_stap')

    # Risk contribution = volatility * weight
    risk_semi = volatility_map['semi'] * semi_order['target_weight']
    risk_stap = volatility_map['consumer_stap'] * stap_order['target_weight']

    # Both risks must be mathematically equal (Zero-Beta Volatility Parity)
    assert pytest.approx(risk_semi, rel=1e-2) == risk_stap
    # High volatility asset must have lower weight than low volatility asset
    assert semi_order['target_weight'] < stap_order['target_weight']


def test_gaussian_cdf_confidence():
    engine = SectorPairAlphaEngine()
    sector_returns = {
        'semi': 0.10,   # Massive divergence
        'tech': 0.00,
        'energy': -0.10
    }
    res = engine.generate_pair_signals(sector_returns=sector_returns, market='US')
    for order in res['pair_orders']:
        conf = order['confidence']
        assert 0.50 <= conf <= 0.99
        # Confidence must match Gaussian normal CDF erf formula
        z = abs(order['z_score'])
        expected_conf = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))
        assert pytest.approx(conf, abs=0.01) == expected_conf


def test_generate_signals_sideways_market(stream):
    market_data = {
        'qqq_ker': 0.20,
        'kospi_ker': 0.25,
        'vix': 16.0,
        'us_sector_returns': {
            'semi': 0.04,
            'tech': 0.02,
            'energy': -0.02,
            'consumer_stap': -0.03
        },
        'us_volatility_map': {
            'semi': 0.28,
            'tech': 0.22,
            'energy': 0.20,
            'consumer_stap': 0.14
        }
    }
    signals = stream.generate_signals(regime='caution', market_data=market_data)
    assert len(signals) > 0
    for s in signals:
        assert s['stream_id'] == 'S14_PAIR_ALPHA'
        assert s['direction'] in ('long', 'short')
        assert s['action'] in ('buy', 'sell')
        assert 0.50 <= s['confidence'] <= 1.0
        assert s['size_pct'] > 0.0


def test_generate_signals_strong_trend_market(stream):
    market_data = {
        'qqq_ker': 0.85,
        'kospi_ker': 0.80,
        'vix': 13.0
    }
    signals = stream.generate_signals(regime='bull', market_data=market_data)
    # High KER means trending, so pair engine should withhold signals
    assert len(signals) == 0


def test_continuous_bayesian_noise_prior_in_allocator():
    allocator = AlphaAllocator()
    
    # 1. Chop market (KER = 0.15 -> Noise = 0.85 > 0.50)
    chop_market = {'signal_cache': {'qqq_ker': 0.15, 'vix': 16.0}}
    metrics = {
        'S14_PAIR_ALPHA': {'win_rate': 0.55, 'avg_win': 0.03, 'avg_loss': 0.01}
    }
    weights_chop = allocator.allocate(stream_metrics=metrics, regime='caution', market_data=chop_market)
    
    # 2. Trend market (KER = 0.85 -> Noise = 0.15 < 0.50)
    trend_market = {'signal_cache': {'qqq_ker': 0.85, 'vix': 14.0}}
    weights_trend = allocator.allocate(stream_metrics=metrics, regime='bull', market_data=trend_market)
    
    # S14 weight in chop market must be strictly higher than in trend market due to Bayesian noise prior!
    assert weights_chop.get('S14_PAIR_ALPHA', 0.0) > weights_trend.get('S14_PAIR_ALPHA', 0.0)


def test_stream_positions_and_performance(stream):
    positions = stream.get_positions()
    assert isinstance(positions, list)
    perf = stream.get_performance()
    assert isinstance(perf, dict)
    assert perf['stream_id'] == 'S14_PAIR_ALPHA'
    assert 'win_rate' in perf
    assert 'sharpe' in perf
