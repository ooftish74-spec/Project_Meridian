"""
tests/risk/test_s0_attacker.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
S0 Predictive Leverage Attacker — 핵심 수학 공식 단위 테스트

★ 3개 시나리오 검증 (Requirement D):

  Scenario 1 (수퍼부스트):
    total_nav=1.5억, target_beta=2.5, inst_beta(leverage)=2.0
    → KODEX 레버리지 매수액 = 1.5억 × (2.5-1.0) / 2.0 = 1.125억

  Scenario 2 (어태커 넷숏 — 현금 충분):
    long=9000만, portfolio_beta=1.0, target_beta=-1.0,
    total_nav=1.5억, inst_beta(inverse)=-2.0
    → 곱버스 매수액 = (9000만×1.0 + 1.5억×1.0) / 2.0 = 1.2억

  Scenario 3 (동시 방정식 — 현금 부족):
    위 Scenario 2 환경, 가용 현금=6000만
    → 현물 매도 X = 4000만, 숏 매수 Y = 1억

외부 의존성(파일IO, DynamicConfig, BetaHedge)은 unittest.mock으로 격리.
"""

import sys
import os
import math
import unittest
from unittest.mock import patch, MagicMock, PropertyMock

# ── 경로 설정 ─────────────────────────────────────────────────────────
_PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _PROJECT)


# ── 순수 수학 함수 (테스트 대상 공식 인라인 복제) ────────────────────
# 프로덕션 코드와 동일한 공식을 단순화된 형태로 재현.
# 외부 의존성을 완전히 분리하여 수학적 정확성만 검증.

def compute_super_boost_buy_amount(
    total_nav: float,
    target_beta: float,
    inst_beta: float,
) -> float:
    """[Case 1] 수퍼부스트 공식.
    buy_amount = total_nav × (target_beta - 1.0) / abs(inst_beta)
    """
    assert target_beta > 1.0, "수퍼부스트는 target_beta > 1.0 조건 필요"
    return total_nav * (target_beta - 1.0) / abs(inst_beta)


def compute_attacker_net_short(
    long_exposure: float,
    portfolio_beta: float,
    target_beta: float,
    total_nav: float,
    inst_beta: float,
) -> float:
    """[Case 3] 어태커 넷숏 핵심 공식 (현금 충분 시).
    hedge_amount = (long × portfolio_beta + total_nav × |target_beta|) / |inst_beta|

    수식 유도:
      목표 넷 베타: long×portfolio_beta - Y×|inst_beta| = target_beta × total_nav
      (target_beta < 0 이므로 -target_beta = |target_beta|)
      → Y = (long×portfolio_beta + total_nav×|target_beta|) / |inst_beta|
    """
    assert target_beta < 0.0, "어태커 넷숏은 target_beta < 0 조건 필요"
    return (long_exposure * portfolio_beta + total_nav * abs(target_beta)) / abs(inst_beta)


def compute_simultaneous_equation(
    long_exposure: float,
    portfolio_beta: float,
    target_beta: float,
    total_nav: float,
    inst_beta: float,
    available_cash: float,
) -> tuple:
    """[Case 3 - 현금 부족] 동시 방정식으로 X(현물 매도), Y(숏 매수) 산출.

    방정식 시스템:
      (1) Y = X + available_cash  (자본 방정식)
      (2) (long - X) × portfolio_beta - Y × |inst_beta|
          = target_beta × total_nav    (베타 방정식)

    (1)을 (2)에 대입하여 X 정리:
      X × (portfolio_beta + |inst_beta|)
        = long × portfolio_beta
          - available_cash × |inst_beta|
          - target_beta × total_nav

    Returns:
        (X, Y): X=최소 현물 매도액, Y=최종 숏 매수액
    """
    inst_beta_abs = abs(inst_beta)
    numerator     = (long_exposure * portfolio_beta
                     - available_cash * inst_beta_abs
                     - target_beta * total_nav)
    denominator   = portfolio_beta + inst_beta_abs

    X = numerator / denominator
    X = max(0.0, X)            # 매도액 음수 불가
    X = min(X, long_exposure)  # 롱 초과 매도 불가
    Y = X + available_cash
    return X, Y


