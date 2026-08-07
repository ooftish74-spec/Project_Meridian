#!/usr/bin/env python3
"""
Pipeline Checkpoint — 체크포인트 + 자동 재실행
===============================================

Phase 완료 시 체크포인트 저장, 재실행 시 완료된 Phase skip.

Usage:
    from src.infra.pipeline_checkpoint import PipelineCheckpoint
    ckpt = PipelineCheckpoint()

    if ckpt.should_run('collect'):
        run_collect()
        ckpt.mark_done('collect')
    else:
        logger.info('collect 이미 완료 → skip')
"""

import json
import logging
from datetime import datetime, date
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
_CKPT_FILE = _RESULTS / 'pipeline_checkpoint.json'

try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _cfg = None


class PipelineCheckpoint:
    """Phase별 체크포인트 관리."""

    def __init__(self):
        self._state = self._load()

    def should_run(self, phase: str) -> bool:
        """이 Phase를 실행해야 하는지 판단.

        같은 날 이미 성공적으로 완료했으면 skip.
        실패했던 Phase는 max_retries까지 재시도.
        """
        today = date.today().isoformat()

        # 다른 날이면 리셋
        if self._state.get('date') != today:
            self._state = {'date': today, 'phases': {}}
            self._save()
            return True

        phase_info = self._state.get('phases', {}).get(phase, {})
        status = phase_info.get('status')

        if status == 'done':
            return False  # 이미 완료

        if status == 'failed':
            max_retries = (_cfg.get('checkpoint.max_retries', 2)
                             if _cfg else 2)
            retries = phase_info.get('retries', 0)
            if retries >= max_retries:
                logger.warning(
                    f"  ⏭ {phase}: max retries ({max_retries}) 초과 → skip")
                return False
            return True  # 재시도

        return True  # 첫 실행

    def mark_running(self, phase: str) -> None:
        """Phase 실행 시작 기록."""
        today = date.today().isoformat()
        if self._state.get('date') != today:
            self._state = {'date': today, 'phases': {}}

        phases = self._state.setdefault('phases', {})
        phases[phase] = {
            'status': 'running',
            'started_at': datetime.now().isoformat(),
            'retries': phases.get(phase, {}).get('retries', 0),
        }
        self._save()

    def mark_done(self, phase: str, duration_sec: float = 0) -> None:
        """Phase 완료 기록."""
        phases = self._state.setdefault('phases', {})
        info = phases.get(phase, {})
        info.update({
            'status': 'done',
            'completed_at': datetime.now().isoformat(),
            'duration_sec': round(duration_sec, 1),
        })
        phases[phase] = info
        self._save()
        logger.debug(f"  ✅ Checkpoint: {phase} done ({duration_sec:.1f}s)")

    def mark_failed(self, phase: str, error: str = '') -> None:
        """Phase 실패 기록."""
        phases = self._state.setdefault('phases', {})
        info = phases.get(phase, {})
        retries = info.get('retries', 0)
        info.update({
            'status': 'failed',
            'failed_at': datetime.now().isoformat(),
            'error': str(error)[:200],
            'retries': retries + 1,
        })
        phases[phase] = info
        self._save()
        logger.warning(
            f"  ❌ Checkpoint: {phase} failed (retry {retries + 1})")

    def get_status(self) -> Dict:
        """오늘 체크포인트 상태."""
        today = date.today().isoformat()
        if self._state.get('date') != today:
            return {'date': today, 'phases': {}, 'all_done': False}

        phases = self._state.get('phases', {})
        n_done = sum(1 for p in phases.values()
                       if p.get('status') == 'done')
        n_failed = sum(1 for p in phases.values()
                         if p.get('status') == 'failed')

        return {
            'date': today,
            'phases': phases,
            'n_done': n_done,
            'n_failed': n_failed,
            'n_total': len(phases),
        }

    def get_incomplete_phases(self, all_phases: List[str]) -> List[str]:
        """미완료 Phase 목록."""
        today = date.today().isoformat()
        if self._state.get('date') != today:
            return list(all_phases)

        phases = self._state.get('phases', {})
        return [p for p in all_phases
                if phases.get(p, {}).get('status') != 'done']

    def reset_today(self) -> None:
        """오늘 체크포인트 초기화."""
        self._state = {'date': date.today().isoformat(), 'phases': {}}
        self._save()

    def _load(self) -> Dict:
        try:
            from src.infra.safe_io import safe_json_read
            return safe_json_read(_CKPT_FILE, {})
        except ImportError as e:
            if _CKPT_FILE.exists():
                try:
                    return json.loads(_CKPT_FILE.read_text())
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).error(f'Targeted fallback: {e}', exc_info=True)
                    pass
        return {}

    def _save(self) -> None:
        """Checkpoint 저장 — ★ Race condition 방지: 기존 파일 merge 후 저장."""
        try:
            # ★ 동시 실행 프로세스 간 경합 방지:
            #   다른 LaunchAgent가 저장한 최신 상태를 먼저 읽고,
            #   현재 프로세스의 phase만 업데이트한 뒤 저장
            on_disk = {}
            if _CKPT_FILE.exists():
                try:
                    on_disk = json.loads(_CKPT_FILE.read_text())
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    pass

            # 같은 날이면 merge, 다른 날이면 현재 state 우선
            if on_disk.get('date') == self._state.get('date'):
                merged_phases = on_disk.get('phases', {})
                # 현재 프로세스의 phase 업데이트만 반영
                merged_phases.update(self._state.get('phases', {}))
                on_disk['phases'] = merged_phases
                self._state = on_disk
            # 다른 날이면 self._state가 리셋된 상태이므로 그대로 저장

            _CKPT_FILE.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(_CKPT_FILE, 
                self._state, indent=2, default=str)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
