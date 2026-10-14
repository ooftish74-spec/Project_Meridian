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

def test_daytime_vs_night_futures_isolation(monkeypatch, tmp_path):
    from src.data_collection.central_data_gateway import CentralDataGateway
    from src.data_collection.kis_data_collector import KISDataCollector
    monkeypatch.setattr(KISDataCollector, "get_current_price", lambda self, code: {"price": 1008.25})
    
    # Mock existing night quote with today's timestamp
    import src.data_collection.central_data_gateway as cdg
    import json
    night_f = tmp_path / "krx_futures_overnight.json"
    with open(night_f, "w") as f:
        json.dump({"close": 961.00, "source": "KRX_Night_Session", "timestamp": datetime.now().isoformat()}, f)
    monkeypatch.setattr(cdg, "_RESULTS_DIR", tmp_path)
    monkeypatch.setattr(cdg, "_PROJECT_ROOT", tmp_path)

    gw = CentralDataGateway()
    res = gw.get_overnight_futures()
    assert res is not None
    assert "close" in res
    assert "daytime_close" in res
    # When night session quote is 961.00 and daytime close is 1008.25, they must not be conflated
    assert res["daytime_close"] == 1008.25
    assert res["close"] == 961.00
    assert res["change"] == round(961.00 - 1008.25, 2)

def test_to_kis_code_resolution():
    assert DynamicFuturesContractResolver.to_kis_code("F202612") == "101V12"
    assert DynamicFuturesContractResolver.to_kis_code("f202612") == "101V12"
    assert DynamicFuturesContractResolver.to_kis_code("10100000") == "10100000"
    assert DynamicFuturesContractResolver.to_kis_code("101V12") == "101V12"

