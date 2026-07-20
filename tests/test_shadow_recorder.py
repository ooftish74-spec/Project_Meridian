#!/usr/bin/env python3
"""
test_shadow_recorder.py — ShadowRecorder 유닛 테스트
=====================================================

검증 항목:
  1. 기본 record() 호출 및 반환 구조
  2. Go/No-Go 평가 로직
  3. 일별 통계 조회
  4. 데이터 부족 시 안전 처리
"""

import os
import sys
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)


class TestShadowRecorder:
    """ShadowRecorder 기본 동작 테스트."""

    def setup_method(self):
        from src.measurement.shadow_recorder import ShadowRecorder
        self.recorder = ShadowRecorder()

    def test_init(self):
        """ShadowRecorder 초기화 성공."""
        assert self.recorder is not None

    def test_record_returns_result(self):
        """record()는 결과를 반환해야 한다."""
        orch_result = {
            'status': 'success',
            'regime': 'bull',
            'orders': [],
            'signals': {},
            'allocation': {'S1': 0.25, 'S2': 0.30, 'S3': 0.20, 'S4': 0.25},
        }
        result = self.recorder.record(orch_result)
        assert result is None or isinstance(result, dict)

    def test_record_with_orders(self):
        """주문 포함 record."""
        orders = [
            {
                'stream_id': 'S1',
                'ticker': '069500',
                'name': 'KODEX 200',
                'direction': 'long',
                'size_pct': 0.10,
                'confidence': 0.75,
            }
        ]
        orch_result = {
            'status': 'success',
            'regime': 'bull',
            'orders': orders,
            'signals': {'S1': orders},
            'allocation': {'S1': 0.25, 'S2': 0.30, 'S3': 0.20, 'S4': 0.25},
        }
        result = self.recorder.record(orch_result)
        assert result is None or isinstance(result, dict)

    def test_go_nogo_returns_dict(self):
        """go_nogo_evaluation()는 딕셔너리를 반환해야 한다."""
        result = self.recorder.go_nogo_evaluation()
        assert isinstance(result, dict)

    def test_go_nogo_has_verdict(self):
        """Go/No-Go에 verdict 키가 있어야 한다."""
        result = self.recorder.go_nogo_evaluation()
        assert 'verdict' in result

    def test_go_nogo_valid_verdicts(self):
        """verdict는 정의된 값 중 하나여야 한다."""
        result = self.recorder.go_nogo_evaluation()
        valid_verdicts = {'GO', 'CONDITIONAL_GO', 'NO_GO', 'INSUFFICIENT_DATA'}
        assert result['verdict'] in valid_verdicts

    def test_get_daily_stats(self):
        """get_daily_stats()는 리스트 또는 딕셔너리를 반환해야 한다."""
        stats = self.recorder.get_daily_stats()
        assert isinstance(stats, (list, dict))


class TestShadowRecorderGoNoGo:
    """Go/No-Go 판정 로직 테스트."""

    def setup_method(self):
        from src.measurement.shadow_recorder import ShadowRecorder
        self.recorder = ShadowRecorder()

    def test_insufficient_data(self):
        """데이터 부족 시 INSUFFICIENT_DATA 반환."""
        import unittest.mock as mock
        
        def mock_cfg_get(key, default=None):
            if key == 'gonogo.shadow_min_days':
                return 100
            if key == 'go.sharpe.ok':
                return 0.5
            return default

        with mock.patch('src.measurement.shadow_recorder.cfg.get', side_effect=mock_cfg_get), \
             mock.patch.object(self.recorder, '_compute_daily_returns', return_value=[0.01]*5):
            result = self.recorder.go_nogo_evaluation()
            assert result['verdict'] == 'INSUFFICIENT_DATA'

    def test_go_nogo_stability(self):
        """연속 호출 시 일관된 결과."""
        r1 = self.recorder.go_nogo_evaluation()
        r2 = self.recorder.go_nogo_evaluation()
        assert r1['verdict'] == r2['verdict']
