"""
tests/execution/test_smart_wallet.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Smart Wallet — Volatility-Scaled Merton-Kelly 단위 테스트

★ 검증 시나리오:

  Part A — 수학 모델 단위 검증
    Test A1  [VoL Penalty]  vix=15, ema=15   → penalty=1.0 (평상시)
    Test A2  [Vol Penalty]  vix=30, ema=15   → penalty=2.0 (공포 급등)
    Test A3  [Vol Penalty]  vix=10, ema=15   → penalty=1.0 (하방 클램핑)
    Test A4  [f_long]       P_c=0.0, P_b=0.0 → f_long=0.90 (맑은 날 최대)
    Test A5  [f_long]       P_c=0.9, P_b=0.0 → f_long=0.009 (폭락 시 최소)
    Test A6  [f_long]       P_c=1.0+P_b=0.5  → f_long=0.0  (분자 음수 Clamping)
    Test A7  [CashRatio]    P_c=0.9          → cash_ratio ≥ min_cash (수학 증명)
    Test A8  [CashRatio]    P_c=0.0, vix 정상 → cash_ratio=0.10 (최소 현금)

  Part B — Graceful Degradation
    Test B1  [Fallback]     HMM 완전 고장    → cash=0.50 (보수적 기본값)
    Test B2  [Fallback]     signal_cache 없음 → Vol_Penalty=1.0 안전 반환
    Test B3  [Fallback]     음수 P_c         → f_long 음수 없음 (Clamping)
    Test B4  [Fallback]     P_c+P_b>1.0     → 정규화 후 f_long 정상

  Part C — regime_probabilities 정규화
    Test C1  정상 확률 합   normal+bear+crash ≈ 1.0
    Test C2  crash 우세     P_c ≥ 0.5 → f_long ≤ 0.50 (수식 검증)

  Part D — 통합 할당기 검증
    Test D1  direct 모드    프리셋 확률로 allocate() 수행
    Test D2  allocate() 반환 키 완전성 검증
"""

import json
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

_PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT))


from src.allocation.capital_allocator import SmartWalletAllocator


# ════════════════════════════════════════════════════════════════
# 헬퍼
# ════════════════════════════════════════════════════════════════

def _wallet() -> SmartWalletAllocator:
    return SmartWalletAllocator()


def _probs(crash: float, bear: float) -> dict:
    return {'crash': crash, 'bear': bear, 'normal': max(0.0, 1 - crash - bear)}


# ════════════════════════════════════════════════════════════════
# Part A — 수학 모델 단위 검증
# ════════════════════════════════════════════════════════════════

class TestVolPenalty(unittest.TestCase):
    """Vol_Penalty = max(1.0, current_vix / ema_vix)"""

    def setUp(self):
        self.sw = _wallet()

    def test_A1_flat_vix_no_penalty(self):
        """vix=ema=15 → 페널티 없음 (1.0)."""
        cache = {'vkospi': 15.0, 'vkospi_ema': 15.0}
        p = self.sw.compute_vol_penalty(cache)
        self.assertAlmostEqual(p, 1.0, places=4)
        print(f"\n  ✅ A1: flat vix → penalty={p:.4f}")

    def test_A2_fear_spike_doubles_penalty(self):
        """vix=30, ema=15 → 페널티 2.0."""
        cache = {'vkospi': 30.0, 'vkospi_ema': 15.0}
        p = self.sw.compute_vol_penalty(cache)
        self.assertAlmostEqual(p, 2.0, places=4)
        print(f"  ✅ A2: fear spike → penalty={p:.4f}")

    def test_A3_low_vix_floored_at_1(self):
        """vix=10 < ema=15 → 하방 클램핑 1.0."""
        cache = {'vkospi': 10.0, 'vkospi_ema': 15.0}
        p = self.sw.compute_vol_penalty(cache)
        self.assertEqual(p, 1.0, f"penalty={p}")
        print(f"  ✅ A3: low vix → clamped to 1.0")

    def test_A3b_missing_vix_safe(self):
        """vkospi 없음 → 안전하게 1.0 반환."""
        p = self.sw.compute_vol_penalty({})
        self.assertEqual(p, 1.0)
        print(f"  ✅ A3b: missing vix → safe 1.0")


