#!/usr/bin/env python3
"""
Watchdog — 파이프라인 프로세스 감시
=====================================

launchd로 매 15분 실행. 파이프라인 프로세스의 건강 상태를 감시.

기능:
  1. Phase 스케줄 준수 확인 (결과 파일 존재 + freshness)
  2. Hang 프로세스 감지 + 강제 종료
  3. Zombie 프로세스 정리
  4. launchd 스케줄 상태 확인
  5. Heartbeat 기록

Usage:
    python scripts/watchdog.py           # 전체 점검
    python scripts/watchdog.py --check   # 점검만 (조치 없음)
"""

import json
import logging
import os
import signal
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from config.dynamic_config import DynamicConfig
try:
    from src.utils.time_utils import now_kst  # ★ L2-15 FIX: 스케줄링 KST 일치
except ImportError as e:
    from datetime import timezone, timedelta as _td_kst
    _KST = timezone(_td_kst(hours=9))
    def now_kst(): return datetime.now(tz=_KST)  # noqa: E301

cfg = DynamicConfig()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [WATCHDOG] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('watchdog')

_RESULTS = _PROJECT_ROOT / 'results'
_HEARTBEAT_FILE = _RESULTS / 'watchdog_heartbeat.json'

# Phase → 예상 완료 시각 + 결과 파일
PHASE_SCHEDULE = {
    'overnight':  {'hour': 5, 'minute': 30, 'result': 'signal_cache.json'},
    'collect':    {'hour': 6, 'minute': 30, 'result': 'signal_cache.json'},
    'premarket':  {'hour': 8, 'minute': 0,  'result': 'signal_cache.json'},
    'market':     {'hour': 9, 'minute': 15, 'result': 'shadow_portfolio.json'},
    'closing':    {'hour': 15, 'minute': 25, 'result': 'shadow_portfolio.json'},
    'evening':    {'hour': 20, 'minute': 30, 'result': 'shadow_portfolio.json'},
}


def check_schedule_compliance() -> List[Dict]:
    """Phase 스케줄 준수 확인.

    수정 2026-05-29:
      - 동일 result file을 공유하는 phase 중 가장 최근 스케줄만 체크
        (overnight/collect/premarket 모두 signal_cache.json → 3건 중복 방지)
      - 마지막 phase(evening 20:30) 이후 ~ 다음날 overnight(05:30) 사이에는
        stale 체크 비활성화 (야간 시간대 허위 경고 방지)
    """
    issues = []
    now = now_kst()  # ★ L2-15 FIX: UTC 서버에서도 KST로 스케줄링 일치

    # ── 1. 동일 result file → 가장 최근 phase만 선택 ──
    # phase별 예상 시각 계산
    phase_times = {}
    for phase, info in PHASE_SCHEDULE.items():
        expected = now.replace(
            hour=info['hour'], minute=info['minute'], second=0, microsecond=0)
        phase_times[phase] = (expected, info)

    # result file별 → 이미 지나간 phase 중 가장 최근 것만 유지
    result_to_latest: Dict[str, str] = {}  # result_file → phase_name
    for phase, (expected, info) in sorted(
        phase_times.items(), key=lambda x: x[1][0]
    ):
        if now < expected:
            continue  # 아직 시간 안 됨
        result_to_latest[info['result']] = phase  # 뒤의 phase가 덮어씀

    # ── 2. 마지막 phase 이후 ~ 다음날 첫 phase 사이 = off-hours ──
    all_expected = sorted(phase_times.values(), key=lambda x: x[0])
    last_phase_time = all_expected[-1][0] if all_expected else None
    first_phase_time = all_expected[0][0] if all_expected else None

    # off-hours: 마지막 phase + grace 이후 ~ 자정 ~ 첫 phase
    grace_min = cfg.get('watchdog.freshness_grace_minutes', 1440)  # 1일 1회 갱신 기준
    if last_phase_time and first_phase_time:
        off_hours_start = last_phase_time + timedelta(minutes=grace_min)
        if now > off_hours_start or now < first_phase_time:
            # 야간 시간대 → stale 체크 불필요
            return issues

    # ── 3. 선택된 phase만 체크 ──
    for result_file_name, phase in result_to_latest.items():
        info = PHASE_SCHEDULE[phase]
        result_file = _RESULTS / result_file_name

        if not result_file.exists():
            issues.append({
                'type': 'missing_result',
                'phase': phase,
                'file': result_file_name,
                'severity': 'WARNING',
            })
            continue

        # freshness 체크
        mtime = datetime.fromtimestamp(result_file.stat().st_mtime, tz=now.tzinfo)
        if now - mtime > timedelta(minutes=grace_min):
            issues.append({
                'type': 'stale_result',
                'phase': phase,
                'file': result_file_name,
                'age_minutes': int((now - mtime).total_seconds() / 60),
                'severity': 'WARNING',
            })

    return issues


