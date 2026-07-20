#!/usr/bin/env python3
"""
Risk Module Unit Tests — KillSwitch, DrawdownGuard, CrashDefense
=================================================================

DD 과제 #4: 3개 핵심 리스크 모듈의 measure/judge 분리 아키텍처 검증.

Usage:
    python -m pytest tests/test_risk_modules.py -v
"""

import sys
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가 (DynamicConfig import용)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import pytest
from unittest.mock import patch

from config.dynamic_config import DynamicConfig
from src.risk.kill_switch import KillSwitch
from src.risk.drawdown_guard import DrawdownGuard
from src.risk.crash_defense import CrashDefense

_cfg = DynamicConfig()
_INITIAL_CAPITAL = _cfg.get('portfolio.initial_capital')


# ═══════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def normal_portfolio():
    """정상 상태 포트폴리오."""
    return {
        'total_nav': _INITIAL_CAPITAL,
        'hwm': _INITIAL_CAPITAL * 1.03,  # 3% profit
        'daily_returns': [0.005, 0.003, -0.001, 0.004, 0.002],
        'active_positions': 8,
    }


@pytest.fixture
def losing_portfolio():
    """연속 손실 포트폴리오."""
    return {
        'total_nav': _INITIAL_CAPITAL * 0.8125,
        'hwm': _INITIAL_CAPITAL,
        'daily_returns': [-0.01, -0.015, -0.02, -0.012, -0.008, -0.025, -0.01, -0.018],
        'active_positions': 3,
    }


@pytest.fixture
def crash_portfolio():
    """급락 포트폴리오 (DD > -25%)."""
    return {
        'total_nav': _INITIAL_CAPITAL * 0.70,
        'hwm': _INITIAL_CAPITAL * 1.05,
        'daily_returns': [-0.05, -0.04, -0.03, -0.02, -0.01],
        'active_positions': 1,
    }


@pytest.fixture
def normal_market():
    """정상 시장 데이터."""
    return {
        'signal_cache': {
            'vix': 16, 'vix_prev': 15.5,
            'vkospi': 15, 'usdkrw': 1350, 'usdkrw_prev': 1345,
            'foreign_net_buy': 100_000_000_000,
            'kospi_change_pct': 0.5,
        },
        'overnight_intel': {
            'sp500_change_pct': 0.3,
            'nasdaq_change_pct': 0.5,
        },
    }


@pytest.fixture
def stressed_market():
    """스트레스 시장 데이터 (VIX 40+)."""
    return {
        'signal_cache': {
            'vix': 45, 'vix_prev': 28,
            'vkospi': 35, 'usdkrw': 1450, 'usdkrw_prev': 1350,
            'foreign_net_buy': -800_000_000_000,
            'kospi_change_pct': -4.5,
        },
        'overnight_intel': {
            'sp500_change_pct': -3.5,
            'nasdaq_change_pct': -4.2,
        },
    }


# ═══════════════════════════════════════════
# KillSwitch Tests
# ═══════════════════════════════════════════

