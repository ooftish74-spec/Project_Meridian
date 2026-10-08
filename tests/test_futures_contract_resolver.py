"""
Unit tests for DynamicFuturesContractResolver
"""

from datetime import datetime
import pytest
from src.data_collection.futures_contract_resolver import DynamicFuturesContractResolver, get_second_thursday

def test_second_thursday_calc():
    # September 2026 2nd Thursday check
    # Sept 1 2026 is Tuesday. Thursdays: Sept 3 (1st), Sept 10 (2nd)
    dt = get_second_thursday(2026, 9)
    assert dt.day == 10
    assert dt.month == 9

def test_front_month_resolution():
    # August 21, 2026 (Before Sept 10 2026 expiry) -> F202609
    ref = datetime(2026, 8, 21, 6, 50)
    code = DynamicFuturesContractResolver.get_current_front_month_code(ref)
    assert code == "F202609"

def test_front_month_rollover_after_expiry():
    # Sept 11, 2026 (After Sept 10 2026 expiry) -> F202612
    ref = datetime(2026, 9, 11, 9, 0)
    code = DynamicFuturesContractResolver.get_current_front_month_code(ref)
    assert code == "F202612"

def test_december_rollover_to_next_year():
    # Dec 15, 2026 (After Dec 10 2026 expiry) -> F202703
    ref = datetime(2026, 12, 15, 9, 0)
    code = DynamicFuturesContractResolver.get_current_front_month_code(ref)
    assert code == "F202703"