class TestFLong(unittest.TestCase):
    """f_long = Base_Long × max(0, 1 - P_c - 0.5×P_b) / Vol_Penalty"""

    def setUp(self):
        self.sw      = _wallet()
        # DynamicConfig에서 읽은 실제 base_long, min_cash
        self.base_long = float(self.sw._get('smart_wallet.base_long_ratio', 0.90))
        self.bear_half = float(self.sw._get('smart_wallet.bear_half_weight', 0.50))

    def test_A4_sunny_day_max_long(self):
        """P_c=P_b=0, Vol=1 → f_long=base_long (최대 롱 비중)."""
        f = self.sw.compute_f_long(p_crash=0.0, p_bear=0.0, vol_penalty=1.0)
        self.assertAlmostEqual(f, self.base_long, places=5)
        print(f"\n  ✅ A4: 맑은 날 f_long={f:.4f} (≈ base_long={self.base_long})")

    def test_A5_high_crash_prob_crushes_long(self):
        """P_c=0.9, P_b=0, Vol=1 → f_long 매우 작아야 함."""
        f = self.sw.compute_f_long(p_crash=0.9, p_bear=0.0, vol_penalty=1.0)
        # 이론값: 0.90 × max(0, 1.0 - 0.9) / 1 = 0.90 × 0.1 = 0.09
        expected = self.base_long * max(0.0, 1.0 - 0.9) / 1.0
        self.assertAlmostEqual(f, expected, places=4)
        self.assertLess(f, 0.15)  # 0.15 미만 (현금 비중이 치솟는 상태)
        print(f"  ✅ A5: P_c=0.9 → f_long={f:.4f} (expected≈{expected:.4f})")

    def test_A6_clamping_prevents_negative_f_long(self):
        """P_c=1.0, P_b=1.0 → 분자 음수 → max(0,.) Clamping → f_long=0.0."""
        # 분자: max(0.0, 1.0 - 1.0 - 0.5×1.0) = max(0.0, -0.5) = 0.0
        f = self.sw.compute_f_long(p_crash=1.0, p_bear=1.0, vol_penalty=1.0)
        self.assertEqual(f, 0.0, f"Clamping 실패: f_long={f}")
        print(f"  ✅ A6: 극단 확률 → Clamping → f_long=0.0 (숏 불가)")

    def test_A6b_float_epsilon_clamping(self):
        """부동소수점 미세 음수 (P_c=0.99999, P_b=0.00002) → f_long ≥ 0.0."""
        f = self.sw.compute_f_long(p_crash=0.99999, p_bear=0.00002, vol_penalty=1.0)
        self.assertGreaterEqual(f, 0.0, f"부동소수점 Clamping 실패: f_long={f}")
        print(f"  ✅ A6b: float epsilon → f_long={f:.8f} ≥ 0.0")

    def test_A7_vol_penalty_divides_exposure(self):
        """Vol_Penalty=2.0 → f_long 절반으로 감소."""
        f_base = self.sw.compute_f_long(p_crash=0.0, p_bear=0.0, vol_penalty=1.0)
        f_fear = self.sw.compute_f_long(p_crash=0.0, p_bear=0.0, vol_penalty=2.0)
        self.assertAlmostEqual(f_fear, f_base / 2.0, places=4)
        print(f"  ✅ A7: Vol×2 → f_long halved ({f_base:.4f} → {f_fear:.4f})")


