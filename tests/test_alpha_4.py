"""
Unit Tests for Alpha 4 (S3 Track C Cross-Asset Macro Sleeve)
"""
import pytest
from src.streams.s3_active_macro.cross_asset_sleeve import S3CrossAssetSleeve
from src.streams.s3_active_macro.active_macro_stream import S3FactorStream

class TestAlpha4CrossAssetSleeve:
    def test_cross_asset_sleeve_outperformance(self):
        sleeve = S3CrossAssetSleeve()
        m_data = {
            'spy_return_5d': -0.02, # SPY dropping -2%
            'gld_return_5d': 0.03,  # Gold rallying +3%
            'uso_return_5d': 0.01,  # Oil +1%
            'cper_return_5d': -0.03, # Copper dropping
            'tlt_return_5d': 0.02,  # Treasury +2%
            'uup_return_5d': 0.01   # Dollar +1%
        }
        signals = sleeve.generate_track_c_signals(regime='caution', market_data=m_data)

        assert len(signals) >= 3
        tickers = {s['ticker'] for s in signals}
        assert 'GLD' in tickers
        assert 'TLT' in tickers
        assert 'USO' in tickers

        for s in signals:
            assert s['stream_id'] == 'S3_C'
            assert s['market'] == 'US'
            assert s['tax_drag_pct'] == 0.00
            assert s['strategy'] == 's3_cross_asset_momentum'

    def test_flight_to_quality_in_bear_regime(self):
        sleeve = S3CrossAssetSleeve()
        m_data = {
            'spy_return_5d': -0.05,
            'gld_return_5d': 0.005,
            'tlt_return_5d': 0.005
        }
        signals = sleeve.generate_track_c_signals(regime='bear', market_data=m_data)
        tickers = {s['ticker'] for s in signals}
        assert 'GLD' in tickers or 'TLT' in tickers

    def test_s3_stream_integration_includes_track_c(self):
        s3 = S3FactorStream()
        m_data = {
            'spy_return_5d': -0.03,
            'gld_return_5d': 0.02,
            'tlt_return_5d': 0.015,
            'signal_cache': {'vix': 20.0, 'vkospi': 20.0}
        }
        signals = s3.generate_signals(regime='caution', market_data=m_data)
        track_c_sigs = [s for s in signals if s.get('stream_id') == 'S3_C']
        assert len(track_c_sigs) >= 1
        for sig in track_c_sigs:
            assert sig['_type'] == 'TRACK_C'
            assert sig['market'] == 'US'