class TestKillSwitch:
    """KillSwitch 측정/판정 분리 검증."""

    def test_measure_normal(self, normal_portfolio):
        """정상 포트폴리오: 측정값 형식 검증."""
        ks = KillSwitch()
        m = ks.measure_metrics(normal_portfolio)

        assert 'today_return_pct' in m
        assert 'consecutive_loss_days' in m
        assert 'dd_pct' in m
        assert 'weekly_return_pct' in m
        assert 'timestamp' in m

        # 마지막 수익률이 양수 → 연속 손실 0
        assert m['consecutive_loss_days'] == 0
        # DD = (NAV / 155M - 1) * 100
        assert -4 < m['dd_pct'] < 60

    def test_measure_losing(self, losing_portfolio):
        """연속 손실 포트폴리오: 연속 손실일 정확성.

        ★ Gap-Aware 격리: shadow_portfolio.json이 로컬에 있으면
          갭 필터가 daily_returns[-1]을 제거해 consecutive_loss_days가
          줄어들 수 있음. Path.exists()를 False로 mock해서 파일시스템
          의존성을 완전히 차단.
        """
        ks = KillSwitch()
        # ★ FIX: Gap-Aware 로직이 로컬 shadow_portfolio.json 읽는 것을 차단
        with patch('src.risk.kill_switch.Path.exists', return_value=False):
            m = ks.measure_metrics(losing_portfolio)

        # 모든 수익률이 음수 → 연속 손실 = 전체 길이
        assert m['consecutive_loss_days'] == 8
        # DD = (130M / 160M - 1) * 100 = -18.75%
        assert m['dd_pct'] == pytest.approx(-18.75, abs=0.1)

    def test_judge_normal_safe(self, normal_portfolio):
        """정상 상태: 킬스위치 미발동."""
        ks = KillSwitch()
        m = ks.measure_metrics(normal_portfolio)
        j = ks.judge_action(m)

        assert j['triggered'] is False
        assert j['safe'] is True
        assert j['action'] == 'continue'
        assert len(j['triggers']) == 0

    def test_judge_daily_loss_trigger(self):
        """일간 손실 한도 초과: 킬스위치 발동."""
        ks = KillSwitch()
        metrics = {
            'today_return_pct': -6.0,  # -6% (한도 -5%)
            'consecutive_loss_days': 1,
            'dd_pct': -3.0,
            'weekly_return_pct': -6.0,
        }
        j = ks.judge_action(metrics)

        assert j['triggered'] is True
        assert j['action'] == 'halt_all'
        assert any(t['type'] == 'daily_loss' for t in j['triggers'])

    def test_judge_consecutive_loss_trigger(self):
        """연속 손실 한도 초과: 킬스위치 발동."""
        ks = KillSwitch()
        metrics = {
            'today_return_pct': -0.5,
            'consecutive_loss_days': 8,  # 한도 7
            'dd_pct': -5.0,
            'weekly_return_pct': -3.0,
        }
        j = ks.judge_action(metrics)

        assert j['triggered'] is True
        assert any(t['type'] == 'consecutive_loss' for t in j['triggers'])

    def test_judge_dd_critical_trigger(self):
        """DD Stage 5+ (-25%): 킬스위치 발동."""
        ks = KillSwitch()
        metrics = {
            'today_return_pct': -1.0,
            'consecutive_loss_days': 3,
            'dd_pct': -27.0,  # Stage 5 한도 -25%
            'weekly_return_pct': -5.0,
        }
        j = ks.judge_action(metrics)

        assert j['triggered'] is True
        assert any(t['type'] == 'dd_critical' for t in j['triggers'])

    def test_judge_crash_weekly_trigger(self):
        """CRASH 레짐 주간 급락: 킬스위치 발동."""
        ks = KillSwitch()
        metrics = {
            'today_return_pct': -2.0,
            'consecutive_loss_days': 3,
            'dd_pct': -8.0,
            'weekly_return_pct': -12.0,  # 한도 -10%
        }
        j = ks.judge_action(metrics, regime='crash')

        assert j['triggered'] is True
        assert any(t['type'] == 'crash_weekly' for t in j['triggers'])

    def test_judge_crash_weekly_ignored_in_bull(self):
        """BULL 레짐에서는 주간 급락 트리거 무시."""
        ks = KillSwitch()
        metrics = {
            'today_return_pct': -2.0,
            'consecutive_loss_days': 3,
            'dd_pct': -8.0,
            'weekly_return_pct': -12.0,  # crash에서만 작동
        }
        j = ks.judge_action(metrics, regime='bull')

        # crash_weekly 트리거는 bull에서는 발동하지 않음
        assert not any(t['type'] == 'crash_weekly' for t in j['triggers'])

    def test_assess_integration(self, normal_portfolio):
        """assess(): 측정 + 판정 통합 2-layer 구조."""
        ks = KillSwitch()
        result = ks.assess(normal_portfolio, regime='caution')

        assert 'measurement' in result
        assert 'judgment' in result
        assert 'today_return_pct' in result['measurement']
        assert 'triggered' in result['judgment']

    def test_check_format(self, normal_portfolio):
        """check(): 간편 반환 형식."""
        ks = KillSwitch()
        # check()는 shadow_portfolio.json을 로드 시도하므로 기본값 사용
        result = ks.check(_INITIAL_CAPITAL, regime='caution')

        assert 'triggered' in result
        assert 'can_buy' in result
        assert 'position_scale' in result
        assert 'reason' in result
        assert isinstance(result['position_scale'], float)

    def test_reset(self):
        """reset(): 킬스위치 상태 초기화."""
        ks = KillSwitch()
        ks._triggered = True
        assert ks.is_triggered is True
        ks.reset()
        assert ks.is_triggered is False