class TestTargetCashRatio(unittest.TestCase):
    """target_cash = max(min_cash_ratio, 1.0 - f_long)"""

    def setUp(self):
        self.sw       = _wallet()
        self.min_cash = float(self.sw._get('smart_wallet.min_cash_ratio', 0.10))

    def test_A7_crash_prob_09_cash_surges(self):
        """★ 핵심 수학 증명: P_c=0.9 → 현금 비중이 수식대로 치솟는다.

        f_long = 0.90 × max(0, 1.0 - 0.9) / 1.0 = 0.09
        target_cash = max(0.10, 1 - 0.09) = max(0.10, 0.91) = 0.91
        → 91%가 현금 → Cash Sweep 불필요 상태 진입 증명.
        """
        sw = self.sw
        base_long  = float(sw._get('smart_wallet.base_long_ratio', 0.90))
        bear_half  = float(sw._get('smart_wallet.bear_half_weight', 0.50))
        min_cash   = float(sw._get('smart_wallet.min_cash_ratio', 0.10))

        p_c = 0.9
        p_b = 0.0
        vol = 1.0

        # 수학적 계산
        numerator     = max(0.0, 1.0 - p_c - bear_half * p_b)
        f_long_theory = base_long * numerator / vol
        cash_theory   = max(min_cash, 1.0 - f_long_theory)

        # 구현 계산
        cash_impl = sw.compute_target_cash(p_crash=p_c, p_bear=p_b, vol_penalty=vol)

        self.assertAlmostEqual(cash_impl, cash_theory, places=4,
                               msg=f"구현={cash_impl:.4f} ≠ 이론={cash_theory:.4f}")
        self.assertGreater(cash_impl, 0.85,
                           f"P_c=0.9일 때 현금이 85% 초과해야 함: {cash_impl:.4f}")

        print(f"\n  ✅ A7 (핵심 수학 증명): P_c=0.9")
        print(f"     numerator   = max(0, 1.0 - 0.9 - 0.5×0.0) = {numerator:.4f}")
        print(f"     f_long      = {base_long} × {numerator:.4f} / {vol} = {f_long_theory:.4f}")
        print(f"     target_cash = max({min_cash}, 1 - {f_long_theory:.4f}) = {cash_theory:.4f}")
        print(f"     구현값       = {cash_impl:.4f} → ✅ 수식 일치")
        print(f"     → 현금 {cash_impl:.0%}: Cash Sweep 불필요 상태 진입 확인")

    def test_A8_sunny_day_min_cash(self):
        """P_c=P_b=0, Vol=1 → 현금이 min_cash_ratio(10%)까지 줄어듦."""
        cash = self.sw.compute_target_cash(p_crash=0.0, p_bear=0.0, vol_penalty=1.0)
        self.assertAlmostEqual(cash, self.min_cash, places=4,
                               msg=f"맑은 날 현금={cash:.4f} ≠ min={self.min_cash}")
        print(f"\n  ✅ A8: 맑은 날 → cash={cash:.1%} = min_cash_ratio")

    def test_A8b_bear_prob_increases_cash(self):
        """P_b=0.5 (약세) → cash가 P_b=0 대비 증가해야 함."""
        cash_normal = self.sw.compute_target_cash(0.0, 0.0, 1.0)
        cash_bear   = self.sw.compute_target_cash(0.0, 0.5, 1.0)
        self.assertGreater(cash_bear, cash_normal,
                           "약세 확률 증가 시 현금 비중 증가해야 함")
        print(f"  ✅ A8b: P_b=0.5 → cash {cash_normal:.1%}→{cash_bear:.1%} 증가")


# ════════════════════════════════════════════════════════════════
# Part B — Graceful Degradation
# ════════════════════════════════════════════════════════════════

class TestGracefulDegradation(unittest.TestCase):

    def setUp(self):
        self.sw           = _wallet()
        self.default_cash = float(self.sw._get('smart_wallet.default_cash_ratio', 0.50))

    def test_B1_hmm_failure_fallback_to_conservative(self):
        """HMM 완전 고장 시 default_cash_ratio(50%)로 Fallback."""
        with patch.object(self.sw, '_get_regime_probs_from_detector',
                          side_effect=RuntimeError("HMM 연결 불가")):
            result = self.sw.allocate(market_data={'dummy': True})
        self.assertEqual(result['regime_source'], 'fallback')
        self.assertAlmostEqual(result['target_cash_ratio'], self.default_cash, places=4)
        print(f"\n  ✅ B1: HMM 고장 → Fallback cash={result['target_cash_ratio']:.1%}")

    def test_B2_missing_signal_cache_vol_penalty_safe(self):
        """signal_cache 없음 → Vol_Penalty=1.0 안전 반환."""
        p = self.sw.compute_vol_penalty(signal_cache={})
        self.assertEqual(p, 1.0)
        print(f"  ✅ B2: 빈 cache → Vol_Penalty={p}")

    def test_B3_negative_crash_prob_clamped(self):
        """음수 P_c(-0.1) → f_long 음수 없음 (Clamping 보장)."""
        f = self.sw.compute_f_long(p_crash=-0.1, p_bear=0.0, vol_penalty=1.0)
        self.assertGreaterEqual(f, 0.0)
        # 음수 입력도 f_long ≤ base_long 이어야 함
        base_long = float(self.sw._get('smart_wallet.base_long_ratio', 0.90))
        self.assertLessEqual(f, base_long)
        print(f"  ✅ B3: 음수 P_c → f_long={f:.4f} ≥ 0.0 (Clamping 작동)")

    def test_B4_sum_gt_1_still_safe(self):
        """P_c=0.8, P_b=0.8 (합>1) → f_long=0.0, cash=1.0-f_long 안전."""
        f    = self.sw.compute_f_long(p_crash=0.8, p_bear=0.8, vol_penalty=1.0)
        cash = self.sw.compute_target_cash(p_crash=0.8, p_bear=0.8, vol_penalty=1.0)
        self.assertGreaterEqual(f, 0.0)
        self.assertLessEqual(f, float(self.sw._get('smart_wallet.base_long_ratio', 0.90)))
        self.assertGreaterEqual(cash, float(self.sw._get('smart_wallet.min_cash_ratio', 0.10)))
        self.assertLessEqual(cash, 1.0)
        print(f"  ✅ B4: P_c+P_b>1 → f_long={f:.4f}, cash={cash:.4f} 안전 범위")


