#!/usr/bin/env python3
"""DynamicConfig 단위 테스트."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.dynamic_config import DynamicConfig


class TestDynamicConfig:
    """DynamicConfig 핵심 기능 검증."""

    def test_instance_creation(self):
        """DynamicConfig 인스턴스 생성."""
        cfg = DynamicConfig()
        assert cfg is not None

    def test_get_portfolio_initial_capital(self):
        """portfolio.initial_capital 키 조회."""
        cfg = DynamicConfig()
        capital = cfg.get('portfolio.initial_capital')
        assert capital is not None
        assert isinstance(capital, (int, float))
        assert capital > 0

    def test_get_with_fallback(self):
        """존재하지 않는 키 → fallback 반환."""
        cfg = DynamicConfig()
        val = cfg.get('nonexistent.key.xyz', 42)
        assert val == 42

    def test_risk_params_exist(self):
        """리스크 관련 파라미터 존재 확인."""
        cfg = DynamicConfig()
        # 이 키들은 DynamicConfig에서 로드 가능해야 함
        keys_to_check = [
            'portfolio.initial_capital',
        ]
        for key in keys_to_check:
            val = cfg.get(key, None)
            assert val is not None, f"키 '{key}'가 None 반환"
