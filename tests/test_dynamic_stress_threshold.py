import pytest
import numpy as np
import json
from unittest.mock import patch
from src.risk.crash_defense import DynamicStressThreshold, CrashDefense

def test_dynamic_stress_threshold():
    dyn = DynamicStressThreshold()
    dyn.rolling_window = 10
    dyn.min_observations = 5
    
    # Test insufficient history fallback
    current = {'vix': 20, 'vkospi': 15, 'fx_change_pct': 0.5, 'sp500_change_pct': 1.0}
    res1 = dyn.update_and_get_thresholds(current)
    # 초기 히스토리 부족 시에도 MD는 계산 가능 (≥0), fallback 임계치 적용
    assert res1['mahalanobis_distance'] >= 0.0
    
    # Add enough history
    for i in range(10):
        dyn.update_and_get_thresholds({
            'vix': 20 + i,
            'vkospi': 15 + i,
            'fx_change_pct': 0.5,
            'sp500_change_pct': 1.0
        })
        
    res2 = dyn.update_and_get_thresholds(current)
    assert 'vix' in res2['thresholds']
    assert 'vix' in res2['z_scores']
    assert res2['mahalanobis_distance'] >= 0.0

def test_crash_defense_measure():
    cd = CrashDefense()
    market_data = {
        'signal_cache': {'vix': 40, 'vkospi': 30},
        'overnight_intel': {}
    }
    portfolio = {}
    
    res = cd.measure(market_data, portfolio)
    assert 'dynamic_thresholds' in res
    assert 'z_scores' in res
    assert 'mahalanobis_distance' in res
    assert res['stress_score'] > 0
