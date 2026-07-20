#!/usr/bin/env python3
"""
test_execution_engine.py — ExecutionEngine 유닛 테스트
======================================================

검증 항목:
  1. Shadow 모드 체결 로직
  2. ExecutionResult 반환 구조
  3. 체결률 계산
  4. 에러 핸들링
"""

import os
import sys
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)


class TestExecutionEngineShadow:
    """Shadow 모드 ExecutionEngine 테스트."""

    def setup_method(self):
        from src.execution.execution_engine import ExecutionEngine
        self.engine = ExecutionEngine(mode='shadow')

    def test_shadow_mode_init(self):
        """Shadow 모드로 초기화 가능해야 한다."""
        assert self.engine is not None

    def test_execute_returns_result(self):
        """execute()는 ExecutionResult를 반환해야 한다."""
        orders = [
            {
                'stream_id': 'S1',
                'ticker': '069500',
                'direction': 'long',
                'size_pct': 0.10,
                'confidence': 0.75,
            }
        ]
        result = self.engine.execute(orders)
        assert hasattr(result, 'n_filled')
        assert hasattr(result, 'n_orders')
        assert hasattr(result, 'mode')

    def test_execute_has_required_attrs(self):
        """반환에 n_filled, n_orders, mode 속성이 있어야 한다."""
        orders = [
            {
                'stream_id': 'S1',
                'ticker': '069500',
                'direction': 'long',
                'size_pct': 0.05,
                'confidence': 0.60,
            }
        ]
        result = self.engine.execute(orders)
        assert isinstance(result.n_filled, int)
        assert isinstance(result.n_orders, int)
        assert isinstance(result.mode, str)

    def test_shadow_mode_label(self):
        """Shadow 모드에서 mode='shadow'로 반환."""
        result = self.engine.execute([])
        assert result.mode == 'shadow'

    def test_execute_empty_orders(self):
        """빈 주문 리스트에서도 에러 없이 동작."""
        result = self.engine.execute([])
        assert result.n_orders == 0

    def test_execute_multiple_orders(self):
        """복수 주문 실행."""
        orders = [
            {
                'stream_id': 'S1',
                'ticker': '069500',
                'direction': 'long',
                'size_pct': 0.05,
                'confidence': 0.70,
            },
            {
                'stream_id': 'S2',
                'ticker': '005930',
                'direction': 'long',
                'size_pct': 0.03,
                'confidence': 0.65,
            },
            {
                'stream_id': 'S3',
                'ticker': '091160',
                'direction': 'long',
                'size_pct': 0.04,
                'confidence': 0.80,
            },
        ]
        result = self.engine.execute(orders)
        assert result.n_orders == 3
        assert result.n_filled <= result.n_orders

    def test_fill_rate_within_bounds(self):
        """체결률은 0~100% 범위."""
        orders = [
            {
                'stream_id': 'S1',
                'ticker': '069500',
                'direction': 'long',
                'size_pct': 0.10,
                'confidence': 0.75,
            }
        ]
        result = self.engine.execute(orders)
        if result.n_orders > 0:
            fill_rate = result.n_filled / result.n_orders
            assert 0 <= fill_rate <= 1.0

    def test_to_dict(self):
        """to_dict() 메서드가 딕셔너리를 반환."""
        result = self.engine.execute([])
        d = result.to_dict()
        assert isinstance(d, dict)
        assert 'mode' in d
        assert 'n_orders' in d


class TestExecutionEngineEdgeCases:
    """ExecutionEngine 경계 조건 테스트."""

    def setup_method(self):
        from src.execution.execution_engine import ExecutionEngine
        self.engine = ExecutionEngine(mode='shadow')

    def test_zero_size_order(self):
        """size_pct=0인 주문도 안전하게 처리."""
        orders = [
            {
                'stream_id': 'S1',
                'ticker': '069500',
                'direction': 'long',
                'size_pct': 0.0,
                'confidence': 0.50,
            }
        ]
        result = self.engine.execute(orders)
        assert hasattr(result, 'n_orders')

    def test_very_small_order(self):
        """극소량 주문도 에러 없이 처리."""
        orders = [
            {
                'stream_id': 'S4',
                'ticker': '133690',
                'direction': 'long',
                'size_pct': 0.001,
                'confidence': 0.90,
            }
        ]
        result = self.engine.execute(orders)
        assert hasattr(result, 'n_orders')