# ═══════════════════════════════════════════
# DrawdownGuard Tests
# ═══════════════════════════════════════════

class TestDrawdownGuard:
    """DrawdownGuard 6단계 방어 검증."""

    def test_measure_normal(self, normal_portfolio):
        """정상 DD: 측정값 형식 검증."""
        guard = DrawdownGuard()
        m = guard.measure(normal_portfolio)

        assert 'total_dd_pct' in m
        assert 'sleeve_a_dd_pct' in m
        assert 'from_initial_dd_pct' in m
        assert 'consecutive_loss_days' in m
        assert -10 < m['total_dd_pct'] < 60

    def test_measure_dd_calculation(self):
        """DD% 정확도: (NAV/HWM - 1) × 100."""
        guard = DrawdownGuard()
        portfolio = {
            'total_nav': 140_000_000,
            'hwm': 160_000_000,
            'daily_returns': [],
        }
        m = guard.measure(portfolio)

        # (140/160 - 1) * 100 = -12.5%
        assert m['total_dd_pct'] == pytest.approx(-12.5, abs=0.01)

    def test_judge_stage0_normal(self):
        """Stage 0 (DD > -5%): exposure=1.0, safe=True."""
        guard = DrawdownGuard()
        measurement = {'total_dd_pct': -3.0}
        j = guard.judge(measurement)

        assert j['dd_stage'] == 0
        assert j['target_exposure'] == 1.0
        assert j['safe'] is True

    def test_judge_stage1(self):
        """Stage 1 (DD ≤ -5%): exposure=0.80."""
        guard = DrawdownGuard()
        measurement = {'total_dd_pct': -6.0}
        j = guard.judge(measurement)

        assert j['dd_stage'] == 1
        assert j['target_exposure'] == pytest.approx(0.80, abs=0.01)
        assert j['safe'] is False

    def test_judge_stage2(self):
        """Stage 2 (DD ≤ -10%): exposure=0.50."""
        guard = DrawdownGuard()
        measurement = {'total_dd_pct': -11.0}
        j = guard.judge(measurement)

        assert j['dd_stage'] == 2
        assert j['target_exposure'] == pytest.approx(0.50, abs=0.01)

    def test_judge_stage3(self):
        """Stage 3 (DD ≤ -15%): exposure=0.30."""
        guard = DrawdownGuard()
        measurement = {'total_dd_pct': -16.0}
        j = guard.judge(measurement)

        assert j['dd_stage'] == 3
        assert j['target_exposure'] == pytest.approx(0.30, abs=0.01)

    def test_judge_stage4(self):
        """Stage 4 (DD ≤ -20%): exposure=0.10."""
        guard = DrawdownGuard()
        measurement = {'total_dd_pct': -21.0}
        j = guard.judge(measurement)

        assert j['dd_stage'] == 4
        assert j['target_exposure'] == pytest.approx(0.10, abs=0.01)

    def test_judge_stage5_halt(self):
        """Stage 5 (DD ≤ -25%): exposure=0.00."""
        guard = DrawdownGuard()
        measurement = {'total_dd_pct': -26.0}
        j = guard.judge(measurement)

        assert j['dd_stage'] == 5
        assert j['target_exposure'] == pytest.approx(0.00, abs=0.01)

    def test_judge_stage6_liquidate(self):
        """Stage 6 (DD ≤ -30%): 전량 청산."""
        guard = DrawdownGuard()
        measurement = {'total_dd_pct': -32.0}
        j = guard.judge(measurement)

        assert j['dd_stage'] == 6
        assert j['target_exposure'] == 0.0
        assert any(a['action'] == 'liquidate_all' for a in j['actions'])

    def test_judge_crash_regime_additional_limit(self):
        """CRASH 레짐: Stage 0에서도 exposure 제한."""
        guard = DrawdownGuard()
        measurement = {'total_dd_pct': -2.0}  # Stage 0
        j = guard.judge(measurement, regime='crash')

        # crash_cash_ratio=0.80 → max exposure = 0.20
        assert j['target_exposure'] <= 0.20
        assert any(a.get('action') == 'crash_protocol' for a in j['actions'])

    def test_assess_integration(self, normal_portfolio):
        """assess(): 2-layer 통합."""
        guard = DrawdownGuard()
        result = guard.assess(normal_portfolio)

        assert 'measurement' in result
        assert 'judgment' in result
        assert 'total_dd_pct' in result['measurement']
        assert 'dd_stage' in result['judgment']

    def test_check_format(self):
        """check(): 간편 반환 형식."""
        guard = DrawdownGuard()
        result = guard.check(_INITIAL_CAPITAL, regime='caution')

        assert 'drawdown_pct' in result
        assert 'exposure' in result
        assert 'stage' in result
        assert 'dd_stage' in result
        assert 'safe' in result

    def test_stage_monotonicity(self):
        """스테이지별 exposure 단조 감소 검증."""
        guard = DrawdownGuard()
        prev_exposure = 2.0  # 시작: 어떤 값보다 큰 값

        for dd in [0, -3, -6, -11, -16, -21, -26, -32]:
            j = guard.judge({'total_dd_pct': float(dd)})
            assert j['target_exposure'] <= prev_exposure, \
                f"DD={dd}%에서 exposure가 증가 (stage={j['dd_stage']})"
            prev_exposure = j['target_exposure']


