"""
[Phase 70] Unit test for Primary Source Fail-Fast Diagnostic Protocol and Monthly Macro Refresh Policy.
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from src.utils.vendor_multiplexer import VendorMultiplexer, DataQualityException
from src.data_collection.macro_collector import MacroCollector
from src.utils.data_imputer import OrthogonalDataImputer, DataNoGoException


def test_vendor_multiplexer_google_finance_mapping():
    """Verify Google Finance mapping keys in VendorMultiplexer."""
    vmx = VendorMultiplexer()
    # Mocking Google Finance urllib fetch
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = b'<div class="YMlKec">15.42</div>'
        mock_urlopen.return_value = mock_response

        series = vmx.fetch('^SKEW', '2026-09-01', '2026-09-25')
        assert series is not None
        assert not series.empty


def test_macro_collector_monthly_ffill():
    """Verify ffill(limit=35) in MacroCollector."""
    mc = MacroCollector()
    with patch.object(mc, '_fetch_hy_spread') as mock_hy, \
         patch.object(mc, '_fetch_copper_gold') as mock_cg, \
         patch.object(mc, '_fetch_cboe_skew') as mock_skew, \
         patch.object(mc, '_fetch_gscpi') as mock_gscpi:

        idx = pd.date_range('2026-08-01', '2026-09-25', freq='B')
        mock_hy.return_value = pd.Series(5.0, index=idx, name='high_yield_spread')
        mock_cg.return_value = pd.Series(0.003, index=idx, name='copper_gold_ratio')
        mock_skew.return_value = pd.Series(120.0, index=idx, name='cboe_skew')
        
        # Monthly series with missing values after 5 days
        s_gscpi = pd.Series(index=idx, dtype=float)
        s_gscpi.iloc[0] = 0.5
        mock_gscpi.return_value = s_gscpi

        df = mc.collect_all()
        assert not df.empty
        # Check that index 10 is ffilled (with limit 35)
        assert not pd.isna(df['gscpi'].iloc[10])


def test_macro_collector_primary_fail_logging(caplog):
    """Verify [Phase 70 PRIMARY_FAIL] log output when 1st primary source fails."""
    mc = MacroCollector()
    with patch.object(mc._vmx, 'fetch', side_effect=DataQualityException("[Phase 70] SKEW: 모든 벤더 실패")):
        start = datetime.today() - timedelta(days=30)
        end = datetime.today()
        series = mc._fetch_cboe_skew(start, end)
        assert series.isna().all()
        assert "[Phase 70 PRIMARY_FAIL] cboe_skew" in caplog.text
