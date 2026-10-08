"""
Unit tests for StealthQVMEngine (KRX Stealth Accumulation & Multi-Factor Engine).
"""
import pytest
import pandas as pd
import numpy as np
from src.strategy.stealth_qvm_engine import StealthQVMEngine, norm_cdf, z_score


def test_norm_cdf_properties():
    """Verify statistical properties of norm_cdf."""
    assert abs(norm_cdf(0.0) - 0.5) < 1e-5
    assert norm_cdf(3.0) > 0.99
    assert norm_cdf(-3.0) < 0.01


def test_coiled_momentum_calculation():
    """Test Coiled Momentum Index calculation."""
    engine = StealthQVMEngine()
    np.random.seed(42)
    prices = pd.Series(100.0 + np.sin(np.linspace(0, 10, 150)))
    vols = pd.Series(1000 + np.random.normal(0, 50, 150))
    # Spike recent volumes
    vols.iloc[-40:] = vols.iloc[-40:] * 2.5

    res = engine.calculate_coiled_momentum(prices, vols, short_window=40, long_window=120)
    assert 'coiled_score' in res
    assert res['vol_ratio'] > 1.5
    assert 0.0 <= res['coiled_score'] <= 1.0


def test_smart_money_flow():
    """Test Smart Money Flow Imbalance calculation."""
    engine = StealthQVMEngine()
    foreign = pd.Series(np.random.normal(100, 20, 100))
    inst = pd.Series(np.random.normal(200, 30, 100))
    adtv = pd.Series(np.full(100, 10000.0))

    res = engine.calculate_smart_money_flow(foreign, inst, adtv, window=20)
    assert 'smart_flow_score' in res
    assert 0.0 <= res['smart_flow_score'] <= 1.0
    assert res['flow_pct'] > 0.0


def test_qvm_safety_score():
    """Test QVM Safety Score calculations."""
    engine = StealthQVMEngine()
    qvs = engine.calculate_qvm_safety_score(
        roe=15.0, debt_ratio=50.0, fcf=10.0, per=10.0, pbr=0.9,
        sector_per_mean=15.0, sector_pbr_mean=1.2
    )
    assert qvs['quality_score'] > 0.5
    assert qvs['value_score'] > 0.5
    assert 0.0 <= qvs['q_v_composite'] <= 1.0


def test_generate_krx_stream_signals():
    """Test S1 and S5 signal generation logic."""
    engine = StealthQVMEngine()
    np.random.seed(42)
    prices = pd.Series(100.0 + np.cumsum(np.random.normal(0.1, 0.5, 150)))
    vols = pd.Series(5000 + np.random.normal(0, 200, 150))
    vols.iloc[-40:] *= 3.0

    foreign = pd.Series(np.random.normal(500, 50, 150))
    inst = pd.Series(np.random.normal(500, 50, 150))
    adtv = pd.Series(np.full(150, 20000.0))

    fund = {'roe': 18.0, 'debt_ratio': 40.0, 'fcf': 50.0, 'per': 8.0, 'pbr': 0.8}

    res = engine.generate_krx_stream_signals(
        ticker='005930', name='삼성전자',
        prices=prices, volumes=vols,
        foreign_net_buy=foreign, inst_net_buy=inst, adtv_series=adtv,
        fundamental_metrics=fund, ofi_velocity=2.5, auction_imbalance=1.0, is_etf=False
    )

    assert res['unified_score'] > 0.5
    assert 's1_signal' in res
    assert 's5_signal' in res
