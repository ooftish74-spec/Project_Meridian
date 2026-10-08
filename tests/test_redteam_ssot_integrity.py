"""
Project Meridian — Red Team SSoT Integrity & Resilience Test Suite
=====================================================================
Verify mathematical robustness of OvernightSSoTManager & OIS Gatekeeper.
"""

import os
import sys
import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.intelligence.overnight_ssot_manager import OvernightSSoTManager, OvernightDataRecord, SSOTValidationException
from src.intelligence.overnight_intelligence import OvernightIntelligenceScore

@pytest.fixture
def ssot_manager(tmp_path):
    mgr = OvernightSSoTManager()
    mgr.SSOT_FILE = tmp_path / "overnight_market_ssot.json"
    return mgr

def test_stale_date_rejection_trigger(ssot_manager):
    """Red Team Test 1: Verify that yesterday's stale record triggers automatic rebuild."""
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    stale_record = OvernightDataRecord(
        trading_date=yesterday_str,
        updated_at=(datetime.now() - timedelta(hours=25)).isoformat(),
        night_futures_close=1088.9,
        night_futures_change_pct=-3.44, # Stale negative value
        sp500_change_1d=-0.75,
        nasdaq_change_1d=-1.07,
        sox_change_1d=-2.08,
        vix=16.04,
        usdkrw=1352.50,
        us10y=5.213,
        status="STALE_OLD"
    )
    
    # Save stale record
    with open(ssot_manager.SSOT_FILE, "w", encoding="utf-8") as f:
        json.dump(stale_record.to_dict(), f, indent=2)

    # Fetching without force_refresh MUST reject stale record and rebuild today's record
    today_str = datetime.now().strftime("%Y-%m-%d")
    with patch.object(ssot_manager, 'rebuild_and_sync_ssot') as mock_fetch:
        mock_fetch.return_value = OvernightDataRecord(
            trading_date=today_str,
            updated_at=datetime.now().isoformat(),
            night_futures_close=1095.0,
            night_futures_change_pct=0.95,
            sp500_change_1d=0.5,
            nasdaq_change_1d=0.8,
            sox_change_1d=1.2,
            vix=15.5,
            usdkrw=1350.0,
            us10y=4.2,
            status="VALIDATED_LIVE"
        )
        verified = ssot_manager.get_verified_overnight_data(force_refresh=False)
        assert verified.trading_date == today_str
        assert verified.night_futures_change_pct == 0.95
        assert verified.status == "VALIDATED_LIVE"

def test_corrupt_json_self_healing(ssot_manager):
    """Red Team Test 2: Verify self-healing when SSoT file is corrupted or truncated."""
    with open(ssot_manager.SSOT_FILE, "w", encoding="utf-8") as f:
        f.write("{ corrupt json truncated ...")

    # Gatekeeper must recover seamlessly without crashing
    verified = ssot_manager.get_verified_overnight_data(force_refresh=False)
    assert verified.night_futures_close > 0
    assert verified.vix > 0

def test_ois_calculation_from_verified_ssot():
    """Red Team Test 3: Verify OvernightIntelligenceScore reads accurate values."""
    ois_engine = OvernightIntelligenceScore()
    result = ois_engine.calculate(include_premarket=True)
    
    assert 0.0 <= result["ois"] <= 100.0
    assert result["sentiment"] in ["strong_bullish", "bullish", "neutral", "bearish", "strong_bearish"]
    assert "components" in result
