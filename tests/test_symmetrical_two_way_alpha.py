"""
Unit Test Suite for Symmetrical 2-Way (Long & Short) Alpha Engine
===================================================================

테스트 항목:
  1. Inverse Chandelier Trailing Exit 계산 정확성
  2. 3-Regime Dynamic ETP Holding Matrix 오버나이트/당일 청산 판정
  3. RealtimeEntryMonitor 숏/인버스 대칭적 민감도 (+1.8σ) 저격
  4. SectorPairAlphaEngine Market-Neutral Long Strong / Short Weak 페어 시그널
"""

import pytest
from src.risk.universal_exit_engine import UniversalExitEngine
from src.execution.execution_engine import ExecutionEngine
from src.streams.s1_edge.us_leveraged_etf_scalper import USLeveragedETFScalper
from src.execution.realtime_entry_monitor import RealtimeEntryMonitor
from src.allocation.sector_pair_alpha_engine import SectorPairAlphaEngine


def test_inverse_chandelier_exit():
    """1. 숏/인버스 포지션 Inverse Chandelier Exit 검증."""
    engine = UniversalExitEngine()
    
    # Low_min = 100.0, ATR = 2.0% (0.02), mult = 1.5 -> gap = 3.0% (0.03) -> Trail Price = 103.0
    trough_price = 100.0
    atr_pct = 0.02
    mult = 1.5

    # Case A: 반등 미달 (101.5) -> Hold
    is_exit, trail_price, gap = engine.compute_inverse_chandelier_exit(
        trough_price=trough_price, current_price=101.5, atr_pct=atr_pct, chandelier_mult=mult
    )
    assert not is_exit
    assert trail_price == 103.0

    # Case B: Low 대비 +3.0% 이상 반등 (103.1) -> Exit Trigger
    is_exit_trigger, trail_price, gap = engine.compute_inverse_chandelier_exit(
        trough_price=trough_price, current_price=103.1, atr_pct=atr_pct, chandelier_mult=mult
    )
    assert is_exit_trigger
    assert trail_price == 103.0


def test_three_regime_etp_matrix():
    """2. 3-Regime Dynamic ETP Holding Matrix 검증."""
    exec_engine = ExecutionEngine(mode='shadow')
    scalper = USLeveragedETFScalper()
    time_close = "04:50"

    # --- Regime 0: Bull ---
    # Long ETPs (TQQQ, 233740) 오버나이트 허용
    assert exec_engine.check_etp_overnight_allowed('TQQQ', regime='bull') is True
    assert exec_engine.check_etp_overnight_allowed('233740', regime='bull') is True
    should_exit_tqqq_bull, _ = scalper.check_same_day_market_close_exit(time_close, ticker='TQQQ', regime='bull')
    assert should_exit_tqqq_bull is False

    # Short ETPs (SQQQ, 252670) Bull 레짐에서는 당일 청산
    assert exec_engine.check_etp_overnight_allowed('SQQQ', regime='bull') is False
    should_exit_sqqq_bull, _ = scalper.check_same_day_market_close_exit(time_close, ticker='SQQQ', regime='bull')
    assert should_exit_sqqq_bull is True

    # --- Regime 1: Bear ---
    # Short ETPs (SQQQ, 252670) 오버나이트 허용
    assert exec_engine.check_etp_overnight_allowed('SQQQ', regime='bear') is True
    assert exec_engine.check_etp_overnight_allowed('252670', regime='bear') is True
    should_exit_sqqq_bear, _ = scalper.check_same_day_market_close_exit(time_close, ticker='SQQQ', regime='bear')
    assert should_exit_sqqq_bear is False

    # Long ETPs (TQQQ) Bear 레짐에서는 당일 청산
    assert exec_engine.check_etp_overnight_allowed('TQQQ', regime='bear') is False

    # --- Regime 2: Sideways / Caution ---
    # 모든 레버리지 ETP 100% 당일 청산
    assert exec_engine.check_etp_overnight_allowed('TQQQ', regime='sideways') is False
    assert exec_engine.check_etp_overnight_allowed('SQQQ', regime='sideways') is False
    should_exit_sideways, _ = scalper.check_same_day_market_close_exit(time_close, ticker='TQQQ', regime='sideways')
    assert should_exit_sideways is True


def test_realtime_entry_monitor_symmetrical_bear():
    """3. RealtimeEntryMonitor 숏/인버스 대칭적 수급 저격 (+1.8σ) 검증."""
    monitor = RealtimeEntryMonitor(mode='shadow')

    # 인버스 ETP 돌파 (+2.0σ)
    sig_inverse = monitor.evaluate_ticker_breakout(
        ticker='252670', current_price=2100.0, prev_close=2000.0, volume_zscore=2.0
    )
    assert sig_inverse is not None
    assert sig_inverse['direction'] == 'long'
    assert sig_inverse['score'] >= 1.8

    # US Short ETP SQQQ 돌파 (+2.0σ)
    sig_sqqq = monitor.evaluate_ticker_breakout(
        ticker='SQQQ', current_price=10.5, prev_close=10.0, volume_zscore=2.0
    )
    assert sig_sqqq is not None
    assert sig_sqqq['score'] >= 1.8


def test_sector_pair_alpha_engine():
    """4. SectorPairAlphaEngine Market-Neutral Pair 시그널 검증."""
    pair_engine = SectorPairAlphaEngine()
    
    sector_returns = {
        'semi': 4.5,     # Strong (+1.5σ)
        'auto': 0.5,
        'steel': -0.5,
        'battery': -4.5  # Weak (-1.5σ)
    }

    result = pair_engine.generate_pair_signals(sector_returns, market='KR')
    assert result['pair_active'] is True
    assert result['spread_z'] >= 1.5
    assert result['strong_sector'] == 'semi'
    assert result['weak_sector'] == 'battery'
    assert len(result['pair_orders']) == 2
    
    # Net Direction check (Long Strong vs Short/Inverse Weak)
    assert result['pair_orders'][0]['direction'] == 'long'
    assert result['pair_orders'][1]['direction'] == 'short'
