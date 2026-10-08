"""
tests/test_three_country_dynamic_allocator.py
Unit tests for 3-Country Dynamic Capital Allocator (Korea, US, Japan).
"""

import pytest
import numpy as np
from src.allocation.three_country_dynamic_allocator import ThreeCountryDynamicAllocator


@pytest.fixture
def allocator():
    return ThreeCountryDynamicAllocator()


def test_calculate_country_risk_parity_weights(allocator):
    res = allocator.calculate_country_risk_parity_weights(
        kr_vol=0.20,
        us_vol=0.20,
        jp_vol=0.20
    )
    assert abs(res['KR'] - 0.3333) < 1e-2
    assert abs(res['US'] - 0.3333) < 1e-2
    assert abs(res['JP'] - 0.3333) < 1e-2


def test_calculate_country_risk_parity_weights_skewed(allocator):
    # Lower JP volatility & higher score -> Higher weight for JP
    res = allocator.calculate_country_risk_parity_weights(
        kr_vol=0.30,
        us_vol=0.25,
        jp_vol=0.15,
        kr_score=0.8,
        us_score=1.0,
        jp_score=1.5
    )
    assert res['JP'] > res['KR']
    assert res['JP'] > res['US']
    assert abs(sum(res.values()) - 1.0) < 1e-5


def test_allocate_asian_session(allocator):
    market_state = {'kr_volatility': 0.18, 'us_volatility': 0.20, 'jp_volatility': 0.16}
    res = allocator.allocate_session_capital(
        current_time_kst='10:30:00',
        regime='bull',
        market_state=market_state,
        available_nav=100000000.0
    )
    assert res['session_name'] == 'ASIAN_DAYTIME_SESSION'
    assert res['country_allocations']['KR'] > 0
    assert res['country_allocations']['JP'] > 0
    assert res['country_allocations']['US'] == 0.0


def test_allocate_us_session(allocator):
    market_state = {'kr_volatility': 0.18, 'us_volatility': 0.20, 'jp_volatility': 0.16}
    res = allocator.allocate_session_capital(
        current_time_kst='23:00:00',
        regime='bull',
        market_state=market_state,
        available_nav=100000000.0
    )
    assert res['session_name'] == 'US_NIGHTTIME_SESSION'
    assert res['country_allocations']['US'] > 0
    assert res['country_allocations']['KR'] == 0.0
    assert res['country_allocations']['JP'] == 0.0


def test_allocate_weekend_sweep(allocator):
    market_state = {'is_weekend_sweep': True}
    res = allocator.allocate_session_capital(
        current_time_kst='16:00:00',
        regime='caution',
        market_state=market_state,
        available_nav=50000000.0
    )
    assert res['session_name'] == 'WEEKEND_HOLIDAY_SWEEP'
    assert res['country_allocations']['WEEKEND_SWEEP'] > 0