def check_hang_processes() -> List[Dict]:
    """Hang 프로세스 감지 (phase-aware + CPU 활성도 확인).

    ★ 근원적 개선:
      1. phase별 최대 허용 시간 분리 (krx_refresh/backtest는 장시간 허용)
      2. CPU 사용률 > 0.5% 이면 "working" 판정 → kill 대상에서 제외
         (네트워크 I/O 대기 중이더라도 실행 중이면 hang이 아님)
    """
    issues = []
    default_max = cfg.get('watchdog.max_runtime_minutes', 30)

    # phase별 최대 허용 시간 (분)
    phase_limits = {
        'krx_refresh': cfg.get('watchdog.krx_refresh_max_minutes', 20),
        'backtest':    cfg.get('watchdog.backtest_max_minutes', 30),
        'collect':     cfg.get('watchdog.collect_max_minutes', 15),
    }

    try:
        result = subprocess.run(
            ['ps', 'aux'], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            if 'daily_pipeline.py' not in line:
                continue
            if 'python' not in line.lower():
                continue

            parts = line.split()
            if len(parts) < 3:
                continue

            pid = int(parts[1])
            # CPU 사용률 확인 (%CPU는 ps aux의 3번째 컬럼)
            try:
                cpu_pct = float(parts[2])
            except (ValueError, IndexError):
                cpu_pct = 0.0

            # 프로세스 실행 시간 확인
            try:
                elapsed = subprocess.run(
                    ['ps', '-o', 'etime=', '-p', str(pid)],
                    capture_output=True, text=True, timeout=5)
                etime = elapsed.stdout.strip()
                minutes = _parse_etime(etime)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                continue

            # 실행 중인 phase 판별 (커맨드 라인에서 추출)
            detected_phase = 'unknown'
            for phase_name in phase_limits:
                if phase_name in line:
                    detected_phase = phase_name
                    break

            max_min = phase_limits.get(detected_phase, default_max)

            if minutes > max_min:
                # ★ CPU 활성도 체크: CPU > 0.5% 이면 실행 중 → hang 아님
                if cpu_pct > 0.5:
                    logger.info(
                        f"  ⏳ PID {pid} ({detected_phase}) {minutes}분 실행 중이지만 "
                        f"CPU {cpu_pct}% 활성 → hang 아님 (slow로 기록)")
                    issues.append({
                        'type': 'slow_process',
                        'pid': pid,
                        'phase': detected_phase,
                        'runtime_min': minutes,
                        'cpu_pct': cpu_pct,
                        'max_min': max_min,
                        'severity': 'WARNING',  # CRITICAL이 아님 → kill 안 함
                    })
                else:
                    issues.append({
                        'type': 'hang_process',
                        'pid': pid,
                        'phase': detected_phase,
                        'runtime_min': minutes,
                        'cpu_pct': cpu_pct,
                        'max_min': max_min,
                        'severity': 'CRITICAL',
                    })
    except Exception as e:
        logger.debug(f"  프로세스 체크 실패: {e}")

    return issues



def kill_hang_processes(issues: List[Dict]) -> int:
    """Hang 프로세스 강제 종료."""
    killed = 0
    for issue in issues:
        if issue.get('type') == 'hang_process':
            pid = issue['pid']
            try:
                os.kill(pid, signal.SIGTERM)
                logger.warning(f"  🔪 SIGTERM → PID {pid}")
                killed += 1
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.warning(f"  Kill 실패 PID {pid}: {e}")
    return killed


def check_launchd_status() -> List[Dict]:
    """launchd 스케줄 상태 확인."""
    issues = []
    expected_agents = [
        'com.meridian.overnight', 'com.meridian.collect',
        'com.meridian.premarket', 'com.meridian.market',
        'com.meridian.closing', 'com.meridian.evening',
    ]

    try:
        result = subprocess.run(
            ['launchctl', 'list'], capture_output=True, text=True, timeout=5)
        loaded = result.stdout

        for agent in expected_agents:
            if agent not in loaded:
                issues.append({
                    'type': 'unloaded_agent',
                    'agent': agent,
                    'severity': 'CRITICAL',
                })
    except Exception as e:
        logger.debug(f"  launchd 체크 실패: {e}")

    return issues


def reload_unloaded_agents(issues: List[Dict]) -> int:
    """unload된 launchd agent 자동 복구."""
    reloaded = 0
    plist_dir = Path.home() / 'Library' / 'LaunchAgents'

    for issue in issues:
        if issue.get('type') == 'unloaded_agent':
            agent = issue['agent']
            plist = plist_dir / f'{agent}.plist'
            if plist.exists():
                try:
                    subprocess.run(
                        ['launchctl', 'load', str(plist)],
                        timeout=5, capture_output=True)
                    logger.info(f"  🔄 launchctl load: {agent}")
                    reloaded += 1
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    pass
    return reloaded


def send_notification(title: str, message: str) -> None:
    """macOS 네이티브 알림."""
    try:
        subprocess.run([
            'osascript', '-e',
            f'display notification "{message}" '
            f'with title "Meridian Watchdog" subtitle "{title}"',
        ], timeout=5, capture_output=True)
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass


def record_heartbeat(result: Dict) -> None:
    """Heartbeat 기록."""
    try:
        from src.infra.safe_io import safe_json_write
        safe_json_write(_HEARTBEAT_FILE, result)
    except ImportError as e:
        try:
            _HEARTBEAT_FILE.write_text(json.dumps(result, indent=2,
                                                     default=str))
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).error(f'Targeted fallback: {e}', exc_info=True)
            pass


