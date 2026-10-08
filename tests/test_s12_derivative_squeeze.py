"""
Unit tests for S12DerivativeSqueezeStream module.
"""

import pytest
from src.streams.s12_derivative_squeeze.derivative_squeeze_stream import S12DerivativeSqueezeStream


def test_s12_instance_creation():
    stream = S12DerivativeSqueezeStream()
    assert stream.stream_id == "S12_DERIVATIVE_SQUEEZE"
    assert stream.is_active() is True


def test_s12_z_score_computation():
    stream = S12DerivativeSqueezeStream()
    # Normal Z-score
    z = stream.compute_z_score(value=10.0, mean=5.0, std=2.5)
    assert z == 2.0

    # Zero std exception handling
    z_zero = stream.compute_z_score(value=10.0, mean=5.0, std=0.0)
    assert z_zero == 0.0


def test_s12_bdi_computation():
    stream = S12DerivativeSqueezeStream()
    # Backwardation (Futures < Spot)
    bdi_back = stream.compute_basis_disparity_index(futures_price=350.0, spot_price=355.0)
    assert round(bdi_back, 4) == round((350.0 - 355.0) / 355.0, 4)
    assert bdi_back < 0.0

    # Contango (Futures > Spot)
    bdi_con = stream.compute_basis_disparity_index(futures_price=360.0, spot_price=355.0)
    assert bdi_con > 0.0

    # Invalid spot
    assert stream.compute_basis_disparity_index(futures_price=350.0, spot_price=0.0) == 0.0


def test_s12_squeeze_signal_trigger():
    stream = S12DerivativeSqueezeStream()
    
    # Mock market data satisfying 3 squeeze conditions:
    # 1. z_skew >= 1.5
    # 2. z_flow_sell >= 1.5 (foreign_futures_flow is -6000, mean=0, std=3000 -> -(-6000)/3000 = +2.0)
    # 3. bdi <= 0.0 (futures 350 vs spot 355)
    market_data = {
        'signal_cache': {
            'call_option_skew': 2.5,
            'call_option_skew_mean_20d': 0.5,
            'call_option_skew_std_20d': 1.0,  # Z_skew = (2.5-0.5)/1.0 = +2.0
            'foreign_futures_net_contracts': -6000.0,
            'foreign_futures_net_mean_20d': 0.0,
            'foreign_futures_net_std_20d': 3000.0,  # Z_flow_sell = -(-6000)/3000 = +2.0
            'kospi200_futures_price': 350.0,
            'kospi200_spot_price': 355.0  # BDI = (350-355)/355 = -0.014 < 0.0
        }
    }

    signals = stream.generate_signals(regime="bull", market_data=market_data)
    assert len(signals) == 1
    sig = signals[0]
    assert sig['stream_id'] == "S12_DERIVATIVE_SQUEEZE"
    assert sig['ticker'] == "252670"  # Inverse 2X ticker
    assert sig['strategy'] == "derivative_squeeze_short_alpha"
    assert sig['metrics']['z_skew'] == 2.0
    assert sig['metrics']['z_flow_sell'] == 2.0
    assert sig['metrics']['bdi'] < 0.0


def test_s12_neutral_passivity():
    stream = S12DerivativeSqueezeStream()
    
    # Market data failing flow trigger condition
    market_data = {
        'signal_cache': {
            'call_option_skew': 0.0,  # Z_skew = 0 < 1.5
            'foreign_futures_net_contracts': 1000.0,
            'kospi200_futures_price': 355.0,
            'kospi200_spot_price': 350.0
        }
    }

    signals = stream.generate_signals(regime="bull", market_data=market_data)
    assert len(signals) == 0
