"""
Tests for FactorDecayTracker
"""

import pytest
from src.analysis.factor_decay_tracker import FactorDecayTracker

def test_factor_decay_tracker_decaying_ic():
    tracker = FactorDecayTracker(default_half_life_days=30.0)

    # Rapidly decaying IC history over 20 days
    ic_history = [0.20 * (0.85 ** i) for i in range(20)]

    result = tracker.compute_decay_rate(ic_history)

    assert result['half_life_days'] < 30.0  # Half life should be shorter than default
    assert result['lambda_decay'] > 0.0
    assert 0.0 < result['current_discount_factor'] <= 1.0

def test_factor_decay_tracker_short_history():
    tracker = FactorDecayTracker(default_half_life_days=30.0)
    ic_history = [0.10, 0.12, 0.11]  # Only 3 days

    result = tracker.compute_decay_rate(ic_history)

    assert result['half_life_days'] == 30.0
    assert result['current_discount_factor'] == 1.0