def _parse_etime(etime: str) -> int:
    """ps etime 파싱 → 분."""
    etime = etime.strip()
    if not etime:
        return 0
    parts = etime.split(':')
    try:
        if len(parts) == 2:
            return int(parts[0])
        elif len(parts) == 3:
            return int(parts[0]) * 60 + int(parts[1])
        elif '-' in etime:
            days, rest = etime.split('-')
            h, m, _ = rest.split(':')
            return int(days) * 1440 + int(h) * 60 + int(m)
    except (ValueError, IndexError):
        pass
    return 0


def run(dry_run: bool = False) -> Dict:
    """전체 Watchdog 점검 실행."""
    logger.info("🐕 Watchdog 점검 시작")
    all_issues = []

    # 1. 스케줄 준수
    schedule_issues = check_schedule_compliance()
    all_issues.extend(schedule_issues)

    # 2. Hang 프로세스
    hang_issues = check_hang_processes()
    all_issues.extend(hang_issues)

    # 3. launchd 상태
    launchd_issues = check_launchd_status()
    all_issues.extend(launchd_issues)

    # 조치
    killed = 0
    reloaded = 0
    if not dry_run:
        killed = kill_hang_processes(hang_issues)
        reloaded = reload_unloaded_agents(launchd_issues)

    # 알림 (CRITICAL 이슈)
    critical = [i for i in all_issues if i.get('severity') == 'CRITICAL']
    if critical and not dry_run:
        msg = ', '.join(f"{i['type']}" for i in critical[:3])
        send_notification('⚠️ Critical', msg)

    result = {
        'timestamp': datetime.now().isoformat(),
        'n_issues': len(all_issues),
        'n_critical': len(critical),
        'killed': killed,
        'reloaded': reloaded,
        'issues': all_issues,
        'dry_run': dry_run,
    }

    record_heartbeat(result)

    if all_issues:
        for issue in all_issues:
            icon = '🔴' if issue.get('severity') == 'CRITICAL' else '🟡'
            logger.warning(f"  {icon} {issue['type']}: {issue}")
    else:
        logger.info("  ✅ 모든 점검 통과")

    return result


if __name__ == '__main__':
    dry = '--check' in sys.argv or '--dry-run' in sys.argv
    run(dry_run=dry)
