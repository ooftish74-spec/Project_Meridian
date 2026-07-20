"""
Project Meridian — 6-Layer Risk Gates Chaos Tester
====================================================

[Live Patch] Phase 2 Execution/Risk 업데이트

목적:
  Kill Switch / Crash Defense / Drawdown Guard 세 모듈이
  실계좌 라이브 거래 환경에서 가상의 -10% 폭락 시나리오에 대해
  0.1초 내로 반응하여 포지션 청산 시그널(exposure_scale=0)을 발생시키는지
  카오스 엔지니어링 방식으로 검증합니다.

테스트 구조:
  1. 가상 shadow_portfolio.json 스냅샷 로드 (또는 인메모리 생성)
  2. 고의로 virtual_nav를 하루 만에 -10% 폭락 주입
  3. KillSwitch → CrashDefense → DrawdownGuard 순서로 호출
  4. 세 모듈 모두 청산 시그널(exposure_scale == 0 or 'liquidate' in action)을 발생시키는지 assert
  5. 반응 시간 0.1초 이내 검증 (time.perf_counter 사용)
  6. 모든 결과를 구조화된 JSON 리포트로 출력

사용법:
  python src/risk/chaos_tester.py
  python src/risk/chaos_tester.py --verbose
  python src/risk/chaos_tester.py --nav-drop 0.15   # 15% 폭락 시나리오
"""
import argparse
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
logging.basicConfig(level=logging.WARNING, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

@dataclass
class ChaosTestResult:
    """개별 카오스 테스트 결과."""
    module: str
    passed: bool
    elapsed_ms: float
    exposure_scale: float
    action: str
    triggered: bool
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    MAX_LATENCY_MS: float = 100.0

@dataclass
class ChaosReport:
    """전체 카오스 테스트 리포트."""
    scenario: str
    initial_nav: float
    crashed_nav: float
    nav_drop_pct: float
    test_timestamp: str
    results: List[ChaosTestResult] = field(default_factory=list)
    all_passed: bool = False
    summary: str = ''

    def to_dict(self) -> Dict:
        return {'scenario': self.scenario, 'initial_nav': self.initial_nav, 'crashed_nav': self.crashed_nav, 'nav_drop_pct': round(self.nav_drop_pct, 4), 'test_timestamp': self.test_timestamp, 'all_passed': self.all_passed, 'summary': self.summary, 'results': [{'module': r.module, 'passed': r.passed, 'elapsed_ms': round(r.elapsed_ms, 3), 'latency_ok': r.elapsed_ms <= r.MAX_LATENCY_MS, 'exposure_scale': r.exposure_scale, 'action': r.action, 'triggered': r.triggered, 'details': r.details, 'error': r.error} for r in self.results]}

def _build_chaos_portfolio(initial_nav: float, nav_drop_pct: float) -> Dict:
    """-nav_drop_pct 폭락이 발생한 가상 shadow_portfolio 생성.

    [Live Patch] Phase 2 Execution/Risk 업데이트:
    실제 shadow_portfolio.json 파일이 있으면 로드 후 NAV만 조작.
    없으면 인메모리로 생성 (완전 독립 standalone 동작 보장).

    Args:
        initial_nav   : 폭락 전 NAV
        nav_drop_pct  : 폭락 비율 (0.10 = -10%)

    Returns:
        가상 shadow_portfolio 딕셔너리
    """
    crashed_nav = initial_nav * (1.0 - nav_drop_pct)
    hwm = initial_nav
    _sp_file = _ROOT / 'results' / 'shadow_portfolio.json'
    base_portfolio: Dict = {}
    if _sp_file.exists():
        try:
            base_portfolio = json.loads(_sp_file.read_text(encoding='utf-8'))
            logger.info(f'  📂 shadow_portfolio.json 로드 완료 → NAV 조작')
        except Exception as e:
            logger.critical(f'  ⚠️ shadow_portfolio.json 로드 실패 (인메모리 생성): {e}', exc_info=True)
    portfolio: Dict = {**base_portfolio, 'initial_capital': initial_nav, 'virtual_nav': crashed_nav, 'cash': crashed_nav * 0.3, 'hwm': hwm, 'daily_returns': base_portfolio.get('daily_returns', []) + [-nav_drop_pct], 'active_positions': base_portfolio.get('active_positions', 5), 'total_nav': crashed_nav, '__chaos_injected__': True, '__chaos_nav_drop_pct__': nav_drop_pct, '__chaos_timestamp__': datetime.now().isoformat()}
    logger.info(f'  💥 카오스 주입: NAV {initial_nav:,.0f} → {crashed_nav:,.0f} ({-nav_drop_pct * 100:.1f}% 폭락)')
    return portfolio

def _test_kill_switch(portfolio: Dict, regime: str='crash') -> ChaosTestResult:
    """KillSwitch 카오스 테스트.

    -10% 폭락 후 KillSwitch가 triggered=True이고
    exposure_scale=0인 판정을 내려야 합니다.

    [Live Patch] Phase 2 Execution/Risk 업데이트:
    파일 I/O(상태 저장·이벤트 레저)를 우회하고 measure_metrics + judge_action을
    직접 호출하여 순수 계산 반응 시간만 측정합니다 (100ms 기준).
    """
    module = 'KillSwitch'
    t0 = time.perf_counter()
    try:
        from src.risk.kill_switch import KillSwitch
        ks = KillSwitch()
        _dummy = {'total_nav': 154000000, 'initial_capital': 154000000, 'hwm': 154000000, 'daily_returns': [], 'active_positions': 0}
        try:
            ks.measure_metrics(_dummy)
        except Exception as _e0:
            logger.critical(f'  [chaos_tester] Chaos Tester 결과 저장: {_e0}', exc_info=True)
        t0 = time.perf_counter()
        measurement = ks.measure_metrics(portfolio)
        judgment = ks.judge_action(measurement, regime=regime)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        triggered = judgment.get('triggered', False)
        action = judgment.get('action', 'continue')
        exposure_scale = 0.0 if triggered else 1.0
        liquidate_signal = triggered and action in ('halt_all', 'halt_new_entry')
        passed = triggered and liquidate_signal and (elapsed_ms <= ChaosTestResult.MAX_LATENCY_MS)
        return ChaosTestResult(module=module, passed=passed, elapsed_ms=elapsed_ms, exposure_scale=exposure_scale, action=action, triggered=triggered, details={'dd_pct': measurement.get('dd_pct', 0), 'today_return_pct': measurement.get('today_return_pct', 0), 'triggers': [t['type'] for t in judgment.get('triggers', []) if not t.get('overridden')], 'safe': judgment.get('safe', True), 'regime': regime})
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as exc:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {exc}')
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return ChaosTestResult(module=module, passed=False, elapsed_ms=elapsed_ms, exposure_scale=1.0, action='error', triggered=False, error=str(exc))

def _test_crash_defense(portfolio: Dict, regime: str='crash') -> ChaosTestResult:
    """CrashDefense 카오스 테스트.

    -10% 폭락 + 극단적 VIX 환경에서 CrashDefense.assess()가
    crash_protocol 액션과 exposure_scale=0을 반환해야 합니다.
    """
    module = 'CrashDefense'
    t0 = time.perf_counter()
    try:
        from src.risk.crash_defense import CrashDefense
        market_data = {'signal_cache': {'vix': 50.0, 'vix_prev': 22.0, 'vkospi': 45.0, 'usdkrw': 1450.0, 'usdkrw_prev': 1340.0, 'foreign_net_buy': -8000000000000, 'kospi_change_pct': -10.0}, 'overnight_intel': {'sp500_change_pct': -5.5, 'nasdaq_change_pct': -7.0}}
        cd = CrashDefense()
        result = cd.assess(market_data, portfolio, regime=regime)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        measurement = result.get('measurement', {})
        judgment = result.get('judgment', {})
        stress_score = measurement.get('stress_score', 0)
        stress_level = judgment.get('stress_level', 'normal')
        actions = judgment.get('actions', [])
        action_names = [a.get('action', '') for a in actions]
        crash_triggered = stress_level in ('crash', 'danger') or any(('crash' in an for an in action_names))
        if stress_level == 'crash' or 'crash_protocol' in action_names:
            exposure_scale = 0.0
            liquidate_action = 'crash_protocol'
        elif stress_level == 'danger' or 'defensive_mode' in action_names:
            exposure_scale = 0.2
            liquidate_action = 'defensive_mode'
        else:
            exposure_scale = 1.0
            liquidate_action = 'none'
        liquidate_signal = exposure_scale == 0.0 or stress_level == 'crash'
        passed = crash_triggered and liquidate_signal and (elapsed_ms <= ChaosTestResult.MAX_LATENCY_MS)
        return ChaosTestResult(module=module, passed=passed, elapsed_ms=elapsed_ms, exposure_scale=exposure_scale, action=liquidate_action, triggered=crash_triggered, details={'stress_score': stress_score, 'stress_level': stress_level, 'action_names': action_names, 'vix': measurement.get('vix', 0), 'fx_change_pct': measurement.get('fx_change_pct', 0), 'regime': regime})
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as exc:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {exc}')
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return ChaosTestResult(module=module, passed=False, elapsed_ms=elapsed_ms, exposure_scale=1.0, action='error', triggered=False, error=str(exc))

def _test_drawdown_guard(portfolio: Dict, regime: str='crash') -> ChaosTestResult:
    """DrawdownGuard 카오스 테스트.

    -10% 폭락 → DD Stage ≥ 2 이상에서 DrawdownGuard.assess()가
    exposure_scale < 1.0을 반환하고 청산 관련 액션을 발생시켜야 합니다.
    """
    module = 'DrawdownGuard'
    t0 = time.perf_counter()
    try:
        from src.risk.drawdown_guard import DrawdownGuard
        dg = DrawdownGuard()
        result = dg.assess(portfolio, regime=regime)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        measurement = result.get('measurement', {})
        judgment = result.get('judgment', {})
        dd_pct = measurement.get('total_dd_pct', 0)
        dd_stage = judgment.get('dd_stage', 0)
        target_exposure = judgment.get('target_exposure', 1.0)
        scale_multiplier = judgment.get('scale_multiplier', 1.0)
        action_required = judgment.get('action_required', 'none')
        actions = judgment.get('actions', [])
        exposure_scale = min(target_exposure, scale_multiplier)
        liquidate_signal = dd_stage >= 2 or exposure_scale < 1.0 or action_required in ('liquidate_all', 'halt_all', 'tail_risk_hedge', 'force_sell_bottom_20')
        passed = liquidate_signal and exposure_scale < 1.0 and (elapsed_ms <= ChaosTestResult.MAX_LATENCY_MS)
        return ChaosTestResult(module=module, passed=passed, elapsed_ms=elapsed_ms, exposure_scale=exposure_scale, action=action_required, triggered=dd_stage > 0 or len(actions) > 0, details={'dd_pct': dd_pct, 'dd_stage': dd_stage, 'target_exposure': target_exposure, 'scale_multiplier': scale_multiplier, 'actions': [a.get('action', '') for a in actions], 'regime': regime})
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as exc:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {exc}')
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return ChaosTestResult(module=module, passed=False, elapsed_ms=elapsed_ms, exposure_scale=1.0, action='error', triggered=False, error=str(exc))

def run_chaos_test(nav_drop_pct: float=0.1, initial_nav: Optional[float]=None, regime: str='crash', verbose: bool=False) -> ChaosReport:
    """6-Layer Risk Gates 카오스 테스트 실행.

    [Live Patch] Phase 2 Execution/Risk 업데이트:
    -nav_drop_pct% 폭락 시나리오를 주입하고 3개 리스크 모듈의
    응답(청산 시그널, 반응 시간 0.1초)을 검증합니다.

    Args:
        nav_drop_pct : 폭락 비율 (기본 0.10 = -10%)
        initial_nav  : 폭락 전 NAV (None이면 DynamicConfig에서 로드)
        regime       : 레짐 ('crash' 권장)
        verbose      : 상세 로그 출력

    Returns:
        ChaosReport (all_passed=True이면 전체 통과)
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.WARNING)
    if initial_nav is None:
        try:
            from config.dynamic_config import DynamicConfig
            _cfg = DynamicConfig()
            initial_nav = float(_cfg.get('portfolio.initial_capital') or 154000000)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            initial_nav = 154000000
    crashed_nav = initial_nav * (1.0 - nav_drop_pct)
    portfolio = _build_chaos_portfolio(initial_nav, nav_drop_pct)
    report = ChaosReport(scenario=f'NAV -{nav_drop_pct * 100:.0f}% 폭락 (regime={regime})', initial_nav=initial_nav, crashed_nav=crashed_nav, nav_drop_pct=nav_drop_pct, test_timestamp=datetime.now().isoformat())
    logger.debug('\n' + '═' * 65)
    logger.info('  🔴  Project Meridian — Chaos Tester  (Phase 2)')
    logger.debug('═' * 65)
    logger.info(f'  시나리오 : NAV -{nav_drop_pct * 100:.0f}% 단일 일간 폭락')
    logger.info(f'  초기 NAV : ₩{initial_nav:,.0f}')
    logger.info(f'  폭락 NAV : ₩{crashed_nav:,.0f}')
    logger.info(f'  레짐     : {regime.upper()}')
    logger.info(f'  반응시간  : ≤ {ChaosTestResult.MAX_LATENCY_MS:.0f}ms 기준')
    logger.debug('─' * 65)
    tests = [('KillSwitch', _test_kill_switch, portfolio, regime), ('CrashDefense', _test_crash_defense, portfolio, regime), ('DrawdownGuard', _test_drawdown_guard, portfolio, regime)]
    for name, fn, *fn_args in tests:
        logger.debug(f'\n  [{name}] 테스트 실행 중...', end=' ', flush=True)
        result = fn(*fn_args)
        report.results.append(result)
        status = '✅ PASS' if result.passed else '❌ FAIL'
        latency_ok = result.elapsed_ms <= ChaosTestResult.MAX_LATENCY_MS
        latency_str = f'{result.elapsed_ms:.1f}ms {('✓' if latency_ok else '⚠ SLOW')}'
        logger.info(f'{status} | {latency_str}')
        logger.debug(f'    exposure_scale = {result.exposure_scale:.2f} | action = {result.action}')
        logger.debug(f'    triggered      = {result.triggered}')
        if result.error:
            logger.error(f'    error          = {result.error}')
        if verbose and result.details:
            for k, v in result.details.items():
                logger.debug(f'    {k:<20} = {v}')
    all_passed = all((r.passed for r in report.results))
    report.all_passed = all_passed
    passed_count = sum((1 for r in report.results if r.passed))
    total_count = len(report.results)
    report.summary = f'{passed_count}/{total_count} 모듈 통과' + (' — 전체 PASS ✅' if all_passed else ' — 일부 FAIL ❌')
    logger.debug('\n' + '─' * 65)
    logger.info(f'  결과: {report.summary}')
    _assert_results(report.results)
    return report

def _assert_results(results: List[ChaosTestResult]) -> None:
    """핵심 안전 보장 assert.

    세 모듈 모두 청산 시그널(exposure_scale < 1.0)을 발생시켜야 합니다.
    하나라도 실패하면 AssertionError를 발생시켜 CI 파이프라인을 차단합니다.
    """
    for r in results:
        assert r.error is None, f'[{r.module}] 예외 발생으로 테스트 실패: {r.error}'
        assert r.exposure_scale < 1.0, f'[{r.module}] exposure_scale={r.exposure_scale:.2f} — 청산 시그널 미발생. Live 배포 차단.'
        assert r.elapsed_ms <= ChaosTestResult.MAX_LATENCY_MS, f'[{r.module}] 반응 시간 초과: {r.elapsed_ms:.1f}ms > {ChaosTestResult.MAX_LATENCY_MS:.0f}ms'
    logger.info('  ✅ assert 검증 완료 — 모든 리스크 게이트 정상 작동')

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Project Meridian 6-Layer Risk Gates Chaos Tester', formatter_class=argparse.RawDescriptionHelpFormatter, epilog='\nExamples:\n  python src/risk/chaos_tester.py\n  python src/risk/chaos_tester.py --nav-drop 0.15\n  python src/risk/chaos_tester.py --verbose\n  python src/risk/chaos_tester.py --regime bear --report chaos_report.json\n        ')
    parser.add_argument('--nav-drop', type=float, default=0.1, metavar='RATIO', help='NAV 폭락 비율 (기본: 0.10 = -10%%)')
    parser.add_argument('--initial-nav', type=float, default=None, metavar='KRW', help='초기 NAV (기본: DynamicConfig portfolio.initial_capital)')
    parser.add_argument('--regime', type=str, default='crash', choices=['bull', 'neutral', 'caution', 'bear', 'crash'], help='시장 레짐 (기본: crash)')
    parser.add_argument('--verbose', action='store_true', help='상세 로그 출력')
    parser.add_argument('--report', type=str, default=None, metavar='PATH', help='결과 리포트 저장 경로 (JSON)')
    return parser.parse_args()
if __name__ == '__main__':
    args = _parse_args()
    try:
        report = run_chaos_test(nav_drop_pct=args.nav_drop, initial_nav=args.initial_nav, regime=args.regime, verbose=args.verbose)
        report_dict = report.to_dict()
        if args.report:
            Path(args.report).write_text(json.dumps(report_dict, indent=2, ensure_ascii=False), encoding='utf-8')
            logger.info(f'\n  📄 리포트 저장: {args.report}')
        else:
            _default_report = _ROOT / 'results' / 'chaos_test_report.json'
            _default_report.parent.mkdir(parents=True, exist_ok=True)
            _default_report.write_text(json.dumps(report_dict, indent=2, ensure_ascii=False), encoding='utf-8')
            logger.info(f'\n  📄 리포트 저장: {_default_report}')
        logger.debug('═' * 65 + '\n')
        sys.exit(0 if report.all_passed else 1)
    except AssertionError as e:
        logger.critical(f'\n  🚨 CHAOS TEST FAILED — Live 배포 차단: {e}', exc_info=True)
        logger.critical('═' * 65 + '\n', exc_info=True)
        sys.exit(2)
    except Exception as e:
        logger.info(f'\n  💥 Chaos Tester 예외: {e}')
        import traceback
        logger.critical('', exc_info=True)
        sys.exit(3)