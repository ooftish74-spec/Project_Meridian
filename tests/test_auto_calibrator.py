import pytest
import numpy as np
from src.analysis.auto_calibrator import AutoCalibrator

def test_compute_dynamic_vix_max():
    # Test fallback
    assert AutoCalibrator.compute_dynamic_vix_max(None) == 18.5
    
    # Test with rolling window (e.g. VIX ranging 10 to 30)
    vix_series = np.linspace(10, 30, 252)
    # 40th percentile of linspace(10, 30) is 10 + 0.40 * 20 = 18.0
    vix_max = AutoCalibrator.compute_dynamic_vix_max(vix_series)
    assert 17.5 <= vix_max <= 18.5

def test_compute_dynamic_min_confidence():
    # Test fallback
    assert AutoCalibrator.compute_dynamic_min_confidence(None) == 0.80

    # Test with score series (e.g. confidence ranging 0.5 to 0.9)
    conf_series = np.linspace(0.5, 0.9, 100)
    # P80 of linspace(0.5, 0.9) is 0.5 + 0.8 * 0.4 = 0.82
    min_conf = AutoCalibrator.compute_dynamic_min_confidence(conf_series)
    assert 0.80 <= min_conf <= 0.85

def test_compute_dynamic_etp_friction_multiplier():
    # Test fallback
    assert AutoCalibrator.compute_dynamic_etp_friction_multiplier(None) == 1.15

    # Test with low spread (0.02%)
    mult_low = AutoCalibrator.compute_dynamic_etp_friction_multiplier(0.0002)
    assert 1.05 <= mult_low <= 1.25

    # Test with high spread (0.2%)
    mult_high = AutoCalibrator.compute_dynamic_etp_friction_multiplier(0.002)
    assert mult_high > mult_low
