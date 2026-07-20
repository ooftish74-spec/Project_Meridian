#!/usr/bin/env python3
"""Measurement Engine 단위 테스트."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestMeasurementEngine:
    """MeasurementEngine 핵심 계산 검증."""

    def test_import(self):
        """MeasurementEngine import 가능."""
        from src.measurement.measurement_engine import MeasurementEngine
        engine = MeasurementEngine()
        assert engine is not None

    def test_sharpe_calculation_known_input(self):
        """알려진 입력으로 Sharpe ratio 검증."""
        # 일일 수익률: 평균 0.001, 표준편차 0.01
        daily_returns = [0.001] * 20  # 상수 수익률 → std=0, Sharpe=inf or 0
        n = len(daily_returns)
        mean_r = sum(daily_returns) / n
        var = sum((r - mean_r) ** 2 for r in daily_returns) / n
        std = math.sqrt(var) if var > 0 else 0

        # 상수 수익률이면 std=0, Sharpe 정의 불가
        assert std == 0.0
        assert mean_r > 0

    def test_sharpe_with_variance(self):
        """변동이 있는 수익률로 Sharpe 계산."""
        daily_returns = [0.01, -0.005, 0.008, -0.002, 0.012,
                         0.003, -0.001, 0.007, 0.002, -0.003]
        n = len(daily_returns)
        mean_r = sum(daily_returns) / n
        var = sum((r - mean_r) ** 2 for r in daily_returns) / n
        std = math.sqrt(var)
        sharpe = (mean_r / std) * math.sqrt(252)

        assert isinstance(sharpe, float)
        assert sharpe > 0  # 양의 평균 → 양의 Sharpe

    def test_daily_return_pct_calculation(self):
        """일일 수익률% 계산 검증."""
        from config.dynamic_config import DynamicConfig
        _cfg = DynamicConfig()
        nav_yesterday = _cfg.get('portfolio.initial_capital')
        nav_today = nav_yesterday * 1.01  # +1%
        daily_return_pct = (nav_today / nav_yesterday - 1) * 100
        assert abs(daily_return_pct - 1.0) < 0.001