# ════════════════════════════════════════════════════════════════
# Part C — 확률 정규화 및 단조성
# ════════════════════════════════════════════════════════════════

class TestProbabilityProperties(unittest.TestCase):

    def setUp(self):
        self.sw = _wallet()

    def test_C1_sum_always_one(self):
        """normal + bear + crash 합은 항상 1.0 (직접 전달 모드)."""
        for crash, bear in [(0.0, 0.0), (0.3, 0.2), (0.9, 0.05), (0.5, 0.5)]:
            probs = _probs(crash, bear)
            total = probs['normal'] + probs['bear'] + probs['crash']
            self.assertAlmostEqual(total, 1.0, places=5,
                                   msg=f"합={total:.5f} ≠ 1.0 (crash={crash}, bear={bear})")
        print(f"\n  ✅ C1: 4개 케이스 확률 합 = 1.0 확인")

    def test_C2_crash_dominant_reduces_long(self):
        """P_c 증가 → f_long 단조 감소."""
        crash_probs = [0.0, 0.2, 0.4, 0.6, 0.8, 0.9]
        f_longs     = [self.sw.compute_f_long(p_crash=p, p_bear=0.0, vol_penalty=1.0)
                       for p in crash_probs]
        for i in range(1, len(f_longs)):
            self.assertLessEqual(f_longs[i], f_longs[i - 1],
                                 f"P_c 증가 시 f_long 감소 위반: "
                                 f"f({crash_probs[i-1]})={f_longs[i-1]:.4f} < "
                                 f"f({crash_probs[i]})={f_longs[i]:.4f}")
        print(f"  ✅ C2: f_long 단조 감소 확인: {[round(f,3) for f in f_longs]}")

    def test_C2b_crash_dominant_cash_sweep_not_needed(self):
        """P_c=0.5 → cash > 0.55 (매크로 Cash Sweep 불필요 조건 확인)."""
        cash = self.sw.compute_target_cash(p_crash=0.5, p_bear=0.0, vol_penalty=1.0)
        self.assertGreaterEqual(cash, 0.55,
                           f"P_c=0.5 → cash={cash:.4f}가 0.55 이상이어야 함")
        print(f"  ✅ C2b: P_c=0.5 → cash={cash:.1%} ≥ 55%")


    def test_C3_vol_penalty_monotone(self):
        """Vol_Penalty 증가 → f_long 단조 감소."""
        penalties = [1.0, 1.5, 2.0, 3.0, 5.0]
        f_longs   = [self.sw.compute_f_long(0.0, 0.0, v) for v in penalties]
        for i in range(1, len(f_longs)):
            self.assertLessEqual(f_longs[i], f_longs[i - 1],
                                 f"Vol_Penalty 증가 시 f_long 단조 감소 위반")
        print(f"  ✅ C3: Vol_Penalty 단조성: {[round(f,3) for f in f_longs]}")


# ════════════════════════════════════════════════════════════════
# Part D — 통합 allocate() 검증
# ════════════════════════════════════════════════════════════════

