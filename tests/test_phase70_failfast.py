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
    """Verify Google Finance mapping keys in VendorMultiplexer.

    ★ Wave 1 (Track B) 격리: 기존 테스트는 `urllib.request.urlopen` 을 mock 했지만 GoogleFinanceCollector 는
    `requests.get` 을 사용하므로 mock 이 무효였고, 실제로는 네트워크의 Alpha Vantage(^SKEW→VIXY 프록시)
    응답 덕에 통과하고 있었다(오프라인에서 실패). 실제 의존성(requests.get)을 결정적으로 mock 하고
    KIS/Alpha Vantage 경로는 비활성화하여, 시리즈가 Google Finance 경로에서 나왔는지까지 검증한다.
    """
    vmx = VendorMultiplexer()
    html = '<div data-last-price="150.42" data-previous-close="148.10"></div>'
    fake_resp = MagicMock(status_code=200, text=html)
    with patch('src.utils.google_finance_collector.requests.get', return_value=fake_resp) as mock_get, \
         patch('src.data_collection.kis_data_collector.KISDataCollector', side_effect=RuntimeError('kis disabled in test')), \
         patch('src.utils.credential_manager.CredentialManager.read_from_env', return_value=None):
        series = vmx.fetch('^SKEW', '2026-09-01', '2026-09-25')
        assert series is not None
        assert not series.empty
        # Google Finance 경로 검증: quote URL 호출 + T-0 가격/전일종가 반영
        assert mock_get.called
        assert 'google.com/finance/quote/' in mock_get.call_args[0][0]
        assert series.iloc[-1] == pytest.approx(150.42)
        assert series.iloc[0] == pytest.approx(148.10)
        assert series.index[-1] <= pd.Timestamp('2026-09-25')


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
