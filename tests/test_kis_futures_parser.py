"""
Unit tests for parse_kis_futures_output in kis_data_collector.py
"""
import pytest
from src.data_collection.kis_data_collector import parse_kis_futures_output

def test_parse_kis_futures_output_standard():
    data = {
        "output3": {
            "bstp_nmix_prpr": "1050.55",
            "bstp_nmix_prdy_vrss": "10.60",
            "bstp_nmix_prdy_ctrt": "1.00",
            "bstp_nmix_prdy_vrss_sign": "5"  # 하락
        }
    }
    price, change, change_pct = parse_kis_futures_output(data)
    assert price == 1050.55
    assert change == -10.60
    assert change_pct == -1.00

def test_parse_kis_futures_output_fallback_prev_close():
    # When prdy_ctrt is 0.00 but previous close and price exist
    data = {
        "output": {
            "futs_prpr": "1050.55",
            "prdy_clpr": "1061.15",
            "prdy_vrss_sign": "5"
        }
    }
    price, change, change_pct = parse_kis_futures_output(data)
    assert price == 1050.55
    assert change == -10.60
    assert change_pct == pytest.approx(-0.9989, abs=0.01)

def test_parse_kis_futures_output_positive():
    data = {
        "output1": {
            "futs_prpr": "1070.00",
            "futs_prdy_vrss": "10.00",
            "futs_prdy_ctrt": "0.9434",
            "prdy_vrss_sign": "2"  # 상승
        }
    }
    price, change, change_pct = parse_kis_futures_output(data)
    assert price == 1070.00
    assert change == 10.00
    assert change_pct == 0.9434

def test_parse_kis_futures_output_empty():
    price, change, change_pct = parse_kis_futures_output({})
    assert price == 0.0
    assert change == 0.0
    assert change_pct == 0.0

def test_parse_kis_futures_output_night_session_baseline():
    # Real KRX Night Futures scenario:
    # output3 has night traded price 936.82, but 0.00 change
    # output1 has daytime price 944.20 or daytime baseline 944.80
    data = {
        "output1": {
            "hts_kor_isnm": "F 202612",
            "futs_prpr": "944.20",
            "futs_sdpr": "944.20",
            "futs_prdy_vrss": "0.00",
            "futs_prdy_ctrt": "0.00"
        },
        "output3": {
            "bstp_cls_code": "2001",
            "hts_kor_isnm": "KOSPI200",
            "bstp_nmix_prpr": "936.82",
            "bstp_nmix_prdy_vrss": "0.00",
            "bstp_nmix_prdy_ctrt": "0.00",
            "prdy_vrss_sign": "3"
        }
    }
    price, change, change_pct = parse_kis_futures_output(data, daytime_baseline=944.80)
    assert price == 936.82
    assert change == -7.98
    assert change_pct == pytest.approx(-0.8446, abs=0.01)