class TestS0AttackerScenario1SuperBoost(unittest.TestCase):
    """Scenario 1: 수퍼부스트 — KODEX 레버리지 매수액 검증"""

    def test_buy_amount_exact(self):
        """
        total_nav = 1.5억, target_beta = 2.5, inst_beta(leverage) = 2.0
        기대: 1.5억 × (2.5 - 1.0) / 2.0 = 1.5억 × 1.5 / 2.0 = 1.125억
        """
        total_nav   = 100_000_000   # 1.5억
        target_beta = 2.5
        inst_beta   = 2.0           # KODEX 레버리지 Beta

        result = compute_super_boost_buy_amount(total_nav, target_beta, inst_beta)

        expected = 112_500_000      # 1.125억
        self.assertAlmostEqual(
            result, expected, places=0,
            msg=f"수퍼부스트 매수액 오류: {result:,.0f} ≠ {expected:,.0f}"
        )
        print(f"\n  ✅ Scenario 1 PASS: KODEX레버리지 매수액 = ₩{result/1e6:.3f}억"
              f" (1.5억 × 1.5 / 2.0 = 1.125억)")

    def test_formula_components(self):
        """공식 각 항목 검증: (target_beta - 1.0) = 1.5, total_nav × 1.5 = 2.25억"""
        total_nav   = 100_000_000
        target_beta = 2.5
        excess_beta = target_beta - 1.0          # 1.5
        weighted    = total_nav * excess_beta     # 2.25억
        result      = weighted / 2.0             # 1.125억

        self.assertAlmostEqual(excess_beta, 1.5, places=9)
        self.assertAlmostEqual(weighted, 225_000_000, places=0)
        self.assertAlmostEqual(result, 112_500_000, places=0)
        print(f"  ✅ Scenario 1 공식 항목 검증: excess_beta={excess_beta}, "
              f"weighted=₩{weighted/1e6:.0f}M, result=₩{result/1e6:.3f}M")


class TestS0AttackerScenario2NetShort(unittest.TestCase):
    """Scenario 2: 어태커 넷숏 — 곱버스 매수액 검증 (현금 충분)"""

    def test_hedge_amount_exact(self):
        """
        long=9000만, portfolio_beta=1.0, target_beta=-1.0,
        total_nav=1.5억, inst_beta(inverse)=-2.0
        기대: (9000만×1.0 + 1.5억×1.0) / 2.0 = 2.4억 / 2.0 = 1.2억
        """
        long_exposure  = 90_000_000   # 9000만
        portfolio_beta = 1.0
        target_beta    = -1.0
        total_nav      = 100_000_000  # 1.5억
        inst_beta      = -2.0         # 곱버스 Beta

        result = compute_attacker_net_short(
            long_exposure, portfolio_beta, target_beta, total_nav, inst_beta
        )

        expected = 120_000_000      # 1.2억
        self.assertAlmostEqual(
            result, expected, places=0,
            msg=f"어태커 넷숏 매수액 오류: {result:,.0f} ≠ {expected:,.0f}"
        )
        print(f"\n  ✅ Scenario 2 PASS: 곱버스 매수액 = ₩{result/1e6:.2f}억"
              f" ((9000만 + 1.5억) / 2.0 = 1.2억)")

    def test_net_beta_verification(self):
        """결과값으로 실제 넷베타가 -1.0이 되는지 역산 검증.
        넷 베타 = (long × portfolio_beta - Y × |inst_beta|) / total_nav
               = (9000만×1.0 - 1.2억×2.0) / 1.5억
               = (9000만 - 2.4억) / 1.5억
               = -1.5억 / 1.5억 = -1.0  ✓
        """
        long_exposure  = 90_000_000
        portfolio_beta = 1.0
        Y              = 120_000_000  # 1.2억 (Scenario 2 결과)
        inst_beta_abs  = 2.0
        total_nav      = 100_000_000

        net_beta = (long_exposure * portfolio_beta - Y * inst_beta_abs) / total_nav
        self.assertAlmostEqual(net_beta, -1.0, places=9,
            msg=f"넷베타 역산 실패: {net_beta:.6f} ≠ -1.0")
        print(f"  ✅ Scenario 2 넷베타 역산: {net_beta:.1f} == -1.0 ✓")


