"""
Unit Tests for Alpha 2 (OFI Velocity) and Alpha 3 (09:00:01 KST Disparity Engine)
"""
import pytest
from src.streams.s1_edge.ofi_velocity_engine import OFIVelocityEngine
from src.streams.s11_high_beta.first_second_disparity_engine import FirstSecondDisparityEngine

class TestAlpha2OFIVelocityEngine:
    def test_ofi_velocity_calculation(self):
        engine = OFIVelocityEngine()
        # Feed history
        for i in range(15):
            res = engine.compute_ofi_velocity(
                current_bid_qty=1000 + i * 10,
                prev_bid_qty=1000,
                current_ask_qty=500,
                prev_ask_qty=500,
                bid_price_delta=1.0,
                dt_seconds=1.0
            )
            assert 'ofi' in res
            assert 'ofi_velocity' in res
            assert 'z_velocity' in res

    def test_ofi_surge_detection(self):
        engine = OFIVelocityEngine()
        # Baseline low volatility
        for _ in range(20):
            engine.compute_ofi_velocity(100, 100, 100, 100, 0, 0, 1.0)
        # Extreme surge
        res = engine.compute_ofi_velocity(5000, 100, 100, 500, 1.0, -1.0, 0.5)
        assert res['is_surge'] is True
        assert res['z_velocity'] > 1.5

class TestAlpha3FirstSecondDisparityEngine:
    def test_neutral_disparity(self):
        engine = FirstSecondDisparityEngine()
        m_data = {
            'soxx_change_pct': 0.1,
            'cme_futures_change_pct': 0.1,
            'ndf_usdkrw_change_pct': 0.0,
            'krx_premarket_gap_pct': 0.1
        }
        res = engine.evaluate_disparity_signal(m_data)
        assert res['signal'] == 'neutral'

    def test_bullish_disparity_trigger(self):
        engine = FirstSecondDisparityEngine()
        m_data = {
            'soxx_change_pct': 2.5,          # Strong US semi rally
            'cme_futures_change_pct': 1.5,    # CME rally
            'ndf_usdkrw_change_pct': -0.5,   # FX drop (KRW strength)
            'krx_premarket_gap_pct': 0.2     # Small KRX gap
        }
        res = engine.evaluate_disparity_signal(m_data)
        assert res['signal'] == 'long'
        assert res['disparity_pct'] > 0.40
        assert res['confidence'] >= 0.80

    def test_bearish_disparity_trigger(self):
        engine = FirstSecondDisparityEngine()
        m_data = {
            'soxx_change_pct': -3.0,
            'cme_futures_change_pct': -2.0,
            'ndf_usdkrw_change_pct': 1.0,    # FX surge (KRW weakness)
            'krx_premarket_gap_pct': -0.1
        }
        res = engine.evaluate_disparity_signal(m_data)
        assert res['signal'] == 'short'
        assert res['disparity_pct'] < -0.40
        assert res['confidence'] >= 0.80
