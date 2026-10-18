"""
Project Meridian — Go/No-Go (DEPRECATED → Live Diagnostics wrapper)
=====================================================================
★ Track E (Wave 3) 정책 변경: Go/No-Go 게이트(PSR/DSR/MinTRL 단일 합격/불합격)는 **폐지**되었다.
시스템은 소액 실계좌로 계속 매매하며 매일 연속 진단/교정 루프를 수행한다
(``src/measurement/live_diagnostics.py``).

이 모듈은 기존 import 경로(``from scripts.go_nogo import GoNoGoEngine``) 호환을 위한 얇은 래퍼다.
  - ``evaluate()`` / ``evaluate_meridian()`` → live diagnostics 실행 후 **항상 비차단** 결과 반환
    (verdict = decision = 'DIAGNOSE_CONTINUE', blocking = False)
  - go_nogo.json 은 더 이상 이 모듈이 쓰지 않는다 (대시보드용 go_nogo.json 은 MeasurementEngine
    SSoT 동기화(sync_ssot_from_me)가 정보용 뷰로 기록).

Usage:
    python scripts/go_nogo.py      # == python scripts/live_diagnostics.py
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from src.utils.time_utils import now_kst
except ImportError:
    def now_kst():
        return datetime.now()

logger = logging.getLogger(__name__)
_RESULTS = _PROJECT_ROOT / 'results'

DECISION = 'DIAGNOSE_CONTINUE'


class GoNoGoEngine:
    """[DEPRECATED] 비차단 Live Diagnostics 래퍼 (하위 호환)."""

    def __init__(self, results_dir: Path = None, write: bool = True):
        self.results_dir = Path(results_dir) if results_dir else _RESULTS
        self.write = write

    def _diagnose(self, scope: str) -> dict:
        try:
            from src.measurement.live_diagnostics import run_live_diagnostics
            diag = run_live_diagnostics(results_dir=self.results_dir, write=self.write)
        except Exception as e:  # 진단 실패도 절대 차단하지 않음
            logger.error(f"  Live Diagnostics 실패 (비차단): {e}", exc_info=True)
            diag = {'decision': DECISION, 'blocking': False, 'status': 'error', 'error': str(e),
                    'issues': [], 'summary': {}}
        issues = diag.get('issues') or []
        result = {
            'verdict': DECISION,
            'decision': DECISION,
            'blocking': False,
            'scope': scope,
            'deprecated': 'Go/No-Go 게이트 폐지 — scripts/live_diagnostics.py 사용',
            # 레거시 소비자(log/대시보드)용: criteria 는 모두 정보용 + pass=True (차단 의미 없음)
            'criteria': {
                i['id']: {'value': i.get('title'), 'threshold': 'info', 'pass': True,
                          'severity': i.get('severity'), 'detail': i.get('recommendation')}
                for i in issues
            },
            'has_pending': False,
            'diagnostics_summary': diag.get('summary', {}),
            'n_issues': len(issues),
            'timestamp': now_kst().isoformat(),
        }
        logger.info(f"  🩺 Live Diagnostics({scope}): decision={DECISION} (blocking=False), issues={len(issues)}")
        return result

    def evaluate(self) -> dict:
        """[DEPRECATED] 레거시 종합 판정 → 비차단 진단 결과."""
        return self._diagnose('legacy')

    def evaluate_meridian(self) -> dict:
        """[DEPRECATED] Meridian 판정 → 비차단 진단 결과."""
        return self._diagnose('meridian')

    # ── 하위 호환 헬퍼 (외부 사용 가능성) ──
    @staticmethod
    def _calc_mdd(records: list) -> float:
        """NAV 레코드 MDD (% 단위, ≤0). perf_stats 단일 정의 사용."""
        from src.measurement.perf_stats import max_drawdown
        navs = [r.get('nav') for r in (records or []) if isinstance(r, dict)]
        v = max_drawdown(navs)['value']
        return (v * 100.0) if v is not None else 0.0

    def _get_shadow_start_date(self):
        """섀도 시작일 (shadow_portfolio → config → 오늘)."""
        from datetime import date
        sp_file = self.results_dir / 'shadow_portfolio.json'
        if sp_file.exists():
            try:
                sp = json.loads(sp_file.read_text(encoding='utf-8'))
                start_str = sp.get('shadow_start_date') or sp.get('start_date')
                if not start_str:
                    snaps = sp.get('daily_snapshots', [])
                    if snaps:
                        start_str = snaps[0].get('date')
                if start_str:
                    return date.fromisoformat(str(start_str)[:10])
            except Exception as e:
                logger.debug(f"  shadow_portfolio.json 날짜 파싱 실패: {e}")
        try:
            from config.dynamic_config import DynamicConfig
            start_str = DynamicConfig().get('gonogo.shadow_start_date')
            if start_str:
                return date.fromisoformat(str(start_str)[:10])
        except Exception as e:
            logger.debug(f'Targeted fallback: {e}')
        return date.today()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    from scripts.live_diagnostics import main
    sys.exit(main())
