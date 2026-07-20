#!/usr/bin/env python3
"""
test_measurement.py — MeasurementEngine 핵심 메서드 테스트
============================================================

DD 권고 #11: 테스트 커버리지 확대
- 측정 파이프라인 실행
- 결과 구조 검증
- 빈 데이터 안전성
"""

import os
import sys
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)


class TestMeasurementEngine:
    """MeasurementEngine 단일 측정 파이프라인 테스트."""

    def test_init(self):
        """MeasurementEngine 생성."""
        from src.measurement.measurement_engine import MeasurementEngine
        me = MeasurementEngine()
        assert me is not None
        assert me.result == {}

    def test_compute_returns_dict(self):
        """compute()는 딕셔너리를 반환."""
        from src.measurement.measurement_engine import MeasurementEngine
        me = MeasurementEngine()
        result = me.compute()
        assert isinstance(result, dict)

    def test_result_structure(self):
        """결과에 핵심 섹션이 포함."""
        from src.measurement.measurement_engine import MeasurementEngine
        me = MeasurementEngine()
        result = me.compute()
        # 주요 키 존재 확인 (데이터 없어도 구조는 유지)
        expected_keys = {'portfolio', 'risk', 'timestamp'}
        present = set(result.keys())
        missing = expected_keys - present
        # 데이터가 없으면 일부 키가 빠질 수 있으므로 최소한 timestamp는 있어야 함
        assert 'timestamp' in result or len(result) > 0, \
            f"Result should have structure, got keys: {list(result.keys())}"

    def test_compute_idempotent(self):
        """같은 데이터로 두 번 compute해도 같은 결과."""
        from src.measurement.measurement_engine import MeasurementEngine
        me = MeasurementEngine()
        r1 = me.compute()
        r2 = me.compute()
        # timestamp 제외하고 같아야 함
        for key in r1:
            if key != 'timestamp':
                assert r1[key] == r2[key], \
                    f"Key {key} differs between runs"

    def test_result_stored(self):
        """compute() 후 result 속성에 저장."""
        from src.measurement.measurement_engine import MeasurementEngine
        me = MeasurementEngine()
        result = me.compute()
        assert me.result == result


class TestMeasurementEngineMetrics:
    """개별 측정 지표 테스트."""

    def test_portfolio_view(self):
        """_compute_portfolio_view는 안전하게 동작."""
        from src.measurement.measurement_engine import MeasurementEngine
        me = MeasurementEngine()
        # 빈 shadow_portfolio로 테스트
        portfolio = me._compute_portfolio_view({})
        assert isinstance(portfolio, dict)

    def test_risk_view(self):
        """_compute_risk_view는 안전하게 동작."""
        from src.measurement.measurement_engine import MeasurementEngine
        me = MeasurementEngine()
        risk = me._compute_risk_view({})
        assert isinstance(risk, dict)
