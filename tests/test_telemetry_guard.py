"""
Unit tests for TelemetryValidationGuard
"""

import pytest
from src.utils.telemetry_guard import TelemetryValidationGuard

def test_telemetry_guard_valid_key():
    data = {'ewy_change_1d': 2.13, 'vix': 16.01}
    val, key, is_valid = TelemetryValidationGuard.extract_validated_metric(data, 'korea_night_market_1d')
    assert val == 2.13
    assert key == 'ewy_change_1d'
    assert is_valid is True

def test_telemetry_guard_missing_key():
    data = {'vix': 16.01}
    val, key, is_valid = TelemetryValidationGuard.extract_validated_metric(data, 'korea_night_market_1d')
    assert val == 0.0
    assert key == 'MISSING_KEY_FALLBACK'
    assert is_valid is False