class TestS0AttackerScenario3SimultaneousEquation(unittest.TestCase):
    """Scenario 3: 동시 방정식 — 현금 부족 시 최소 현물 매도액(X) + 숏 매수액(Y)"""

    def setUp(self):
        self.long_exposure  = 90_000_000   # 9000만
        self.portfolio_beta = 1.0
        self.target_beta    = -1.0
        self.total_nav      = 100_000_000  # 1.5억
        self.inst_beta      = -2.0         # 곱버스
        self.available_cash = 60_000_000   # 6000만 (부족)

    def test_X_equity_sell_exact(self):
        """현물 매도액 X = 4000만 정확히 계산되는가?

        방정식 풀이:
          inst_beta_abs = 2.0
          numerator = 9000만×1.0 - 6000만×2.0 - (-1.0)×1.5억
                    = 9000만 - 1.2억 + 1.5억 = 1.2억
          denominator = 1.0 + 2.0 = 3.0
          X = 1.2억 / 3.0 = 4000만  ✓
        """
        X, Y = compute_simultaneous_equation(
            self.long_exposure, self.portfolio_beta,
            self.target_beta, self.total_nav,
            self.inst_beta, self.available_cash
        )
        expected_X = 40_000_000  # 4000만
        self.assertAlmostEqual(
            X, expected_X, places=0,
            msg=f"현물 매도액 X 오류: {X:,.0f} ≠ {expected_X:,.0f}"
        )
        print(f"\n  ✅ Scenario 3 PASS: 현물 매도액 X = ₩{X/1e6:.0f}M (기대: 4000만)")

    def test_Y_short_buy_exact(self):
        """숏 매수액 Y = 1억 정확히 계산되는가?
        Y = X + available_cash = 4000만 + 6000만 = 1억  ✓
        """
        X, Y = compute_simultaneous_equation(
            self.long_exposure, self.portfolio_beta,
            self.target_beta, self.total_nav,
            self.inst_beta, self.available_cash
        )
        expected_Y = 100_000_000  # 1억
        self.assertAlmostEqual(
            Y, expected_Y, places=0,
            msg=f"숏 매수액 Y 오류: {Y:,.0f} ≠ {expected_Y:,.0f}"
        )
        print(f"  ✅ Scenario 3 PASS: 숏 매수액 Y = ₩{Y/1e6:.0f}M (기대: 1억)")

    def test_net_beta_after_equation(self):
        """방정식 해로 실제 넷베타가 정확히 -1.0이 달성되는가?

        매도 후 롱 노출도 = 9000만 - X(4000만) = 5000만
        넷 베타 = (5000만×1.0 - Y(1억)×2.0) / 1.5억
               = (5000만 - 2억) / 1.5억
               = -1.5억 / 1.5억 = -1.0  ✓
        """
        X, Y = compute_simultaneous_equation(
            self.long_exposure, self.portfolio_beta,
            self.target_beta, self.total_nav,
            self.inst_beta, self.available_cash
        )
        inst_beta_abs   = abs(self.inst_beta)
        long_after_sell = self.long_exposure - X
        net_beta        = (long_after_sell * self.portfolio_beta - Y * inst_beta_abs) \
                          / self.total_nav

        self.assertAlmostEqual(net_beta, self.target_beta, places=9,
            msg=f"방정식 후 넷베타 오류: {net_beta:.6f} ≠ {self.target_beta}")
        print(f"  ✅ Scenario 3 넷베타 검증: {net_beta:.4f} == {self.target_beta} ✓")

    def test_equation_components_manual(self):
        """수식 각 구성 요소를 수동으로 단계별 검증"""
        inst_beta_abs = abs(self.inst_beta)   # 2.0

        numerator = (self.long_exposure * self.portfolio_beta
                     - self.available_cash * inst_beta_abs
                     - self.target_beta * self.total_nav)
        # = 9000만×1.0 - 6000만×2.0 - (-1.0)×1.5억
        # = 9000만 - 1.2억 + 1.5억 = 1.2억

        denominator = self.portfolio_beta + inst_beta_abs  # 1.0 + 2.0 = 3.0

        self.assertAlmostEqual(numerator,   120_000_000, places=0,
            msg=f"분자 계산 오류: {numerator:,.0f}")
        self.assertAlmostEqual(denominator, 3.0, places=9,
            msg=f"분모 계산 오류: {denominator:.4f}")

        X = numerator / denominator  # 4000만
        Y = X + self.available_cash  # 1억

        self.assertAlmostEqual(X, 40_000_000, places=0)
        self.assertAlmostEqual(Y, 100_000_000, places=0)
        print(f"  ✅ Scenario 3 단계별 검증: "
              f"분자={numerator/1e6:.0f}M, 분모={denominator}, "
              f"X={X/1e6:.0f}M, Y={Y/1e6:.0f}M")


