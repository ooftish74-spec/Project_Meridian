"""
tests/test_hyper_qvm_engine.py
Unit tests for Meridian 2.0 ModularHyperQVMEngine (100% Dynamic Math Model).
"""

import pytest
import numpy as np
from src.strategy.hyper_qvm_engine import ModularHyperQVMEngine, norm_cdf


@pytest.fixture
def engine():
    return ModularHyperQVMEngine()


def test_norm_cdf_precision():
    assert abs(norm_cdf(0.0) - 0.5) < 1e-6
    assert norm_cdf(3.0) > 0.99
    assert norm_cdf(-3.0) < 0.01


def test_orderbook_phase_transition(engine):
    bids = [{'price': 100, 'qty': 500}, {'price': 99, 'qty': 300}]
    asks = [{'price': 101, 'qty': 100}, {'price': 102, 'qty': 100}]
    hist_imb = [0.1, 0.2, 0.15, 0.3, 0.25, 0.4, 0.35, 0.5, 0.45, 0.6]

    res = engine.calculate_orderbook_phase_transition(bids, asks, hist_imb)
    assert 'phase_signal' in res
    assert 0.0 <= res['phase_signal'] <= 1.0
    assert res['raw_imbalance'] > 0  # Bids > Asks
    assert 'is_vacuum_risk' in res


def test_volatility_surface_tensor(engine):
    hist_skew = [0.1, 0.12, 0.11, 0.15, 0.14, 0.13, 0.16, 0.18, 0.17, 0.20]
    res = engine.calculate_volatility_surface_tensor(
        atm_vol=0.20,
        put_90_vol=0.25,
        call_110_vol=0.17,
        historical_skew=hist_skew
    )
    assert 'dealer_vanna_proxy' in res
    assert -1.0 <= res['tensor_signal'] <= 1.0


def test_score_hyper_qvm_universe(engine):
    candidates = [
        {'ticker': 'NVDA', 'roic': 0.35, 'fcf_yield': 0.04, 'mom_12m': 0.80, 'buyback_yield': 0.02, 'pricing_power': 0.9},
        {'ticker': 'AAPL', 'roic': 0.45, 'fcf_yield': 0.05, 'mom_12m': 0.30, 'buyback_yield': 0.04, 'pricing_power': 0.8},
        {'ticker': 'JNJ', 'roic': 0.15, 'fcf_yield': 0.06, 'mom_12m': -0.05, 'buyback_yield': 0.01, 'pricing_power': 0.5},
        {'ticker': 'BAD', 'roic': -0.10, 'fcf_yield': -0.02, 'mom_12m': -0.40, 'buyback_yield': 0.0, 'pricing_power': 0.1}
    ]

    res = engine.score_hyper_qvm_universe(candidates)
    assert len(res) == 4
    # NVDA or AAPL should rank highest
    assert res[0]['ticker'] in ['NVDA', 'AAPL']
    assert res[-1]['ticker'] == 'BAD'
    assert 'hyper_qvm_score' in res[0]
    assert 'cdf_rank' in res[0]


def test_score_hyper_qvm_universe_empty(engine):
    assert engine.score_hyper_qvm_universe([]) == []


def test_dislocation_trigger(engine):
    res_normal = engine.calculate_dislocation_trigger(0.0, 0.0, 0.0)
    assert res_normal['distress_confidence'] == 0.5
    assert not res_normal['is_dislocation_event']

    res_stress = engine.calculate_dislocation_trigger(3.0, 2.5, -2.0)
    assert res_stress['distress_confidence'] > 0.95
    assert res_stress['is_dislocation_event']
    assert res_stress['recommended_aggression_multiplier'] > 1.5


def test_tda_topological_scale(engine):
    # High correlation matrix -> High Graph Energy -> Risk scale decreases
    corr_high = np.array([
        [1.0, 0.95, 0.90],
        [0.95, 1.0, 0.92],
        [0.90, 0.92, 1.0]
    ])
    hist_energies = [1.0, 1.2, 1.1, 1.3, 1.0, 1.2, 1.1, 1.4, 1.2, 1.3]

    res = engine.calculate_tda_topological_scale(corr_high, hist_energies)
    assert res['graph_energy'] > 4.0
    assert 0.20 <= res['tda_risk_scale'] <= 1.0


def test_tda_topological_scale_invalid(engine):
    res = engine.calculate_tda_topological_scale(np.array([1.0]))
    assert res['tda_risk_scale'] == 1.0


def test_capital_recycling_ratio(engine):
    res = engine.calculate_capital_recycling_ratio(
        s1_s2_rolling_sharpe=2.5,
        s3_valuation_zscore=-1.8  # Deep valuation discount
    )
    assert res['reinvest_transfer_ratio'] > 0.30
    assert res['reinvest_transfer_ratio'] <= 0.60


def test_evaluate_full_snapshot(engine):
    snapshot = {
        'bids': [{'qty': 100}],
        'asks': [{'qty': 100}],
        'atm_vol': 0.20,
        'put_90_vol': 0.22,
        'call_110_vol': 0.18,
        'vix_zscore': 1.0,
        'candidates': [
            {'ticker': 'NVDA', 'roic': 0.30, 'fcf_yield': 0.05, 'mom_12m': 0.50}
        ]
    }
    state = {'s1_s2_sharpe': 1.5, 's3_val_zscore': -0.5}

    out = engine.evaluate(snapshot, state)
    assert out['overall_status'] == 'OK'
    assert 'orderbook_phase' in out
    assert 'hyper_qvm_rankings' in out