class TestAllocateIntegration(unittest.TestCase):

    def setUp(self):
        self.sw = _wallet()

    def test_D1_direct_mode_with_preset_probs(self):
        """direct 모드: 프리셋 확률으로 allocate() 수행."""
        probs  = {'crash': 0.7, 'bear': 0.2, 'normal': 0.1}
        result = self.sw.allocate(regime_probs=probs,
                                  signal_cache={'vkospi': 20.0, 'vkospi_ema': 15.0})
        self.assertEqual(result['regime_source'], 'direct')
        self.assertAlmostEqual(result['p_crash'], 0.7, places=3)
        self.assertAlmostEqual(result['p_bear'],  0.2, places=3)
        self.assertGreater(result['vol_penalty'], 1.0)   # vix=20 > ema=15

        # f_long 수동 검증
        base_long  = float(self.sw._get('smart_wallet.base_long_ratio', 0.90))
        bear_half  = float(self.sw._get('smart_wallet.bear_half_weight', 0.50))
        min_cash   = float(self.sw._get('smart_wallet.min_cash_ratio', 0.10))
        vol_pen    = result['vol_penalty']
        numerator  = max(0.0, 1.0 - 0.7 - bear_half * 0.2)
        f_expected = base_long * numerator / vol_pen
        cash_exp   = max(min_cash, 1.0 - f_expected)

        self.assertAlmostEqual(result['target_cash_ratio'], cash_exp, places=3)
        print(f"\n  ✅ D1: direct mode → cash={result['target_cash_ratio']:.1%}, "
              f"Vol×={vol_pen:.3f}")

    def test_D2_return_keys_complete(self):
        """allocate() 반환 딕셔너리에 필수 키가 모두 있는지 확인."""
        required_keys = [
            'target_cash_ratio', 'target_long_ratio', 'f_long',
            'vol_penalty', 'p_crash', 'p_bear', 'p_normal',
            'regime_source', 'fallback_reason',
        ]
        result = self.sw.allocate(
            regime_probs={'crash': 0.0, 'bear': 0.0, 'normal': 1.0},
            signal_cache={'vkospi': 15.0, 'vkospi_ema': 15.0},
        )
        for key in required_keys:
            self.assertIn(key, result, f"필수 키 누락: '{key}'")
        print(f"  ✅ D2: 모든 필수 키 존재 확인 ({len(required_keys)}개)")

    def test_D2b_cash_plus_long_equals_one(self):
        """target_cash_ratio + target_long_ratio = 1.0 (자본 합산 보존)."""
        for crash, bear in [(0.0, 0.0), (0.5, 0.2), (0.9, 0.0)]:
            result = self.sw.allocate(
                regime_probs={'crash': crash, 'bear': bear, 'normal': 1 - crash - bear},
                signal_cache={'vkospi': 15.0, 'vkospi_ema': 15.0},
            )
            total = result['target_cash_ratio'] + result['target_long_ratio']
            self.assertAlmostEqual(total, 1.0, places=5,
                                   msg=f"cash+long={total:.5f} ≠ 1.0 (P_c={crash})")
        print(f"  ✅ D2b: cash + long = 1.0 보존")


# ════════════════════════════════════════════════════════════════
# Entry Point
# ════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print()
    print("=" * 70)
    print(" Smart Wallet — Volatility-Scaled Merton-Kelly 단위 테스트")
    print("=" * 70)
    print()
    print("  핵심 수식:")
    print("   Vol_Penalty = max(1.0, current_vix / ema_vix)")
    print("   f_long      = Base_Long × max(0, 1 - P_c - 0.5×P_b) / Vol_Penalty")
    print("   target_cash = max(min_cash_ratio, 1.0 - f_long)")
    print()

    loader     = unittest.TestLoader()
    suite      = loader.loadTestsFromModule(__import__('__main__'))
    runner     = unittest.TextTestRunner(verbosity=2)
    result_obj = runner.run(suite)

    print()
    if result_obj.wasSuccessful():
        print("=" * 70)
        print(" ✅ ALL TESTS PASSED — Smart Wallet 수학 모델 검증 완료")
        print("=" * 70)
        sys.exit(0)
    else:
        print("=" * 70)
        print(f" ❌ FAILED: {len(result_obj.failures)} failures, "
              f"{len(result_obj.errors)} errors")
        print("=" * 70)
        sys.exit(1)
