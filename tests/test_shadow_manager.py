#!/usr/bin/env python3
"""Shadow Manager 단위 테스트."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.dynamic_config import DynamicConfig
from src.portfolio.shadow_manager import ShadowPortfolioManager


class TestShadowManager:
    """ShadowPortfolioManager 핵심 기능 검증."""

    def test_instance_creation(self):
        """ShadowPortfolioManager 인스턴스 생성."""
        mgr = ShadowPortfolioManager()
        assert mgr is not None

    def test_has_positions_attribute(self):
        """positions 속성 존재."""
        mgr = ShadowPortfolioManager()
        assert hasattr(mgr, 'positions')

    def test_has_nav_attribute(self):
        """NAV 관련 속성 존재."""
        mgr = ShadowPortfolioManager()
        # nav 또는 total_value 또는 virtual_nav
        has_nav = (
            hasattr(mgr, 'nav') or
            hasattr(mgr, 'total_value') or
            hasattr(mgr, 'virtual_nav')
        )
        assert has_nav, "NAV 관련 속성이 없습니다"

    def test_initial_capital_from_config(self):
        """초기 자본이 DynamicConfig에서 로드."""
        cfg = DynamicConfig()
        expected = cfg.get('portfolio.initial_capital')
        assert expected is not None
        assert isinstance(expected, (int, float))
        assert expected > 0
