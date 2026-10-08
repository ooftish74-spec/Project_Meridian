"""
tests/test_s5_s10_promotion_engine.py
Unit tests for Meridian S5 & S10 Dynamic Mathematical Promotion Engine.
"""

import pytest
import numpy as np
from src.strategy.s5_s10_promotion_engine import S5S10PromotionEngine, norm_cdf, norm_ppf


@pytest.fixture
def engine():
    return S5S10PromotionEngine()


def test_norm_ppf_precision():
    assert abs(norm_ppf(0.5) - 0.0) < 1e-4
    assert norm_ppf(0.975) > 1.90
    assert norm_ppf(0.025) < -1.90


def test_evaluate_s5_overnight_bullish(engine):
    snapshot = {
        'night_futures_chg_pct': 0.8,
        'vix_afterhours_chg': -1.2,
        'us_momentum_chg_pct': 0.5,
        'vix': 16.0,
        'globex_futures_chg_pct': 0.3
    }
    res = engine.evaluate_s5_overnight(snapshot)
    assert res['p_ois_probability'] > 0.60
    assert res['is_entry_approved']
    assert not res['early_exit_triggered']
    assert res['recommended_target_etf'] == '069500'


def test_evaluate_s5_overnight_early_exit(engine):
    snapshot = {
        'night_futures_chg_pct': -1.5,
        'vix_afterhours_chg': 3.0,
        'us_momentum_chg_pct': -1.2,
        'vix': 25.0,
        'globex_futures_chg_pct': -2.5,
        'historical_globex_futures': [0.1, -0.2, 0.0, 0.1, -0.1, 0.2, -0.3, 0.1, -0.2, 0.0]
    }
    res = engine.evaluate_s5_overnight(snapshot)
    assert res['p_ois_probability'] < 0.40
    assert not res['is_entry_approved']
    assert res['early_exit_triggered']


def test_evaluate_s10_mega_trend_normal(engine):
    snapshot = {
        'sector_flows': {
            'semiconductor': {'streak': 5, 'netbuy': 5e10, 'accel': 1e10, 'leader_ticker': '005930'},
            'bio': {'streak': 1, 'netbuy': 1e9, 'accel': -5e8, 'leader_ticker': '207940'}
        },
        'regime': 'bull',
        'vix_zscore': 0.0
    }
    res = engine.evaluate_s10_mega_trend(snapshot)
    assert res['top_sector'] == 'semiconductor'
    assert not res['is_crisis_alpha_regime']


def test_evaluate_s10_mega_trend_crisis_alpha(engine):
    snapshot = {
        'sector_flows': {
            'semiconductor': {'streak': 0, 'netbuy': -2e10, 'accel': -1e10}
        },
        'regime': 'crash',
        'vix_zscore': 2.5,
        'gold_trend_z': 2.0,
        'dollar_trend_z': 1.5,
        'treasury_trend_z': 1.8,
        'inverse_trend_z': 2.5
    }
    res = engine.evaluate_s10_mega_trend(snapshot)
    assert res['is_crisis_alpha_regime']
    assert 'GLD' in res['crisis_alpha_allocations']
    assert '252670' in res['crisis_alpha_allocations']
    assert res['crisis_alpha_allocations']['252670'] > 0.10