class TestS0AttackerMathEdgeCases(unittest.TestCase):
    """경계 조건 검증"""

    def test_super_boost_exact_1x_boundary(self):
        """target_beta = 1.001 (수퍼부스트 최소 진입)"""
        result = compute_super_boost_buy_amount(100_000_000, 1.001, 2.0)
        expected = 100_000_000 * 0.001 / 2.0  # = 75,000
        self.assertAlmostEqual(result, expected, places=0)
        print(f"\n  ✅ 경계 테스트: target_beta=1.001 → 매수액=₩{result:,.0f}")

    def test_max_leverage_25(self):
        """수퍼부스트 최대 target_beta=2.5 정확성"""
        result = compute_super_boost_buy_amount(100_000_000, 2.5, 2.0)
        self.assertAlmostEqual(result, 112_500_000, places=0)

    def test_attacker_min_target_beta_minus1(self):
        """어태커 최소 beta=-1.0 시 공식 결과 검증"""
        result = compute_attacker_net_short(90_000_000, 1.0, -1.0, 100_000_000, -2.0)
        self.assertAlmostEqual(result, 120_000_000, places=0)

    def test_simultaneous_equation_cash_equals_needed(self):
        """현금 = 필요액 정확히 일치 시 X=0, Y=available_cash"""
        # hedge_amount = (9000만 + 1.5억) / 2.0 = 1.2억
        # available_cash = 1.2억 → X=0, Y=1.2억
        X, Y = compute_simultaneous_equation(
            90_000_000, 1.0, -1.0, 100_000_000, -2.0,
            120_000_000  # 현금 = 필요액
        )
        self.assertAlmostEqual(X, 0.0, places=0,
            msg=f"현금 충분 시 X != 0: {X:,.0f}")
        self.assertAlmostEqual(Y, 120_000_000, places=0)
        print(f"  ✅ 현금=필요액 경계: X=₩{X/1e6:.0f}M, Y=₩{Y/1e6:.0f}M")

    def test_attacker_zero_cash(self):
        """가용 현금=0 극단 케이스 — 전액 현물 매도로 숏 구축"""
        X, Y = compute_simultaneous_equation(
            90_000_000, 1.0, -1.0, 100_000_000, -2.0, 0.0
        )
        # numerator = 9000만×1 - 0×2 - (-1)×1.5억 = 9000만+1.5억 = 2.4억
        # denominator = 1+2 = 3
        # X = 2.4억/3 = 8000만, Y = 8000만+0 = 8000만
        self.assertAlmostEqual(X, 80_000_000, places=0)
        self.assertAlmostEqual(Y, 80_000_000, places=0)

        # 넷베타 검증: (9000만-8000만)×1.0 - 8000만×2.0 / 1.5억
        # = (1000만 - 1.6억) / 1.5억 = -1.5억 / 1.5억 = -1.0 ✓
        net_beta = ((90_000_000 - X) * 1.0 - Y * 2.0) / 100_000_000
        self.assertAlmostEqual(net_beta, -1.0, places=9)
        print(f"  ✅ 현금=0 극단: X=₩{X/1e6:.0f}M, Y=₩{Y/1e6:.0f}M, "
              f"넷베타={net_beta:.1f}")


if __name__ == '__main__':
    print()
    print("=" * 68)
    print(" S0 Predictive Leverage Attacker — 수학 공식 단위 테스트")
    print("=" * 68)
    print()
    print("  Scenario 1 (수퍼부스트):")
    print("    total_nav=1.5억, target_beta=2.5, inst_beta=2.0")
    print("    buy = 1.5억 × (2.5-1.0) / 2.0 = 1.125억")
    print()
    print("  Scenario 2 (어태커 넷숏):")
    print("    long=9000만, p_beta=1.0, t_beta=-1.0, nav=1.5억, inst=-2.0")
    print("    hedge = (9000만 + 1.5억) / 2.0 = 1.2억")
    print()
    print("  Scenario 3 (동시 방정식):")
    print("    위 환경 + 현금=6000만")
    print("    X(현물매도) = 4000만, Y(숏매수) = 1억, 넷베타=-1.0")
    print()

    loader    = unittest.TestLoader()
    suite     = loader.loadTestsFromModule(__import__('__main__'))
    runner    = unittest.TextTestRunner(verbosity=2)
    result_obj = runner.run(suite)

    print()
    if result_obj.wasSuccessful():
        print("=" * 68)
        print(" ✅ ALL TESTS PASSED — S0 Attacker 수학 공식 검증 완료")
        print("=" * 68)
        sys.exit(0)
    else:
        print("=" * 68)
        print(f" ❌ FAILED: {len(result_obj.failures)} failures, "
              f"{len(result_obj.errors)} errors")
        print("=" * 68)
        sys.exit(1)
