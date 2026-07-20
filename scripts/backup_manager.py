#!/usr/bin/env python3
"""
Backup Manager — 데이터 자동 백업 + Rotation
==============================================

launchd로 매일 03:30 실행. 핵심 데이터 증분 백업.

기능:
  1. results/ 일일 스냅샷
  2. data/ 증분 백업 (rsync)
  3. config/ 스냅샷
  4. 보관 기간 자동 rotation (기본 7일)

Usage:
    python scripts/backup_manager.py              # 전체 백업
    python scripts/backup_manager.py --results     # results만
    python scripts/backup_manager.py --restore 2026-05-27  # 복구
"""

import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from config.dynamic_config import DynamicConfig

cfg = DynamicConfig()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [BACKUP] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('backup')

_BACKUP_ROOT = _PROJECT_ROOT / 'backups'


def backup_results() -> Dict:
    """results/ 디렉토리 일일 스냅샷."""
    src = _PROJECT_ROOT / 'results'
    today = date.today().isoformat()
    dst = _BACKUP_ROOT / 'results' / today

    if not src.exists():
        return {'status': 'skip', 'reason': 'no results dir'}

    dst.mkdir(parents=True, exist_ok=True)

    copied = 0
    for f in src.glob('*.json'):
        try:
            shutil.copy2(str(f), str(dst / f.name))
            copied += 1
        except Exception as e:
            logger.warning(f"  복사 실패: {f.name}: {e}")

    logger.info(f"  ✅ results/ → {dst.name}: {copied}개 파일")
    return {'status': 'ok', 'files': copied, 'path': str(dst)}


def backup_data() -> Dict:
    """data/ 증분 백업 (rsync)."""
    src = _PROJECT_ROOT / 'data'
    dst = _BACKUP_ROOT / 'data_latest'

    if not src.exists():
        return {'status': 'skip', 'reason': 'no data dir'}

    dst.mkdir(parents=True, exist_ok=True)

    try:
        result = subprocess.run(
            ['rsync', '-a', '--delete', '--exclude=__pycache__',
             str(src) + '/', str(dst) + '/'],
            capture_output=True, text=True, timeout=300)

        if result.returncode == 0:
            logger.info(f"  ✅ data/ rsync 완료")
            return {'status': 'ok'}
        else:
            logger.warning(f"  rsync 실패: {result.stderr[:200]}")
            return {'status': 'error', 'error': result.stderr[:200]}

    except FileNotFoundError:
        # rsync 없으면 shutil fallback
        logger.info("  rsync 없음 → shutil fallback")
        try:
            if dst.exists():
                shutil.rmtree(str(dst))
            shutil.copytree(str(src), str(dst),
                              ignore=shutil.ignore_patterns('__pycache__'))
            return {'status': 'ok', 'method': 'shutil'}
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return {'status': 'error', 'error': str(e)}

    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        return {'status': 'error', 'error': str(e)}


def backup_config() -> Dict:
    """config/ 스냅샷."""
    src = _PROJECT_ROOT / 'config'
    today = date.today().isoformat()
    dst = _BACKUP_ROOT / 'config' / today

    if not src.exists():
        return {'status': 'skip'}

    dst.mkdir(parents=True, exist_ok=True)

    copied = 0
    for f in src.rglob('*'):
        if f.is_file() and '__pycache__' not in str(f):
            rel = f.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(str(f), str(target))
                copied += 1
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass

    logger.info(f"  ✅ config/ → {dst.name}: {copied}개 파일")
    return {'status': 'ok', 'files': copied}


def rotate_backups() -> Dict:
    """보관 기간 초과 백업 자동 삭제."""
    retention_days = cfg.get('backup.retention_days', 7)
    cutoff = date.today() - timedelta(days=retention_days)
    deleted = 0

    for subdir in ['results', 'config']:
        backup_dir = _BACKUP_ROOT / subdir
        if not backup_dir.exists():
            continue

        for d in backup_dir.iterdir():
            if d.is_dir():
                try:
                    d_date = date.fromisoformat(d.name)
                    if d_date < cutoff:
                        shutil.rmtree(str(d))
                        deleted += 1
                        logger.info(f"  🗑 삭제: {subdir}/{d.name}")
                except (ValueError, TypeError):
                    pass

    return {'deleted': deleted, 'retention_days': retention_days}


def restore(target_date: str) -> Dict:
    """특정 날짜 백업에서 복구."""
    logger.info(f"  🔄 복구 시작: {target_date}")

    results_backup = _BACKUP_ROOT / 'results' / target_date
    if results_backup.exists():
        dst = _PROJECT_ROOT / 'results'
        copied = 0
        for f in results_backup.glob('*.json'):
            try:
                shutil.copy2(str(f), str(dst / f.name))
                copied += 1
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        logger.info(f"  ✅ results 복구: {copied}개 파일")
    else:
        logger.warning(f"  ❌ 백업 없음: results/{target_date}")
        return {'status': 'error', 'reason': 'backup not found'}

    return {'status': 'ok', 'date': target_date}


def run() -> Dict:
    """전체 백업 실행."""
    logger.info("💾 Backup Manager 시작")

    results = {
        'timestamp': datetime.now().isoformat(),
        'results': backup_results(),
        'data': backup_data(),
        'config': backup_config(),
        'rotation': rotate_backups(),
    }

    # 결과 저장
    try:
        from src.infra.safe_io import safe_json_write
        safe_json_write(_PROJECT_ROOT / 'results' / 'backup_status.json',
                          results)
    except ImportError as e:
        pass

    logger.info("  ✅ 백업 완료")
    return results


if __name__ == '__main__':
    if '--restore' in sys.argv:
        idx = sys.argv.index('--restore')
        if idx + 1 < len(sys.argv):
            restore(sys.argv[idx + 1])
        else:
            print("Usage: --restore YYYY-MM-DD")
    elif '--results' in sys.argv:
        backup_results()
    else:
        run()
