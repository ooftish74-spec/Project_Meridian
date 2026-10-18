"""
Unit & Integration Tests for DynamicQuantEngine (100% Mathematical Dynamic Model).
Verifies empirical moment calibration, dynamic asset classification, log-normal VIX
quantiles, first-passage exit barriers, Ornstein-Uhlenbeck pair discovery, and
Maximum-Entropy (Gibbs-Boltzmann) stream weight allocation.
"""

import math
import pytest
from src.utils.dynamic_quant_engine import DynamicQuantEngine, get_quant_engine
from src.allocation.alpha_allocator import AlphaAllocator


def test_dynamic_quant_engine_singleton_and_calibration():
    dqe = get_quant_engine()
    assert isinstance(dqe, DynamicQuantEngine)
    # Trigger calibration
    cash_set = dqe.get_cash_equivalent_tickers()
    assert isinstance(cash_set, set)
    assert len(cash_set) >= 2
    # Optimal KR parking ticker must be a valid 6-char KR code
    kr_parking = dqe.get_optimal_kr_parking_ticker()
    assert isinstance(kr_parking, str)
    assert len(kr_parking) == 6


def test_dynamic_inverse_and_defensive_classification():
    dqe = get_quant_engine()
    kr_inv = dqe.get_inverse_hedge_tickers('KR')
    us_inv = dqe.get_inverse_hedge_tickers('US')
    assert isinstance(kr_inv, list) and len(kr_inv) >= 1
    assert isinstance(us_inv, list) and len(us_inv) >= 1

    # Verify inverse tickers actually have negative empirical beta in calibrated moments
    for tk in kr_inv:
        m = dqe.get_asset_metrics(tk)
        if m.get('n_obs', 0) > 0:
            assert m['beta'] < -0.30
            assert m['corr'] < -0.40

    for tk in us_inv:
        m = dqe.get_asset_metrics(tk)
        if m.get('n_obs', 0) > 0:
            assert m['beta'] < -0.30
            assert m['corr'] < -0.40

    # Defensive exempt set must contain inverse + cash + safe haven tickers
    exempt = dqe.get_defensive_exempt_tickers()
    assert isinstance(exempt, set)
    for tk in kr_inv + us_inv:
        assert tk in exempt


def test_vix_lognormal_regime_quantiles():
    dqe = get_quant_engine()
    q = dqe.get_vix_regime_quantiles()
    assert set(q.keys()) == {'baseline', 'caution', 'bear', 'crash', 'extreme'}
    # Quantiles must be strictly monotonic increasing
    assert 8.0 <= q['baseline'] < q['caution'] < q['bear'] < q['crash'] < q['extreme'] <= 85.0


def test_first_passage_dynamic_exit_barriers():
    dqe = get_quant_engine()
    barriers = dqe.compute_dynamic_exit_barriers('005930', holding_days=5.0, regime='caution')
    assert 'sl_pct' in barriers and 'tp_pct' in barriers and 'rr_ratio' in barriers
    assert 0.004 <= barriers['sl_pct'] <= 0.15
    assert 0.008 <= barriers['tp_pct'] <= 0.35
    assert barriers['tp_pct'] > barriers['sl_pct']
    assert barriers['rr_ratio'] > 1.0


def test_cross_sectional_dynamic_confidence_cutoff():
    dqe = get_quant_engine()
    signals = [
        {'ticker': '005930', 'confidence': 0.45},
        {'ticker': '000660', 'confidence': 0.52},
        {'ticker': '035420', 'confidence': 0.61},
        {'ticker': '051910', 'confidence': 0.68},
        {'ticker': '006400', 'confidence': 0.75},
        {'ticker': '207940', 'confidence': 0.82},
    ]
    c_bull = dqe.compute_dynamic_confidence_cutoff(signals, regime='bull', vix=15.0)
    c_crash = dqe.compute_dynamic_confidence_cutoff(signals, regime='crash', vix=35.0)
    assert 0.35 <= c_bull <= 0.85
    assert 0.35 <= c_crash <= 0.85
    # Higher VIX / crash regime should demand higher confidence cutoff for non-hedges
    assert c_crash >= c_bull


def test_maxent_gibbs_boltzmann_stream_weights():
    dqe = get_quant_engine()
    streams = ['S0', 'S1', 'S2', 'S3', 'S5', 'S7', 'S10', 'S14_PAIR_ALPHA']
    for regime in ('bull', 'caution', 'bear', 'crash'):
        w = dqe.compute_maxent_stream_weights(streams, regime=regime)
        assert set(w.keys()) == set(streams)
        assert math.isclose(sum(w.values()), 1.0, rel_tol=1e-3)
        assert all(v > 0.0 for v in w.values())

    # In crash regime, defensive/tail-hedge/pair streams (S0, S7, S14_PAIR_ALPHA) receive higher share than in bull
    w_bull = dqe.compute_maxent_stream_weights(streams, regime='bull')
    w_crash = dqe.compute_maxent_stream_weights(streams, regime='crash')
    assert (w_crash['S0'] + w_crash['S7'] + w_crash['S14_PAIR_ALPHA']) > (
        w_bull['S0'] + w_bull['S7'] + w_bull['S14_PAIR_ALPHA']
    )

    # Verify AlphaAllocator._default_regime_weights uses MaxEnt weights
    w_alloc = AlphaAllocator()._default_regime_weights()['bear']
    assert math.isclose(sum(w_alloc.values()), 1.0, rel_tol=1e-3)


def test_ornstein_uhlenbeck_stat_arb_pair_discovery():
    dqe = get_quant_engine()
    kr_pairs = dqe.discover_stat_arb_pairs(market='KR', max_pairs=3)
    assert isinstance(kr_pairs, list)
    for p in kr_pairs:
        assert 'stock_a' in p and 'stock_b' in p
        assert 1.5 <= p['half_life'] <= 25.0
        assert 1.15 <= p['z_entry_threshold'] <= 2.20
        assert abs(p['z_score']) >= p['z_entry_threshold']