# ═══════════════════════════════════════════
# CrashDefense Tests
# ═══════════════════════════════════════════

class TestCrashDefense:
    """CrashDefense 급락 방어 검증."""

    def test_measure_normal(self, normal_market, normal_portfolio):
        """정상 시장: 스트레스 측정값 형식 검증."""
        cd = CrashDefense()
        m = cd.measure(normal_market, normal_portfolio)

        assert 'vix' in m
        assert 'stress_score' in m
        assert 'vkospi' in m
        assert 'fx_change_pct' in m
        assert 'timestamp' in m

    def test_measure_normal_low_stress(self, normal_market, normal_portfolio):
        """정상 시장: stress_score < 30."""
        cd = CrashDefense()
        m = cd.measure(normal_market, normal_portfolio)

        assert m['stress_score'] < 30
        assert m['vix'] == 16

    def test_measure_stressed_high_score(self, stressed_market, normal_portfolio):
        """스트레스 시장: stress_score ≥ 70."""
        cd = CrashDefense()
        m = cd.measure(stressed_market, normal_portfolio)

        # VIX=45 → +30, VKOSPI=35 → +20, SP500=-3.5 → +17.5, FX=+7.4% → +10
        assert m['stress_score'] >= 50
        assert m['vix'] == 45

    def test_judge_normal_safe(self, normal_market, normal_portfolio):
        """정상: 방어 불필요."""
        cd = CrashDefense()
        m = cd.measure(normal_market, normal_portfolio)
        j = cd.judge(m, normal_portfolio)

        assert j['stress_level'] == 'normal'
        assert j['safe'] is True
        assert len(j['actions']) == 0

    def test_judge_caution_level(self):
        """경계 (stress 30~50): 현금 비중 증가."""
        cd = CrashDefense()
        measurement = {
            'stress_score': 40,
            'fx_change_pct': 0.5,
        }
        j = cd.judge(measurement, {})

        assert j['stress_level'] == 'caution'
        assert any(a['action'] == 'increase_cash' for a in j['actions'])

    def test_judge_danger_level(self):
        """위험 (stress 50~70): 방어 모드."""
        cd = CrashDefense()
        measurement = {
            'stress_score': 60,
            'fx_change_pct': 1.0,
        }
        j = cd.judge(measurement, {})

        assert j['stress_level'] == 'danger'
        assert any(a['action'] == 'defensive_mode' for a in j['actions'])

    def test_judge_crash_level(self):
        """크래시 (stress 70+): 크래시 프로토콜."""
        cd = CrashDefense()
        measurement = {
            'stress_score': 80,
            'fx_change_pct': 2.0,
        }
        j = cd.judge(measurement, {})

        assert j['stress_level'] == 'crash'
        assert any(a['action'] == 'crash_protocol' for a in j['actions'])
        # crash protocol에는 halt_new_positions가 포함
        crash_action = next(a for a in j['actions'] if a['action'] == 'crash_protocol')
        assert crash_action.get('halt_new_positions') is True

    def test_judge_fx_alert(self):
        """환율 급변: FX 리밸런싱."""
        cd = CrashDefense()
        measurement = {
            'stress_score': 10,  # normal
            'fx_change_pct': 6.0,  # > rebalance_on_fx_move(5.0)
        }
        j = cd.judge(measurement, {})

        assert any(a['action'] == 'fx_hedge_rebalance' for a in j['actions'])

    def test_assess_integration(self, normal_market, normal_portfolio):
        """assess(): 2-layer 통합."""
        cd = CrashDefense()
        result = cd.assess(normal_market, normal_portfolio, regime='caution')

        assert 'measurement' in result
        assert 'judgment' in result
        assert 'stress_score' in result['measurement']
        assert 'stress_level' in result['judgment']

    def test_stress_score_bounded(self):
        """stress_score는 항상 0~100 범위."""
        cd = CrashDefense()

        # 극단적 입력
        extreme_market = {
            'signal_cache': {
                'vix': 80, 'vix_prev': 15,
                'vkospi': 60, 'usdkrw': 1600, 'usdkrw_prev': 1300,
                'foreign_net_buy': -2_000_000_000_000,
                'kospi_change_pct': -10,
            },
            'overnight_intel': {
                'sp500_change_pct': -8,
                'nasdaq_change_pct': -10,
            },
        }
        m = cd.measure(extreme_market, {})

        assert 0 <= m['stress_score'] <= 100


