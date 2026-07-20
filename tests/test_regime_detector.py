#!/usr/bin/env python3
"""
test_regime_detector.py — RegimeDetector 유닛 테스트
=====================================================

검증 항목:
  1. 기본 detect() 호출 및 반환 구조
  2. Bull / Bear / Caution 레짐 판정 정확성
  3. 시장 데이터 입력에 따른 동적 판정
  4. 에지 케이스 (빈 데이터, None 등)
"""

import os
import sys
import pytest

# 프로젝트 루트 경로 설정
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)


class TestRegimeDetector:
    """RegimeDetector 기본 동작 테스트."""

    def setup_method(self):
        from src.regime.regime_detector import RegimeDetector
        self.detector = RegimeDetector()

    def test_detect_returns_dict(self):
        """detect()는 딕셔너리를 반환해야 한다."""
        result = self.detector.detect(market_data={})
        assert isinstance(result, dict)

    def test_detect_has_required_keys(self):
        """반환에 regime, confidence, method 키가 있어야 한다."""
        result = self.detector.detect(market_data={})
        assert 'regime' in result
        assert 'confidence' in result
        assert 'method' in result

    def test_regime_value_is_valid(self):
        """regime은 bull/caution/bear/crash 중 하나여야 한다."""
        result = self.detector.detect(market_data={})
        assert result['regime'] in ('bull', 'caution', 'bear', 'crash')

    def test_confidence_range(self):
        """confidence는 0~1 사이여야 한다."""
        result = self.detector.detect(market_data={})
        assert 0 <= result['confidence'] <= 1

    def test_detect_with_bull_data(self):
        """VIX 낮고 KOSPI 상승세인 데이터 → bull 또는 caution."""
        market_data = {
            'signal_cache': {
                'vix': 12.0,
                'us10y': 4.0,
                'usdkrw': 1300.0,
                'sp500': 5000.0,
                'kospi_trend': 'up',
                'kospi_ma20_dist': 5.0,
                'kospi_volatility': 10.0,
            }
        }
        result = self.detector.detect(market_data=market_data)
        assert result['regime'] in ('bull', 'caution')

    def test_detect_with_crash_data(self):
        """VIX 40+, 추세 하락 → bear 또는 crash."""
        market_data = {
            'signal_cache': {
                'vix': 45.0,
                'vkospi': 46.0,
                'us10y': 4.0,
                'usdkrw': 1300.0,
                'sp500': 5000.0,
                'kospi_trend': 'down',
                'kospi_ma20_dist': -10.0,
                'kospi_volatility': 60.0,
            }
        }
        result = self.detector.detect(market_data=market_data)
        assert result['regime'] in ('bear', 'crash')

    def test_detect_with_empty_data(self):
        """빈 market_data에서도 에러 없이 동작해야 한다."""
        result = self.detector.detect(market_data={})
        assert result['regime'] is not None

    def test_detect_with_none_values(self):
        """None 값이 포함된 데이터에서 TypeError 발생 (알려진 제한사항)."""
        market_data = {
            'signal_cache': {
                'vix': None,
                'us10y': 4.0,
                'usdkrw': 1300.0,
                'sp500': 5000.0,
                'kospi_trend': None,
            }
        }
        # RegimeDetector는 누락된 데이터에 대해 유연하게 대처 (e.g. Caution 전환)
        res = self.detector.detect(market_data=market_data)
        assert res is not None
        assert 'regime' in res


class TestRegimeDetectorEdgeCases:
    """RegimeDetector 경계 조건 테스트."""

    def setup_method(self):
        from src.regime.regime_detector import RegimeDetector
        self.detector = RegimeDetector()

    def test_multiple_calls_consistent(self):
        """같은 데이터로 연속 호출 시 일관된 결과."""
        data = {'signal_cache': {'vix': 20.0}}
        r1 = self.detector.detect(market_data=data)
        r2 = self.detector.detect(market_data=data)
        assert r1['regime'] == r2['regime']

    def test_extreme_vix(self):
        """VIX 극단값에서도 에러 없이 동작."""
        for vix_val in [0, 5, 80, 100]:
            data = {'signal_cache': {'vix': vix_val}}
            result = self.detector.detect(market_data=data)
            assert result['regime'] in ('bull', 'caution', 'bear', 'crash')
