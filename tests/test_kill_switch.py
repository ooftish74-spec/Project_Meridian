#!/usr/bin/env python3
"""Kill Switch 단위 테스트."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.risk.kill_switch import KillSwitch


class TestKillSwitch:
    """KillSwitch 핵심 기능 검증."""

    def test_instance_creation(self):
        """KillSwitch 인스턴스 생성."""
        ks = KillSwitch()
        assert ks is not None

    def test_assess_returns_dict(self):
        """assess()가 dict 반환."""
        ks = KillSwitch()
        status = ks.assess({'history': {'daily_returns': [0.01]*30}, 'positions': {}})
        assert isinstance(status, dict)

    def test_assess_has_required_keys(self):
        """assess() 반환값에 필수 키 존재."""
        ks = KillSwitch()
        status = ks.assess({'history': {'daily_returns': [0.01]*30}, 'positions': {}})
        # 최소한 이 키들은 존재해야 함
        expected_keys = ['measurement', 'judgment']
        for key in expected_keys:
            assert key in status or any(
                k for k in status.keys() if key in k.lower()
            ), f"'{key}' 관련 키가 status에 없음: {list(status.keys())}"
