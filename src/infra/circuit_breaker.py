#!/usr/bin/env python3
"""
Circuit Breaker — API 회로 차단기
====================================

API 연속 실패 시 자동 차단 → 복구 대기 → 반개방 시도.

상태 전환:
  CLOSED  → [failure_threshold 초과] → OPEN
  OPEN    → [recovery_timeout 경과]  → HALF_OPEN
  HALF_OPEN → [성공] → CLOSED
  HALF_OPEN → [실패] → OPEN

Usage:
    from src.infra.circuit_breaker import CircuitBreaker
    cb = CircuitBreaker('kospi_api')
    if cb.allow_request():
        try:
            result = call_api()
            cb.record_success()
        except Exception:
            cb.record_failure()
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_STATE_FILE = _PROJECT_ROOT / 'results' / 'circuit_breaker.json'

try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _cfg = None


class CircuitBreaker:
    """API별 독립 회로 차단기."""

    # 전역 상태 공유 (프로세스 내)
    _states: Dict[str, Dict] = {}
    _loaded = False

    CLOSED = 'CLOSED'
    OPEN = 'OPEN'
    HALF_OPEN = 'HALF_OPEN'

    def __init__(self, name: str):
        """
        Args:
            name: API/수집기 이름 (예: 'kospi_api', 'yfinance')
        """
        self.name = name
        if not CircuitBreaker._loaded:
            CircuitBreaker._load_all()

        if name not in CircuitBreaker._states:
            CircuitBreaker._states[name] = {
                'state': self.CLOSED,
                'failures': 0,
                'successes': 0,
                'last_failure': None,
                'last_success': None,
                'opened_at': None,
            }

    @property
    def _s(self) -> Dict:
        return CircuitBreaker._states[self.name]

    def allow_request(self) -> bool:
        """요청 가능 여부 판단."""
        state = self._s['state']

        if state == self.CLOSED:
            return True

        if state == self.OPEN:
            timeout = self._get_timeout()
            opened = self._parse_ts(self._s.get('opened_at'))
            if opened and datetime.now() - opened > timedelta(seconds=timeout):
                self._s['state'] = self.HALF_OPEN
                logger.info(f"  🔄 CircuitBreaker [{self.name}]: "
                              f"OPEN → HALF_OPEN")
                return True
            return False

        # HALF_OPEN: 1회 시도 허용
        return True

    def record_success(self) -> None:
        """성공 기록."""
        self._s['successes'] += 1
        self._s['last_success'] = datetime.now().isoformat()

        if self._s['state'] == self.HALF_OPEN:
            self._s['state'] = self.CLOSED
            self._s['failures'] = 0
            logger.info(f"  ✅ CircuitBreaker [{self.name}]: "
                          f"HALF_OPEN → CLOSED")

        self._save()

    def record_failure(self) -> None:
        """실패 기록."""
        self._s['failures'] += 1
        self._s['last_failure'] = datetime.now().isoformat()

        threshold = self._get_threshold()

        if self._s['state'] == self.HALF_OPEN:
            self._s['state'] = self.OPEN
            self._s['opened_at'] = datetime.now().isoformat()
            logger.warning(f"  🔴 CircuitBreaker [{self.name}]: "
                             f"HALF_OPEN → OPEN")
        elif (self._s['state'] == self.CLOSED
                and self._s['failures'] >= threshold):
            self._s['state'] = self.OPEN
            self._s['opened_at'] = datetime.now().isoformat()
            logger.warning(
                f"  🔴 CircuitBreaker [{self.name}]: "
                f"CLOSED → OPEN ({self._s['failures']} failures)")

        self._save()

    def reset(self) -> None:
        """수동 리셋."""
        self._s['state'] = self.CLOSED
        self._s['failures'] = 0
        self._save()

    def get_status(self) -> Dict:
        """현재 상태 조회."""
        return {
            'name': self.name,
            'state': self._s['state'],
            'failures': self._s['failures'],
            'last_failure': self._s.get('last_failure'),
            'last_success': self._s.get('last_success'),
        }

    # ── 설정 ──

    def _get_threshold(self) -> int:
        key = f'circuit_breaker.{self.name}.failure_threshold'
        default_key = 'circuit_breaker.default_failure_threshold'
        if _cfg:
            val = _cfg.get(key)
            if val is not None:
                return int(val)
            return int(_cfg.get(default_key, 3))
        return 3

    def _get_timeout(self) -> int:
        """복구 대기 시간 (초)."""
        key = f'circuit_breaker.{self.name}.recovery_timeout'
        default_key = 'circuit_breaker.default_recovery_timeout'
        if _cfg:
            val = _cfg.get(key)
            if val is not None:
                return int(val)
            return int(_cfg.get(default_key, 1800))
        return 1800

    # ── 영속화 ──

    @classmethod
    def _load_all(cls) -> None:
        """전역 상태 로드."""
        cls._loaded = True
        if _STATE_FILE.exists():
            try:
                cls._states = json.loads(
                    _STATE_FILE.read_text(encoding='utf-8'))
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                cls._states = {}

    def _save(self) -> None:
        """전역 상태 저장."""
        try:
            from src.infra.safe_io import safe_json_write
            safe_json_write(_STATE_FILE, CircuitBreaker._states)
        except ImportError as e:
            try:
                _STATE_FILE.write_text(json.dumps(
                    CircuitBreaker._states, indent=2, default=str))
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).error(f'Targeted fallback: {e}', exc_info=True)
                pass

    @staticmethod
    def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return None

    @classmethod
    def get_all_status(cls) -> Dict:
        """전체 회로 상태 조회."""
        if not cls._loaded:
            cls._load_all()
        result = {}
        for name, state in cls._states.items():
            result[name] = {
                'state': state.get('state', 'CLOSED'),
                'failures': state.get('failures', 0),
            }
        return result
