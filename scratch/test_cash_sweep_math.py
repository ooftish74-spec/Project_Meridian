#!/usr/bin/env python3
"""
Test: Mathematical Cash Sweep Refactoring
==========================================
시나리오:
  S0 기대수익률 = 5.0%  (Beta 베팅)
  마찰비용 = 0.3%  (sweep_friction_buffer)
  목표 징발 비율 = 20%

  스트림 구성:
    S5 = 5%  (유휴 현금 파킹 — Tier 1 무조건 징발)
    S3 = 2%  기대수익률  (hurdle=2.3% < S0 5% → 징발 적격)
    S1 = 8%  기대수익률  (hurdle=8.3% > S0 5% → 보호, 징발 불가)

기대 결과:
  [Tier 1] S5에서 5% 징발 완료
  [Tier 2] S3에서 15% 징발 (hurdle=2.3% < S0=5%)
  [Tier 2] S1은 절대 보호 (hurdle=8.3% > S0=5%)
  총 징발 = 20% (목표 달성)
  S1 비중 변동 없음 <- 핵심 검증 포인트
"""

import sys, os, logging, unittest

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
logger = logging.getLogger(__name__)


def apply_s0_cash_sweep_math(
    weights, s0_expected_return, target_sweep_ratio,
    stream_metrics, exempt_streams, friction_buffer,
):
    """_apply_s0_cash_sweep 핵심 수학 로직을 외부 의존성 없이 추출."""
    weights = dict(weights)
    current_total_sweep = 0.0
    needed_sweep = target_sweep_ratio
    log_lines = []

    # Tier 1: S5 무조건 최우선
    if 'S5' in weights and weights['S5'] > 0:
        available = weights['S5']
        take = min(available, needed_sweep)
        weights['S5'] -= take
        current_total_sweep += take
        needed_sweep -= take
        if take > 0:
            msg = f"  [Tier 1] S5에서 {take:.1%} 징발 (잔여={weights['S5']:.1%})"
            log_lines.append(msg); logger.info(msg)

    # Tier 2: 순수 기대수익률 경쟁
    if needed_sweep > 0:
        candidates = []
        for sid in weights:
            if sid in ('S0', 'S5') or sid in exempt_streams:
                continue
            if weights.get(sid, 0) <= 0:
                continue
            exp_ret = float(stream_metrics.get(sid, {}).get('expected_return', 0.0))
            candidates.append((sid, exp_ret, weights[sid]))

        candidates.sort(key=lambda x: x[1])  # 오름차순

        for sid, exp_ret, cur_w in candidates:
            if needed_sweep <= 0:
                break
            hurdle = exp_ret + friction_buffer
            if s0_expected_return <= hurdle:
                msg = (f"  [Tier 2] {sid} 스킵 — S0({s0_expected_return:.2%}) "
                       f"<= hurdle({hurdle:.2%})")
                log_lines.append(msg); logger.info(msg)
                continue
            take = min(cur_w, needed_sweep)
            weights[sid] -= take
            current_total_sweep += take
            needed_sweep -= take
            msg = (f"  [Tier 2] {sid}에서 {take:.1%} 징발 — "
                   f"S0({s0_expected_return:.2%}) > hurdle({hurdle:.2%})")
            log_lines.append(msg); logger.warning(msg)

    weights['S0'] = weights.get('S0', 0.0) + current_total_sweep
    shortfall = max(0.0, target_sweep_ratio - current_total_sweep)
    summary = (f"  [완료] 총 징발={current_total_sweep:.1%}, "
               f"목표={target_sweep_ratio:.1%}, 미달={shortfall:.1%}")
    log_lines.append(summary); logger.warning(summary)
    return weights, current_total_sweep, needed_sweep, log_lines


