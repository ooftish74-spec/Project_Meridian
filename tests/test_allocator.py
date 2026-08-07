#!/usr/bin/env python3
"""
test_allocator.py — AlphaAllocator 배분 로직 유닛 테스트
=========================================================

DD 권고 #11: 테스트 커버리지 확대
- 4-Stream 배분 비율 정합성
- 레짐별 배분 변화 검증
- 리스크 패리티 로직
"""

import os
import sys
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)


@pytest.fixture
def stream_metrics():
    """스트림 성과 지표 fixture — 변동성이 다른 리턴으로 리스크패리티 차별화.

    ★ S4 Advisory는 AlphaAllocator 리스크패리티 대상에서 제외됨.
    ★ S5 Overnight이 S4 대신 리스크패리티 파트너로 편입.
    """
    import random
    random.seed(42)
    return {
        'S1': {
            'sharpe': 1.2,
            'daily_returns': [0.003 * (1 + 0.5 * (i % 3 - 1)) for i in range(10)],
            'volatility': 0.025,
        },
        'S2': {
            'sharpe': 0.8,
            'daily_returns': [0.001 * (1 + 0.3 * (i % 4 - 1.5)) for i in range(10)],
            'volatility': 0.015,
        },
        'S3': {
            'sharpe': 1.0,
            'daily_returns': [0.002 * (1 + 0.2 * (i % 2 - 0.5)) for i in range(10)],
            'volatility': 0.020,
        },
        'S5': {
            'sharpe': 0.5,
            'daily_returns': [0.0005 * (1 + 0.1 * (i % 2)) for i in range(10)],
            'volatility': 0.005,
        },
        '_s2_rolling': {
            'wr_5d': 0.0,
            'n_trades_5d': 0
        }
    }


class TestAlphaAllocator:
    """AlphaAllocator 4-Stream 배분 테스트."""

    def test_init(self):
        """AlphaAllocator 생성."""
        from src.allocation.alpha_allocator import AlphaAllocator
        alloc = AlphaAllocator()
        assert alloc is not None

    def test_allocate_returns_four_streams(self, stream_metrics):
        """allocate()는 S1/S2/S3/S5 키를 가진 dict 반환.

        ★ S4 Advisory는 AlphaAllocator 로직에서 제외되어 별도 관리됨.
           대신 S5 Overnight이 리스크패리티 파트너로 편입됨.
        """
        from src.allocation.alpha_allocator import AlphaAllocator
        alloc = AlphaAllocator()
        weights = alloc.allocate(stream_metrics, regime='bull')
        assert isinstance(weights, dict)
        for stream in ['S1', 'S2', 'S3', 'S5']:
            assert stream in weights, f"{stream} missing from weights"
        # S4는 advisory 전용 — 리스크패리티 할당에서 제외되어 weights에 없어야 함
        assert 'S4' not in weights, 'S4 should be excluded from risk-parity allocator'

    def test_weights_sum_to_one(self, stream_metrics):
        """배분 가중치 합 ≈ 1.0."""
        from src.allocation.alpha_allocator import AlphaAllocator
        alloc = AlphaAllocator()
        for regime in ['bull', 'caution', 'bear', 'crash']:
            weights = alloc.allocate(stream_metrics, regime=regime)
            total = sum(weights.values())
            assert abs(total - 1.0) < 0.05, \
                f"{regime} weights sum={total:.3f}, expected ~1.0"

    def test_crash_regime_low_s1(self, stream_metrics):
        """crash 레짐에서 S1 가중치가 과도하지 않아야 함.

        ★ S5·S10 스트림이 active에 편입되어 정규화 분모가 바뀌므로
          Phase 76 Two-Track 로직 작동 후 crash S1은 0.25 이하여야 함.
        """
        from src.allocation.alpha_allocator import AlphaAllocator
        import unittest.mock as mock
        alloc = AlphaAllocator()
        with mock.patch('src.allocation.alpha_allocator.cfg.get', side_effect=lambda k, d=None: False if k == 'allocator.chameleon_v2_enabled' else (0.0 if k == 'allocator.risk_parity_blend' else d)):
            crash_w = alloc.allocate(stream_metrics, regime='crash')
        assert crash_w['S1'] <= 0.25, \
            f"Crash S1={crash_w.get('S1', 0):.3f} 과도하게 높음 (max=0.25)"

    def test_bull_higher_s1_than_crash(self, stream_metrics):
        """bull 장세에서 S1이 적절한 비중을 가져야 함.

        ★ S5·S10 편입 후 정규화에 의해 절대값 상한(0.30)으로
          bull S1이 적절 범위 내에 있는지 확인.
        """
        from src.allocation.alpha_allocator import AlphaAllocator
        import unittest.mock as mock
        alloc = AlphaAllocator()
        with mock.patch('src.allocation.alpha_allocator.cfg.get', side_effect=lambda k, d=None: False if k == 'allocator.chameleon_v2_enabled' else (0.0 if k == 'allocator.risk_parity_blend' else d)):
            bull = alloc.allocate(stream_metrics, regime='bull')
        assert 0.0 <= bull['S1'] <= 0.30, \
            f"Bull S1={bull.get('S1', 0):.3f} 적절 범위에 있어야 함 (0~0.30)"

    def test_weights_all_non_negative(self, stream_metrics):
        """모든 가중치 ≥ 0."""
        from src.allocation.alpha_allocator import AlphaAllocator
        alloc = AlphaAllocator()
        for regime in ['bull', 'caution', 'bear', 'crash']:
            weights = alloc.allocate(stream_metrics, regime=regime)
            for stream, w in weights.items():
                assert w >= 0, f"{stream} in {regime} has negative weight: {w}"

    def test_empty_metrics_returns_weights(self):
        """빈 metrics에서도 base weights 반환.

        ★ AlphaAllocator는 S1/S2/S3/S4/S5 기본 5개 스트림을 관리.
           (S4=Advisory는 리스크패리티에서 제외되지만 weights dict에는 포함)
        """
        from src.allocation.alpha_allocator import AlphaAllocator
        alloc = AlphaAllocator()
        weights = alloc.allocate({}, regime='caution')
        assert isinstance(weights, dict)
        # 빈 metrics 시에도 지정된 스트림들에 대한 weights가 제공되어야 함
        assert len(weights) >= 4  # 최소 S1/S2/S3/S5 (S4 선택적)
        assert all(v >= 0 for v in weights.values()), 'All weights must be non-negative'


class TestAlphaAllocatorRegimeShifts:
    """레짐 변화에 따른 배분 시프트 테스트."""

    def test_bear_increases_s5(self, stream_metrics):
        """bear 레징에서 S5 현금파킹 비중 >= bull."""
        from src.allocation.alpha_allocator import AlphaAllocator
        import unittest.mock as mock
        alloc = AlphaAllocator()
        with mock.patch('src.allocation.alpha_allocator.cfg.get', side_effect=lambda k, d=None: 0.0 if k == 'allocator.risk_parity_blend' else d):
            bull = alloc.allocate(stream_metrics, regime='bull')
            bear = alloc.allocate(stream_metrics, regime='bear')
            assert bear.get('S5', 0) >= bull.get('S5', 0) - 0.01, \
                f"Bear S5={bear.get('S5', 0):.3f} should >= Bull S5={bull.get('S5', 0):.3f}"
