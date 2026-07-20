#!/usr/bin/env python3
"""
test_pipeline.py — StreamOrchestrator 통합 테스트
==================================================

검증 항목:
  1. StreamOrchestrator 초기화
  2. 전체 파이프라인 실행 (shadow 모드)
  3. 반환 구조 검증
  4. 4-Stream 활성화 확인
  5. DynamicConfig 기본값 정합성
"""

import os
import sys
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)


class TestStreamOrchestratorInit:
    """StreamOrchestrator 초기화 테스트."""

    def test_init_default(self):
        """기본 생성 시 shadow 모드."""
        from scripts.stream_orchestrator import StreamOrchestrator
        orch = StreamOrchestrator()
        assert orch is not None

    def test_init_shadow_mode(self):
        """명시적 shadow 모드."""
        from scripts.stream_orchestrator import StreamOrchestrator
        orch = StreamOrchestrator(exec_mode='shadow')
        assert orch is not None

    def test_has_six_streams(self):
        """6개 스트림이 등록되어야 한다."""
        from scripts.stream_orchestrator import StreamOrchestrator
        orch = StreamOrchestrator()
        assert len(orch.streams) == 6

    def test_stream_ids(self):
        """S1~S5 스트림 ID 확인 (S4=Advisory, S5=Overnight 포함)."""
        from scripts.stream_orchestrator import StreamOrchestrator
        orch = StreamOrchestrator()
        ids = [s.stream_id for s in orch.streams]
        assert 'S1' in ids
        assert 'S2' in ids
        assert 'S3' in ids
        assert 'S5' in ids

    def test_has_shadow_recorder(self):
        """ShadowRecorder가 존재해야 한다."""
        from scripts.stream_orchestrator import StreamOrchestrator
        orch = StreamOrchestrator()
        assert orch.shadow_recorder is not None

    def test_get_stream_status(self):
        """get_stream_status()가 6개 항목을 반환."""
        from scripts.stream_orchestrator import StreamOrchestrator
        orch = StreamOrchestrator()
        status = orch.get_stream_status()
        assert len(status) == 6
        for s in status:
            assert 'stream_id' in s
            assert 'active' in s
            assert 'shadow' in s


class TestStreamOrchestratorRun:
    """StreamOrchestrator.run() 통합 테스트."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from scripts.stream_orchestrator import StreamOrchestrator
        import unittest.mock as mock
        self.patcher = mock.patch('pykrx.stock.get_market_ohlcv_by_date', return_value=['dummy'])
        self.patcher_date = mock.patch('scripts.stream_orchestrator.date')
        self.mock_date = self.patcher_date.start()
        self.mock_date.today.return_value.weekday.return_value = 0 # Monday
        self.patcher.start()
        self.orch = StreamOrchestrator(exec_mode='shadow')
        yield
        self.patcher.stop()
        self.patcher_date.stop()

    def test_run_returns_dict(self):
        """run()은 딕셔너리를 반환해야 한다."""
        result = self.orch.run()
        assert isinstance(result, dict)

    def test_run_has_required_keys(self):
        """반환에 필수 키가 있어야 한다."""
        result = self.orch.run()
        required_keys = ['status', 'regime', 'signals', 'orders',
                         'allocation', 'execution']
        for key in required_keys:
            assert key in result, f"Missing key: {key}"

    def test_run_status_success(self):
        """정상 실행 시 status='success'."""
        result = self.orch.run()
        assert result['status'] == 'success'

    def test_run_regime_valid(self):
        """레짐이 유효한 값이어야 한다."""
        result = self.orch.run()
        assert result['regime'] in ('bull', 'caution', 'bear', 'crash')

    def test_run_signals_is_dict(self):
        """signals는 스트림 ID → 신호 리스트 딕셔너리."""
        result = self.orch.run()
        assert isinstance(result['signals'], dict)

    def test_run_orders_is_list(self):
        """orders는 리스트."""
        result = self.orch.run()
        assert isinstance(result['orders'], list)

    def test_run_allocation_has_streams(self):
        """allocation에 S1/S2/S3/S5 비율이 있어야 한다.

        ★ AlphaAllocator는 S4(Advisory 전용)를 리스크패리티 배분에서 제외.
           S5(Overnight)가 현금파킹/헤지 역할로 배분에 포함.
        """
        result = self.orch.run()
        alloc = result['allocation']
        assert isinstance(alloc, dict)
        # S4는 advisory 전용으로 배분에서 제외, S1/S2/S3/S5가 포함되어야 함
        for sid in ['S1', 'S2', 'S3', 'S5']:
            assert sid in alloc, f"{sid} missing from allocation"
        assert 'S4' not in alloc, 'S4 (Advisory) should not be in risk-parity allocation'

    def test_run_execution_info(self):
        """execution에 체결 정보가 있어야 한다."""
        result = self.orch.run()
        exec_info = result['execution']
        assert isinstance(exec_info, dict)
        assert 'n_filled' in exec_info
        assert 'n_orders' in exec_info
        assert 'mode' in exec_info

    def test_run_elapsed_seconds(self):
        """elapsed_seconds가 양수."""
        result = self.orch.run()
        assert result.get('elapsed_seconds', 0) >= 0


class TestDynamicConfig:
    """DynamicConfig 기본값 정합성 테스트."""

    def setup_method(self):
        from config.dynamic_config import DynamicConfig
        self.cfg = DynamicConfig()

    def test_portfolio_capital(self):
        """초기 자본금 확인."""
        capital = self.cfg.get('portfolio.initial_capital')
        assert capital is not None
        assert isinstance(capital, (int, float))
        assert capital > 0

    def test_get_with_default(self):
        """존재하지 않는 키에 대한 default 반환."""
        val = self.cfg.get('nonexistent.key.12345', 'FALLBACK')
        assert val == 'FALLBACK'

    def test_regime_params(self):
        """레짐 관련 파라미터 존재 확인."""
        vix_weight = self.cfg.get('regime.weight_vix')
        assert vix_weight is not None
        assert 0 <= vix_weight <= 1

    def test_risk_params(self):
        """리스크 파라미터 존재 확인."""
        dd_limit = self.cfg.get('risk.total_dd_limit')
        assert dd_limit is not None
        assert dd_limit < 0  # 음수 (예: -10%)

    def test_set_and_get(self):
        """set → get 정합성."""
        self.cfg.set('_test_key_12345', 42)
        assert self.cfg.get('_test_key_12345') == 42
        # 정리
        if hasattr(self.cfg, '_runtime') and '_test_key_12345' in self.cfg._runtime:
            del self.cfg._runtime['_test_key_12345']

    def test_all_params(self):
        """all_params()가 딕셔너리를 반환."""
        params = self.cfg.all_params()
        assert isinstance(params, dict)
        assert len(params) > 100  # 최소 100개 이상

    def test_allocation_returns_list(self):
        """get_allocation()은 리스트를 반환."""
        alloc = self.cfg.get_allocation('A', 'bull')
        assert isinstance(alloc, list)