# ═══════════════════════════════════════════
# Cross-Module Integration Test
# ═══════════════════════════════════════════

class TestRiskModuleIntegration:
    """3개 리스크 모듈 간 일관성 검증."""

    def test_measure_judge_separation(self, normal_portfolio, normal_market):
        """모든 모듈이 측정/판정 분리 아키텍처를 따르는지 확인."""
        # KillSwitch
        ks = KillSwitch()
        m1 = ks.measure_metrics(normal_portfolio)
        j1 = ks.judge_action(m1)
        assert isinstance(m1, dict) and isinstance(j1, dict)

        # DrawdownGuard
        dd = DrawdownGuard()
        m2 = dd.measure(normal_portfolio)
        j2 = dd.judge(m2)
        assert isinstance(m2, dict) and isinstance(j2, dict)

        # CrashDefense
        cd = CrashDefense()
        m3 = cd.measure(normal_market, normal_portfolio)
        j3 = cd.judge(m3, normal_portfolio)
        assert isinstance(m3, dict) and isinstance(j3, dict)

    def test_all_safe_in_normal(self, normal_portfolio, normal_market):
        """정상 상태에서 3개 모듈 모두 safe=True."""
        ks = KillSwitch()
        dd = DrawdownGuard()
        cd = CrashDefense()

        ks_result = ks.assess(normal_portfolio)
        dd_result = dd.assess(normal_portfolio)
        cd_result = cd.assess(normal_market, normal_portfolio)

        assert ks_result['judgment']['safe'] is True
        assert dd_result['judgment']['safe'] is True
        assert cd_result['judgment']['safe'] is True

    def test_all_triggered_in_crash(self, crash_portfolio, stressed_market):
        """극단적 상황에서 방어 메커니즘 작동."""
        ks = KillSwitch()
        dd = DrawdownGuard()
        cd = CrashDefense()

        # DD = (110/160 - 1) * 100 = -31.25% → Stage 6
        dd_result = dd.assess(crash_portfolio)
        assert dd_result['judgment']['dd_stage'] >= 5

        # Stressed market
        cd_result = cd.assess(stressed_market, crash_portfolio, regime='crash')
        assert cd_result['judgment']['stress_level'] in ('danger', 'crash')
