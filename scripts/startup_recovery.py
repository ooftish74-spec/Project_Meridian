#!/usr/bin/env python3
"""
Startup Recovery — 재부팅 후 상태 복구
========================================

맥북 재시작 후 자동 실행. 파이프라인 상태 정합성 진단 + 복구.

기능:
  1. 체크포인트 상태 진단 (어제 미완료 phase 확인)
  2. 결과 파일 무결성 검증 (손상 → .bak 복구)
  3. Circuit Breaker 리셋 (재부팅이면 OPEN → CLOSED)
  4. launchd agent 상태 확인 + 자동 load
  5. 복구 리포트 생성

Usage:
    python scripts/startup_recovery.py
"""

import json
import logging
import subprocess
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [RECOVERY] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('recovery')

_RESULTS = _PROJECT_ROOT / 'results'


def diagnose_checkpoint() -> Dict:
    """체크포인트 상태 진단."""
    ckpt_file = _RESULTS / 'pipeline_checkpoint.json'
    if not ckpt_file.exists():
        return {'status': 'no_checkpoint', 'action': 'fresh_start'}

    try:
        data = json.loads(ckpt_file.read_text())
        ckpt_date = data.get('date', '')
        today = date.today().isoformat()

        if ckpt_date == today:
            phases = data.get('phases', {})
            failed = [p for p, info in phases.items()
                        if info.get('status') == 'failed']
            running = [p for p, info in phases.items()
                         if info.get('status') == 'running']

            if running:
                # 재부팅으로 running 상태가 남은 것 → failed로 전환
                for p in running:
                    phases[p]['status'] = 'failed'
                    phases[p]['error'] = 'interrupted_by_restart'
                data['phases'] = phases
                ckpt_file.write_text(json.dumps(data, indent=2))
                logger.warning(f"  🔄 running → failed 전환: {running}")

            return {
                'status': 'today',
                'failed': failed,
                'running_recovered': running,
            }
        else:
            return {'status': 'stale', 'date': ckpt_date,
                      'action': 'new_day'}

    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        return {'status': 'error', 'error': str(e)}


def repair_corrupted_files() -> Dict:
    """결과 파일 무결성 검증 + .bak 복구."""
    repaired = []
    corrupted = []

    for f in _RESULTS.glob('*.json'):
        try:
            json.loads(f.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            bak = f.with_suffix(f.suffix + '.bak')
            if bak.exists():
                try:
                    json.loads(bak.read_text(encoding='utf-8'))
                    # bak 정상 → 복구
                    import shutil
                    shutil.copy2(str(bak), str(f))
                    repaired.append(f.name)
                    logger.info(f"  ✅ 복구: {f.name} ← .bak")
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    corrupted.append(f.name)
            else:
                corrupted.append(f.name)
                logger.warning(f"  ❌ 복구 불가: {f.name} (bak 없음)")

    return {'repaired': repaired, 'corrupted': corrupted}


def reset_circuit_breakers() -> int:
    """재부팅 시 Circuit Breaker 리셋."""
    cb_file = _RESULTS / 'circuit_breaker.json'
    if not cb_file.exists():
        return 0

    try:
        data = json.loads(cb_file.read_text())
        reset_count = 0
        for name, state in data.items():
            if state.get('state') == 'OPEN':
                state['state'] = 'CLOSED'
                state['failures'] = 0
                reset_count += 1
                logger.info(f"  🔄 CircuitBreaker [{name}]: OPEN → CLOSED")

        if reset_count > 0:
            cb_file.write_text(json.dumps(data, indent=2, default=str))

        return reset_count

    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        return 0


def verify_launchd() -> Dict:
    """launchd agent 상태 확인 + 자동 load."""
    plist_dir = Path.home() / 'Library' / 'LaunchAgents'
    meridian_plists = list(plist_dir.glob('com.meridian.*.plist'))

    loaded = set()
    try:
        result = subprocess.run(
            ['launchctl', 'list'], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            if 'com.meridian' in line:
                parts = line.split()
                if parts:
                    loaded.add(parts[-1])
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass

    missing = []
    reloaded = []
    for plist in meridian_plists:
        name = plist.stem
        if name not in loaded:
            missing.append(name)
            try:
                subprocess.run(
                    ['launchctl', 'load', str(plist)],
                    timeout=5, capture_output=True)
                reloaded.append(name)
                logger.info(f"  🔄 launchctl load: {name}")
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass

    return {
        'total_plists': len(meridian_plists),
        'loaded': len(loaded),
        'missing': missing,
        'reloaded': reloaded,
    }


def run() -> Dict:
    """전체 복구 프로세스."""
    logger.info("🔧 Startup Recovery 시작")

    result = {
        'timestamp': datetime.now().isoformat(),
        'checkpoint': diagnose_checkpoint(),
        'file_repair': repair_corrupted_files(),
        'circuit_breakers_reset': reset_circuit_breakers(),
        'launchd': verify_launchd(),
    }

    # 결과 저장
    try:
        from src.infra.safe_io import safe_json_write
        safe_json_write(_RESULTS / 'startup_recovery.json', result)
    except ImportError as e:
        (_RESULTS / 'startup_recovery.json').write_text(
            json.dumps(result, indent=2, default=str))

    logger.info("  ✅ Startup Recovery 완료")
    return result


if __name__ == '__main__':
    run()