class TestMathCashSweep(unittest.TestCase):
    def setUp(self):
        self.initial_weights = {
            'S0': 0.10, 'S1': 0.40, 'S2': 0.20, 'S3': 0.25, 'S5': 0.05
        }
        self.stream_metrics = {
            'S1': {'expected_return': 0.08},
            'S2': {'expected_return': 0.04},
            'S3': {'expected_return': 0.02},
        }
        self.s0_exp    = 0.05
        self.target    = 0.20
        self.friction  = 0.003
        self.exempt    = {'S4'}

    def _run(self, weights=None, metrics=None, s0=None, target=None):
        return apply_s0_cash_sweep_math(
            weights or self.initial_weights,
            s0 if s0 is not None else self.s0_exp,
            target if target is not None else self.target,
            metrics or self.stream_metrics,
            self.exempt, self.friction,
        )

    def test_tier1_s5_fully_consumed(self):
        """S5(유휴현금)가 Tier 1에서 완전 소진되어야 함"""
        res, total, _, _ = self._run()
        self.assertAlmostEqual(res['S5'], 0.0, places=9)
        logger.info(f"PASS: S5 완전 소진 확인")

    def test_s1_completely_protected(self):
        """S1(기대수익 8%) — hurdle=8.3% > S0=5% → 한 푼도 징발 불가"""
        original = self.initial_weights['S1']
        res, _, _, _ = self._run()
        self.assertAlmostEqual(res['S1'], original, places=9,
            msg=f"S1 보호 실패: {res['S1']:.4%} != {original:.4%}")
        logger.info(f"PASS: S1 완전 보호 — {original:.1%} 그대로")

    def test_s3_liquidated_for_15pct(self):
        """S3(기대수익 2%) — hurdle=2.3% < S0=5% → 15% 징발 (20%-S5의5%)"""
        original = self.initial_weights['S3']
        res, _, _, _ = self._run()
        expected_remaining = original - 0.15
        self.assertAlmostEqual(res['S3'], expected_remaining, places=9,
            msg=f"S3 징발 실패: 기대 {expected_remaining:.2%}, 실제 {res['S3']:.2%}")
        logger.info(f"PASS: S3 {original:.1%} → {res['S3']:.1%} (-15%)")

    def test_total_sweep_equals_target(self):
        """총 징발량이 정확히 목표(20%)에 도달해야 함"""
        _, total, remaining, _ = self._run()
        self.assertAlmostEqual(total, 0.20, places=9,
            msg=f"목표 미달: total={total:.4%}")
        self.assertAlmostEqual(remaining, 0.0, places=9,
            msg=f"잔여 미달량 존재: {remaining:.4%}")
        logger.info(f"PASS: 총 징발={total:.1%} == 목표=20%")

    def test_hurdle_math_correctness(self):
        """hurdle 수식: S3 hurdle=2.3% < 5%, S1 hurdle=8.3% > 5%"""
        s1_hurdle = 0.08 + self.friction  # 8.3%
        s3_hurdle = 0.02 + self.friction  # 2.3%
        self.assertGreater(self.s0_exp, s3_hurdle)   # S3 징발 조건
        self.assertLessEqual(self.s0_exp, s1_hurdle) # S1 보호 조건
        logger.info(f"PASS: S3 hurdle={s3_hurdle:.2%} < S0={self.s0_exp:.2%} (징발)")
        logger.info(f"PASS: S1 hurdle={s1_hurdle:.2%} > S0={self.s0_exp:.2%} (보호)")

    def test_s4_always_exempt(self):
        """S4 — 초저수익(1%)이어도 exempt_streams 면제로 절대 보호"""
        w = dict(self.initial_weights); w['S4'] = 0.15
        m = dict(self.stream_metrics); m['S4'] = {'expected_return': 0.01}
        res, _, _, _ = self._run(weights=w, metrics=m)
        self.assertAlmostEqual(res['S4'], 0.15, places=9)
        logger.info("PASS: S4 면제 확인 (초저수익이어도 절대 보호)")

    def test_ascending_sort_s3_before_s2(self):
        """S3(2%) < S2(4%) 정렬 — S3가 먼저 징발되어야 함"""
        # 목표 5%: S5(0%) + S3 일부만 징발으로 충족
        w = {'S0': 0.10, 'S1': 0.40, 'S2': 0.20, 'S3': 0.25, 'S5': 0.0}
        res, _, _, _ = self._run(weights=w, target=0.05)
        # S3가 먼저 깎이고, S2는 0.20 그대로여야 함
        self.assertAlmostEqual(res['S2'], 0.20, places=9,
            msg="정렬 실패: S3(2%)보다 S2(4%)가 먼저 깎임")
        self.assertLess(res['S3'], 0.25,
            msg="S3가 징발되지 않음")
        logger.info(f"PASS: S3 먼저 징발 → S3={res['S3']:.2%}, S2={res['S2']:.2%}")


if __name__ == '__main__':
    print()
    print("=" * 68)
    print(" Mathematical Cash Sweep Refactoring — Dry-run Test")
    print("=" * 68)
    print()
    print("  S0=5%  S5=5%(idle)  S3=2%  S1=8%  마찰=0.3%  목표=20%")
    print()

    initial = {'S0': 0.10, 'S1': 0.40, 'S2': 0.20, 'S3': 0.25, 'S5': 0.05}
    metrics = {'S1': {'expected_return': 0.08},
               'S2': {'expected_return': 0.04},
               'S3': {'expected_return': 0.02}}

    res, total, remaining, _ = apply_s0_cash_sweep_math(
        initial, 0.05, 0.20, metrics, {'S4'}, 0.003)

    print()
    print("  비중 변화:")
    for sid in ['S0','S1','S2','S3','S5']:
        before = initial[sid]; after = res[sid]; delta = after - before
        mark = ''
        if sid == 'S1': mark = '  <- 보호 대상'
        elif sid == 'S3': mark = '  <- 15% 징발'
        elif sid == 'S5': mark = '  <- Tier 1 소진'
        print(f"    {sid}: {before:.1%} -> {after:.1%}  ({delta:+.1%}){mark}")
    print()
    ok_s1  = abs(res['S1'] - 0.40) < 1e-9
    ok_s3  = abs(res['S3'] - 0.10) < 1e-9
    ok_tot = abs(total - 0.20) < 1e-9
    print(f"  S1 보호:     {'PASS' if ok_s1 else 'FAIL'}")
    print(f"  S3 15% 징발: {'PASS' if ok_s3 else 'FAIL'}")
    print(f"  총 목표 달성: {'PASS' if ok_tot else 'FAIL'}")
    print()

    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromTestCase(TestMathCashSweep)
    runner = unittest.TextTestRunner(verbosity=2)
    result_obj = runner.run(suite)
    print()
    if result_obj.wasSuccessful():
        print("ALL TESTS PASSED")
        sys.exit(0)
    else:
        sys.exit(1)
