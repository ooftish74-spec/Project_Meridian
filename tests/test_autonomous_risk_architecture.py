"""
Unit Test Suite for 0% Hardcoding Autonomous Risk Architecture
==============================================================

테스트 항목:
  1. IntradayRegimeFastSwitch 롤링 Z-Score 복합 스위칭 오발화 방지 및 비상 레짐(CRASH_FLASH) 포착
  2. VolatilityDecayShield Choppiness Index 수식 및 Sigmoid 연속 레버리지 스케일링
  3. ExecutionEngine 통합 오버나이트 ETP 보유 제어
"""

import pytest
import numpy as np
from src.risk.intraday_regime_fast_switch import IntradayRegimeFastSwitch
from src.risk.volatility_decay_shield import VolatilityDecayShield
from src.execution.execution_engine import ExecutionEngine


def test_intraday_regime_fast_switch():
    """1. 롤링 Z-Score 복합 패스트 레짐 스위칭 검증."""
    fast_switch = IntradayRegimeFastSwitch()

    # Case A: 일반 평시 시장 (Z_composite < +2.0σ) ➔ 정상 유지
    is_trig, regime, z_comp, details = fast_switch.evaluate_fast_switch(
        vix=18.0, vix3m=20.0, declining_ratio_5m=0.45, current_high=100.0, current_low=99.5
    )
    assert not is_trig
    assert regime == 'normal'
    assert z_comp < 2.0

    # Case B: VIX 기간구조 역전 + 90% 종목 폭락 + Parkinson 변동성 스파이크 (Z_composite >= +2.0σ) ➔ CRASH_FLASH 트리거
    is_trig_crash, regime_crash, z_comp_crash, details_crash = fast_switch.evaluate_fast_switch(
        vix=35.0, vix3m=22.0, declining_ratio_5m=0.92, current_high=100.0, current_low=92.0
    )
    assert is_trig_crash
    assert regime_crash == 'CRASH_FLASH'
    assert z_comp_crash >= 2.0


def test_volatility_decay_shield():
    """2. Choppiness Index 및 Sigmoid 연속 레버리지 스케일링 검증."""
    shield = VolatilityDecayShield(period=14)

    # 톱니바퀴 횡보장 데이터 생성 (High - Low가 작고 TrueRange Sum이 큼)
    highs_choppy = [100.0 + (i % 2) * 1.5 for i in range(30)]
    lows_choppy = [98.5 + (i % 2) * 1.5 for i in range(30)]
    closes_choppy = [99.0 + (i % 3) * 0.5 for i in range(30)]

    eval_choppy = shield.evaluate_etp_deleveraging(highs_choppy, lows_choppy, closes_choppy)
    assert 'chop' in eval_choppy
    assert 'leverage_scale' in eval_choppy
    assert 0.20 <= eval_choppy['leverage_scale'] <= 1.0

    # 명확한 우상향 추세장 데이터 생성 (High - Low 폭이 넓음)
    highs_trend = [100.0 + i * 2.0 for i in range(30)]
    lows_trend = [99.0 + i * 2.0 for i in range(30)]
    closes_trend = [99.5 + i * 2.0 for i in range(30)]

    eval_trend = shield.evaluate_etp_deleveraging(highs_trend, lows_trend, closes_trend)
    assert eval_trend['chop'] < eval_choppy['chop']
    assert eval_trend['leverage_scale'] >= eval_choppy['leverage_scale']


def test_execution_engine_integrated_risk_shield():
    """3. ExecutionEngine 통합 오버나이트 ETP 보유 제어 검증."""
    engine = ExecutionEngine(mode='shadow')

    # Market data with crash spike
    crash_market_data = {
        'vix': 38.0,
        'vix3m': 24.0,
        'declining_ratio': 0.95
    }

    # Under Fast Switch CRASH_FLASH, Short ETP (SQQQ) overnight is ALLOWED
    sqqq_allowed = engine.check_etp_overnight_allowed('SQQQ', regime='bull', market_data=crash_market_data)
    assert sqqq_allowed is True

    # Long ETP (TQQQ) overnight under CRASH_FLASH is DISALLOWED
    tqqq_allowed = engine.check_etp_overnight_allowed('TQQQ', regime='bull', market_data=crash_market_data)
    assert tqqq_allowed is False
