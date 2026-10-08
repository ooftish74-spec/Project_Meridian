"""
Unit tests for Wall Street Unbiased Engine modules:
1. NonParametricECDF
2. WalkForwardOOSGuard
"""

import numpy as np
import pytest
from src.utils.non_parametric_ecdf import NonParametricECDF
from src.learning.walk_forward_guard import WalkForwardOOSGuard

def test_non_parametric_ecdf_basic():
    ecdf = NonParametricECDF(window=252)
    history = [10.0, 15.0, 20.0, 25.0, 30.0]
    
    # 20.0 is the 3rd out of 5 -> 3/5 = 0.60
    pct = ecdf.compute_percentile(20.0, history)
    assert pytest.approx(pct, 0.01) == 0.60

def test_non_parametric_ecdf_empty():
    ecdf = NonParametricECDF(window=252)
    assert ecdf.compute_percentile(20.0, []) == 0.50
    assert ecdf.compute_z_score(20.0, []) == 0.0

def test_walk_forward_guard_approval():
    guard = WalkForwardOOSGuard(min_oos_ratio=0.70)
    
    # Good consistent returns
    is_rets = np.array([0.01, 0.02, -0.005, 0.015, 0.01] * 5)
    oos_rets = np.array([0.01, 0.015, -0.004, 0.012, 0.009] * 3)
    
    approved, metrics = guard.evaluate_override_safety(is_rets, oos_rets)
    assert approved is True
    assert metrics['decay_ratio'] >= 0.70

def test_walk_forward_guard_rejection():
    guard = WalkForwardOOSGuard(min_oos_ratio=0.70)
    
    # Overfitted returns: Great IS, terrible OOS
    is_rets = np.array([0.03, 0.025, 0.02, 0.035, 0.04] * 5)
    oos_rets = np.array([-0.02, -0.01, 0.001, -0.015, -0.02] * 3)
    
    approved, metrics = guard.evaluate_override_safety(is_rets, oos_rets)
    assert approved is False
    assert metrics['decay_ratio'] < 0.70

def test_domestic_and_overseas_pegging_engine():
    from datetime import datetime, timedelta
    from src.execution._kis_adapter import KISTraderAdapter
    adapter = KISTraderAdapter(mode="mock")
    
    # Domestic KRX stock pegging test (069500) - price within 10 bps of mock price 87635.0
    res_krx = adapter.execute_time_bounded_toleranced_pegging("069500", 87630.0, datetime.now() - timedelta(seconds=20), max_wait_sec=15.0, max_tolerance_bps=10.0)
    assert res_krx['action'] == 'repeg'
    assert res_krx['execution_method'] == 'modify'
    
    # Overseas US stock pegging test (SHV)
    res_us = adapter.execute_time_bounded_toleranced_pegging("SHV", 110.28, datetime.now() - timedelta(seconds=20), max_wait_sec=15.0, max_tolerance_bps=10.0)
    assert res_us['action'] == 'repeg'
    assert res_us['execution_method'] == 'cancel_and_reorder'
