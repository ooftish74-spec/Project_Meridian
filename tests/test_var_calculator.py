#!/usr/bin/env python3
"""VaR Calculator 단위 테스트."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from config.dynamic_config import DynamicConfig
from src.risk.realtime_var import RealtimeVaR


class TestRealtimeVaR:
    """RealtimeVaR 핵심 기능 검증."""

    def test_instance_creation(self):
        """RealtimeVaR 인스턴스 생성."""
        var_calc = RealtimeVaR()
        assert var_calc is not None

    def test_fallback_var_structure(self):
        """_fallback_var 반환 구조 검증."""
        var_calc = RealtimeVaR()
        cfg = DynamicConfig()
        pv = float(cfg.get('portfolio.initial_capital'))
        result = var_calc._fallback_var(pv, 0.95)

        assert isinstance(result, dict)
        assert 'var_pct' in result
        assert 'var_amount' in result
        assert 'confidence' in result
        assert result['confidence'] == 0.95
        assert result['var_pct'] > 0
        assert result['var_amount'] > 0

    def test_ewma_variance(self):
        """EWMA 분산 계산 검증 (알려진 입력)."""
        var_calc = RealtimeVaR()
        # 상수 수익률 → 분산 ≈ 0
        returns = np.array([0.01] * 50)
        ewma_var = var_calc._ewma_variance(returns, 0.94)
        assert ewma_var >= 0
        assert ewma_var < 0.001  # 상수이므로 거의 0

    def test_ewma_variance_volatile(self):
        """변동 수익률의 EWMA 분산 계산."""
        var_calc = RealtimeVaR()
        np.random.seed(42)
        returns = np.random.normal(0, 0.02, 100)
        ewma_var = var_calc._ewma_variance(returns, 0.94)
        assert ewma_var > 0
        # 대략 0.02^2 = 0.0004 근처
        assert ewma_var < 0.01

    def test_calculate_returns_dict(self):
        """calculate()가 dict 반환."""
        var_calc = RealtimeVaR()
        cfg = DynamicConfig()
        pv = float(cfg.get('portfolio.initial_capital'))
        result = var_calc.calculate(portfolio_value=pv)
        assert isinstance(result, dict)
        assert 'var_pct' in result
